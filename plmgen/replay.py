"""Read event streams back, apply them the way Silver should, and replay them over time.

apply_cdc shows the target behaviour for a Silver MERGE: ignore corrupt lines, drop
duplicate event ids, order by log sequence number, and apply creates, updates and deletes.
stream_files re-releases a batch's events in small files at a chosen rate, so Auto Loader
or a streaming job has something live to read.
"""

import json
import time
from pathlib import Path

from .schema import TABLES, format_value, parse_value


def read_events(paths):
    """Yield (event, error) for every line. Corrupt lines yield (None, line)."""
    for path in paths:
        with Path(path).open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line), None
                except json.JSONDecodeError:
                    yield None, line


def apply_cdc(state, events):
    """Apply CDC events to {table: {pk_tuple: row_json}} and return counts of what was ignored.

    Events are deduplicated by event_id and applied in lsn order, whatever order they arrived in.
    """
    seen, ignored = set(), {"duplicates": 0, "corrupt": 0}
    good = []
    for event, bad in events:
        if event is None:
            ignored["corrupt"] += 1
        elif event["event_id"] in seen:
            ignored["duplicates"] += 1
        else:
            seen.add(event["event_id"])
            good.append(event)
    for event in sorted(good, key=lambda e: (e["source"], e["lsn"])):
        table = state.setdefault(event["table"], {})
        key = tuple(event["key"][c] for c in TABLES[event["table"]].pk)
        if event["op"] == "d":
            table.pop(key, None)
        else:
            table[key] = event["after"]
    return ignored


def rows_to_json_state(table, rows):
    """Index typed rows by primary key in the same JSON form events use."""
    from .outputs import json_key, json_row
    spec = TABLES[table]
    return {tuple(json_key(table, tuple(r[c] for c in spec.pk)).values()): json_row(table, r) for r in rows}


def drip(source_dir, target_dir, interval=0.0, pattern="part-*.csv", max_ticks=None):
    """Deliver part files into target_dir in rounds, keeping the folder structure.

    Round 1 delivers the first part of every table, round 2 the second part, and so on, so each
    round adds a slice of every table, as an incremental initial load would. Files appear atomically.
    Returns the list of files delivered in each round."""
    import shutil
    source = Path(source_dir)
    groups = {}
    for path in sorted(source.rglob(pattern)):
        groups.setdefault(path.parent, []).append(path)
    rounds = max((len(files) for files in groups.values()), default=0)
    if max_ticks is not None:
        rounds = min(rounds, max_ticks)
    delivered = []
    for tick in range(rounds):
        batch = []
        for parent in sorted(groups):
            if tick < len(groups[parent]):
                path = groups[parent][tick]
                dest = Path(target_dir) / path.relative_to(source)
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(".tmp")
                shutil.copyfile(path, tmp)
                tmp.replace(dest)
                batch.append(dest)
        delivered.append(batch)
        if interval and tick < rounds - 1:
            time.sleep(interval)
    return delivered


def stream_files(source_file, target_dir, events_per_file=50, rate=None, shuffle_seed=None, prefix="events"):
    """Split one JSONL file into small files in target_dir, pausing between files to hit `rate` events/second.

    The original line order is kept. Returns the list of files written."""
    lines = [ln for ln in Path(source_file).read_text(encoding="utf-8").splitlines() if ln.strip()]
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for n, start in enumerate(range(0, len(lines), events_per_file), 1):
        chunk = lines[start:start + events_per_file]
        path = target / f"{prefix}-{n:06d}.jsonl"
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(chunk) + "\n", encoding="utf-8")
        tmp.replace(path)            # appears atomically, as a real landing process would
        written.append(path)
        if rate:
            time.sleep(len(chunk) / rate)
    return written


# ---- delivering micro-batches in real time ---------------------------------------------------------

def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


def retime_jsonl(text, stamp):
    """Set every event's time to `stamp`, keeping the original in orig_<field>. Corrupt lines pass through."""
    out = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            out.append(line)
            continue
        if isinstance(event, dict):
            for field in ("ts", "modified_at", "occurred_at"):
                if field in event:
                    event.setdefault(f"orig_{field}", event[field])
                    event[field] = stamp
            line = json.dumps(event, separators=(",", ":"))
        out.append(line)
    return "\n".join(out) + "\n"


def retime_csv(text, stamp):
    """Set the _ts column of a delta extract to `stamp`."""
    import csv
    import io
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or "_ts" not in rows[0]:
        return text
    at = rows[0].index("_ts")
    for row in rows[1:]:
        row[at] = stamp
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows(rows)
    return out.getvalue()


def release_batch(batch_dir, target_dir, interval=0.0, retime=False, max_windows=None):
    """Deliver one batch folder window by window, as a micro-batch feed would.

    Files land under target_dir/<batch folder name>/..., each atomically, and every file of a window
    arrives before the pause. With retime=True the events are restamped to the moment of release, so a
    consumer can measure its own apply lag. Returns [(window, [files delivered])]."""
    from .lakebase import window_groups
    batch = Path(batch_dir)
    groups = list(window_groups(batch).items())
    if max_windows is not None:
        groups = groups[:max_windows]
    delivered = []
    for n, (window, files) in enumerate(groups):
        stamp = _now_iso()
        landed = []
        for path in files:
            dest = Path(target_dir) / batch.name / path.relative_to(batch)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp")
            text = path.read_text(encoding="utf-8")
            if retime:
                text = retime_jsonl(text, stamp) if path.suffix == ".jsonl" else retime_csv(text, stamp)
            tmp.write_text(text, encoding="utf-8", newline="\n")
            tmp.replace(dest)
            landed.append(dest)
        delivered.append((window or "day", landed))
        if interval and n < len(groups) - 1:
            time.sleep(interval)
    return delivered
