"""Turn simulation state and change records into what each source system would deliver.

How each source reaches the lakehouse (matching the architecture diagram):
  sap_pdm        CDC stream            JSON lines, Debezium-style envelope
  part_release   workflow event mesh   JSON lines, domain events
  windchill      REST / OData          change log (JSON lines) plus the mock API in api.py
  bom_mgmt       batch extracts        delta CSV with _op / _seq / _ts
  master         batch extracts        delta CSV (master data export)
  project_docs   file drops            document files plus a metadata JSON per file (see docs.py)

The base extract is a full snapshot of every table, one folder per source.

Dirty mode adds known problems to what is delivered, never to the truth in state/. Each
batch records exactly what it injected in _batch.json, so tests can assert on it.
"""

import csv
import json
import random
from collections import Counter
from datetime import timedelta
from pathlib import Path

from .db import write_rows
from .schema import TABLES, format_value, to_json_value
from .util import bom_key, idx_from_bom_key, pn_idx, pn_of, seq_of, wt_id_of

WINDCHILL_ENTITY = {
    "windchill_part": "Part", "windchill_part_revision": "PartRevision",
    "windchill_part_supersession": "PartSupersession", "windchill_change_notice": "ChangeNotice",
    "windchill_change_affected_item": "AffectedItem", "windchill_change_bom_delta": "BomDelta",
    "windchill_cad_document": "CadDocument",
}
OPS = {"c": "c", "u": "u", "d": "d"}


# ---- helpers ------------------------------------------------------------------------------------

def json_row(table, row):
    if row is None:
        return None
    spec = TABLES[table]
    return {c.name: to_json_value(row[c.name], c.type) for c in spec.cols}


def json_key(table, pk):
    spec = TABLES[table]
    return {name: to_json_value(value, spec.col(name).type) for name, value in zip(spec.pk, pk)}


def iso_ts(ts):
    return ts.isoformat(timespec="milliseconds") + "Z"


def write_jsonl(path, lines):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for line in lines:
            fh.write(line if isinstance(line, str) else json.dumps(line, separators=(",", ":")))
            fh.write("\n")


def write_delta_csv(path, table, records):
    """records: list of (op, seq, ts, row). Columns: _op, _seq, _ts, then the table columns."""
    spec = TABLES[table]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["_op", "_seq", "_ts"] + spec.col_names)
        for op, seq, ts, row in records:
            writer.writerow([op, seq, iso_ts(ts)] + [format_value(row[c.name], c.type) for c in spec.cols])


# ---- per-day delivery ---------------------------------------------------------------------------

class Emitter:
    def __init__(self, out_dir, manifest, seed, dirty=False):
        self.out = Path(out_dir)
        self.manifest = manifest
        self.seed = seed
        self.dirty = dirty
        self.manifest.setdefault("lsn", {})
        self.manifest.setdefault("event_seq", 0)

    def _next_lsn(self, source):
        self.manifest["lsn"][source] = self.manifest["lsn"].get(source, 0) + 1
        return self.manifest["lsn"][source]

    def emit_day(self, day, changes, folder=None, extra=None):
        """Write one day's deliveries. Returns a summary dict (also written as _batch.json).
        `extra` is merged into the summary, for example the document counts."""
        folder = Path(folder) if folder else self.out / "batches" / f"batch={day.isoformat()}"
        drng = random.Random(f"{self.seed}:{day}:dirty")
        files, injected = [], Counter()

        by_source = {}
        for change in changes:
            by_source.setdefault(TABLES[change.table].source, []).append(change)

        # SAP PDM: CDC stream.
        sap_events = []
        for change in by_source.get("sap_pdm", []):
            lsn = self._next_lsn("sap_pdm")
            sap_events.append({
                "event_id": f"sap_pdm-{lsn:012d}", "source": "sap_pdm", "table": change.table, "op": OPS[change.op],
                "ts": iso_ts(change.ts), "lsn": lsn, "key": json_key(change.table, change.pk),
                "before": json_row(change.table, change.before), "after": json_row(change.table, change.after)})
        if sap_events:
            lines = self._degrade_events(sap_events, drng, injected, "sap_pdm")
            path = folder / "sap_pdm" / "cdc" / "events.jsonl"
            write_jsonl(path, lines)
            files.append({"path": path.relative_to(folder).as_posix(), "rows": len(lines), "kind": "cdc"})

        # Part release: workflow events.
        workflow = []
        for change in by_source.get("part_release", []):
            event = self._workflow_event(change)
            if event:
                workflow.append(event)
        if workflow:
            lines = self._degrade_events(workflow, drng, injected, "part_release")
            path = folder / "part_release" / "events" / "events.jsonl"
            write_jsonl(path, lines)
            files.append({"path": path.relative_to(folder).as_posix(), "rows": len(lines), "kind": "events"})

        # Windchill: change log behind the REST/OData API.
        log = []
        for change in by_source.get("windchill", []):
            lsn = self._next_lsn("windchill")
            row = change.after if change.after is not None else change.before
            log.append({"entity": WINDCHILL_ENTITY[change.table], "table": change.table, "op": OPS[change.op],
                        "lsn": lsn, "modified_at": iso_ts(change.ts), "key": json_key(change.table, change.pk),
                        "data": json_row(change.table, row)})
        if log:
            path = folder / "windchill" / "changelog" / "changes.jsonl"
            write_jsonl(path, log)
            files.append({"path": path.relative_to(folder).as_posix(), "rows": len(log), "kind": "changelog"})

        # BOM management and master data: delta CSV extracts.
        for source, kind in (("bom_mgmt", "delta"), ("master", "delta")):
            per_table = {}
            for change in by_source.get(source, []):
                row = change.after if change.after is not None else change.before
                per_table.setdefault(change.table, []).append((OPS[change.op], change.seq, change.ts, row))
            for table, records in sorted(per_table.items()):
                if self.dirty and len(records) >= 20:
                    dupes = [records[drng.randrange(len(records))] for _ in range(max(1, len(records) // 50))]
                    records = records + dupes
                    injected[f"{source}_duplicate_rows"] += len(dupes)
                path = folder / source / "delta" / f"{table}.csv"
                write_delta_csv(path, table, records)
                files.append({"path": path.relative_to(folder).as_posix(), "rows": len(records), "kind": "delta"})

        summary = {"date": day.isoformat(), "dirty": self.dirty, "files": files, "injected": dict(injected),
                   "changes": len(changes), **(extra or {})}
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "_batch.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return summary

    def _workflow_event(self, change):
        self.manifest["event_seq"] += 1
        seq = self.manifest["event_seq"]
        row = change.after if change.after is not None else change.before
        base = {"event_id": f"evt-{seq:012d}", "sequence": seq, "occurred_at": iso_ts(change.ts),
                "schema_version": 1, "aggregate_type": "ChangeNotice", "aggregate_id": row["change_number"]}
        if change.table == "part_release_task":
            if change.op == "c":
                return {**base, "event_type": "TaskAssigned", "payload": json_row(change.table, row)}
            if change.op == "u" and change.before["decision"] == "Pending" and change.after["decision"] != "Pending":
                return {**base, "event_type": "TaskDecided", "payload": json_row(change.table, row)}
        elif change.table == "part_release_release" and change.op == "c":
            return {**base, "event_type": "ReleaseIssued", "payload": json_row(change.table, row)}
        self.manifest["event_seq"] -= 1
        return None

    def _degrade_events(self, events, drng, injected, label):
        """Dirty mode: duplicates, out-of-order delivery, unexpected fields and a corrupt line."""
        if not self.dirty:
            return events
        lines = [dict(e) for e in events]
        for e in lines:
            if drng.random() < 0.03:
                e["extra"] = {"hint": "unexpected field"}
                injected[f"{label}_unexpected_field"] += 1
        dupes = [dict(lines[drng.randrange(len(lines))]) for _ in range(max(1, len(lines) // 50))]
        injected[f"{label}_duplicate_events"] += len(dupes)
        lines.extend(dupes)
        for _ in range(max(1, len(lines) // 20)):
            i, j = drng.randrange(len(lines)), drng.randrange(len(lines))
            if i != j:
                lines[i], lines[j] = lines[j], lines[i]
                injected[f"{label}_out_of_order_swaps"] += 1
        if len(lines) >= 10:
            broken = json.dumps(lines[drng.randrange(len(lines))], separators=(",", ":"))
            lines.insert(drng.randrange(len(lines)), broken[: len(broken) // 2])
            injected[f"{label}_corrupt_lines"] += 1
        return lines


# ---- base extract ---------------------------------------------------------------------------------

def write_base(tables, cfg, out_dir, dirty=False):
    """Write the full snapshot, one folder per source. Returns what was injected."""
    out_dir = Path(out_dir)
    issues = Counter()
    snapshot = tables
    if dirty:
        snapshot, issues = inject_snapshot_problems(tables, cfg)
    for name, spec in TABLES.items():
        (out_dir / spec.source).mkdir(parents=True, exist_ok=True)
        write_rows(out_dir / spec.source / f"{name}.csv", spec, snapshot[name])
    (out_dir / "_base.json").write_text(json.dumps({
        "as_of": cfg.as_of, "dirty": dirty, "injected": dict(issues),
        "row_counts": {name: len(rows) for name, rows in snapshot.items()}}, indent=2), encoding="utf-8")
    return dict(issues), snapshot


def inject_snapshot_problems(tables, cfg):
    """Return a copy of the tables with a fixed number of known problems added."""
    drng = random.Random(f"{cfg.seed}:{cfg.as_of}:base-dirty")
    t = {name: [dict(r) for r in rows] for name, rows in tables.items()}
    issues = Counter()
    as_of = cfg.as_of_date

    # Mass delivered in grams. Valid data that needs unit conversion, so it is not a violation.
    materials = t["sap_pdm_material"]
    for i in drng.sample(range(len(materials)), round(len(materials) * 0.08)):
        materials[i]["gewei"] = "G"
        materials[i]["mass_value"] = round(materials[i]["mass_value"] * 1000, 1)
        issues["mass_in_grams_sap"] += 1

    lines = t["bom_mgmt_ebom_line"]
    next_seq = max((seq_of(r["ebom_line_id"]) for r in lines), default=0)
    active = [r for r in lines if r["effective_to"] is None]
    template = active[0] if active else None

    def new_line(parent_key, child_key, quantity):
        nonlocal next_seq
        next_seq += 1
        return {**template, "ebom_line_id": f"EBOM-{next_seq:07d}", "parent_item_key": parent_key,
                "child_item_key": child_key, "quantity": quantity, "find_number": 999,
                "effective_from": as_of, "effective_to": None}

    if template:
        # Orphan EBOM lines: a child key that exists in no source.
        for i in range(max(3, len(lines) // 500)):
            parent = drng.choice(active)["parent_item_key"]
            lines.append(new_line(parent, f"BM-{9000000 + i:07d}", 1))
            issues["orphan_ebom_lines"] += 1

        # Lifecycle-violating lines: a Released assembly containing a Design or Obsolete leaf.
        state = {p["part_number"]: p["canonical_lifecycle"] for p in t["master_part"]}
        has_children = {r["parent_item_key"] for r in active}
        pairs = {(r["parent_item_key"], r["child_item_key"]) for r in active}
        released_parents = sorted({r["parent_item_key"] for r in active
                                   if state[pn_of(idx_from_bom_key(r["parent_item_key"]))] == "Released"})
        unfit = sorted(bom_key(pn_idx(pn)) for pn, s in state.items()
                       if s in ("Design", "Obsolete") and bom_key(pn_idx(pn)) not in has_children)
        wanted = max(3, len(lines) // 300)
        for _ in range(wanted * 30):
            if issues["lifecycle_violating_bom_lines"] >= wanted or not released_parents or not unfit:
                break
            parent, child = drng.choice(released_parents), drng.choice(unfit)
            if (parent, child) in pairs or parent == child:
                continue
            pairs.add((parent, child))
            lines.append(new_line(parent, child, 1))
            issues["lifecycle_violating_bom_lines"] += 1

    # Changes dated before any part existed.
    notices = t["windchill_change_notice"]
    for i in drng.sample(range(len(notices)), min(len(notices), max(3, len(notices) // 200)) if notices else 0):
        notices[i]["created_on"] = cfg.start_date - timedelta(days=30)
        issues["time_travel_changes"] += 1

    # Changes that touch parts that are not Released.
    created = {r["change_number"]: r["created_on"] for r in tables["windchill_change_notice"]}
    open_changes = sorted(r["change_number"] for r in notices if r["state"] != "Implemented")
    unreleased = [p for p in t["master_part"] if p["canonical_lifecycle"] != "Released"]
    existing = {(a["change_number"], a["wt_id"]) for a in t["windchill_change_affected_item"]}
    wanted = max(3, len(notices) // 100) if notices else 0
    for _ in range(wanted * 30):
        if issues["changes_on_unreleased_parts"] >= wanted or not open_changes or not unreleased:
            break
        change, part = drng.choice(open_changes), drng.choice(unreleased)
        wt = wt_id_of(pn_idx(part["part_number"]))
        if part["created_date"] > created[change] or (change, wt) in existing:
            continue
        existing.add((change, wt))
        t["windchill_change_affected_item"].append({"change_number": change, "wt_id": wt,
                                                    "before_version": "A.1", "after_version": "B.1"})
        issues["changes_on_unreleased_parts"] += 1
    return t, issues
