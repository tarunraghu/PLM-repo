"""Apply micro-batches to a Postgres-compatible target and verify it against the truth.

This is the harness for proving that lifecycle updates and deletes reach an operational store
(Lakebase) within a micro-batch, and that the store ends up exactly equal to the source.

The target holds mirror tables, one per source table, plus three bookkeeping columns:
  _lsn         log sequence number of the last event applied to the row
  _source_ts   when the change happened in the source
  _applied_at  when this harness wrote it

A micro-batch window is applied in one transaction. Within a window, duplicate events are dropped,
corrupt lines are counted and skipped, and events run in time order. Upserts only win when their
_lsn is higher than the stored one, and deletes only remove a row whose _lsn is lower, so replaying
a file twice or receiving an old event late cannot move a row backwards.

Targets: sqlite:///path/to.db (for local testing) or postgresql://user:password@host:5432/db for
Postgres and Lakebase. Lakebase credentials are short-lived OAuth tokens, so long runs must refresh
the URL. The PostgreSQL path needs psycopg:  pip install "psycopg[binary]".
"""

import csv
import json
import re
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from .db import read_rows
from .naming import parse_batch_file
from .schema import POSTGRES_TYPES, TABLES, format_value, parse_value

# Tables that carry product lifecycle: state, revision, and the part records in SAP and Windchill.
LIFECYCLE_TABLES = ("master_part", "master_part_lifecycle_history", "sap_pdm_material", "windchill_part",
                    "windchill_part_revision")
SQLITE_TYPES = {"str": "TEXT", "int": "INTEGER", "float": "REAL", "date": "TEXT", "ts": "TEXT", "bool": "INTEGER"}
BOOKKEEPING = ("_lsn", "_source_ts", "_applied_at")


class Target:
    """A thin wrapper so the same code runs on SQLite and PostgreSQL."""

    def __init__(self, url, schema="plm_mirror"):
        self.url = url
        if url.startswith("sqlite:///"):
            self.dialect, self.ph, self.prefix = "sqlite", "?", ""
            self.conn = sqlite3.connect(url[len("sqlite:///"):])
        elif url.startswith(("postgresql://", "postgres://")):
            try:
                import psycopg
            except ImportError:
                raise SystemExit('PostgreSQL and Lakebase targets need psycopg: pip install "psycopg[binary]"')
            self.dialect, self.ph, self.prefix = "postgres", "%s", f"{schema}."
            self.conn = psycopg.connect(url)
            self.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
            self.commit()
        else:
            raise SystemExit("target must be sqlite:///file.db or postgresql://user:password@host:5432/database")

    def t(self, table):
        return f"{self.prefix}{table}"

    def execute(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        return cur

    def many(self, sql, rows):
        cur = self.conn.cursor()
        cur.executemany(sql, rows)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()

    def stamp(self, ts):
        """A timestamp in the form this target stores."""
        if self.dialect == "sqlite":
            return None if ts is None else ts.replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"
        return None if ts is None else (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc))


def now_utc():
    return datetime.now(timezone.utc)


def to_db(value, type_, dialect):
    if value is None or dialect == "postgres":
        return value
    if type_ in ("date", "ts"):
        return format_value(value, type_)
    if type_ == "bool":
        return 1 if value else 0
    return value


def db_text(value, type_):
    """Normalise a stored value to the text form used in the CSV files, so both sides compare equal."""
    if value is None:
        return ""
    if type_ == "bool":
        return "true" if value in (1, True) else "false"
    if type_ == "ts":
        if isinstance(value, datetime):
            return format_value(value.astimezone(timezone.utc).replace(tzinfo=None), "ts")
        return str(value)
    if type_ == "date":
        return value.isoformat() if isinstance(value, date) else str(value)
    if type_ == "float":
        return str(float(value))
    return str(value)


# ---- schema ------------------------------------------------------------------------------------------

def create_tables(target, tables):
    types = POSTGRES_TYPES if target.dialect == "postgres" else SQLITE_TYPES
    ts_type = "timestamptz" if target.dialect == "postgres" else "TEXT"
    for name in tables:
        spec = TABLES[name]
        cols = [f"{c.name} {types[c.type]}" + ("" if c.nullable else " NOT NULL") for c in spec.cols]
        cols += ["_lsn bigint NOT NULL DEFAULT 0", f"_source_ts {ts_type}", f"_applied_at {ts_type}"]
        cols.append(f"PRIMARY KEY ({', '.join(spec.pk)})")
        target.execute(f"CREATE TABLE IF NOT EXISTS {target.t(name)} ({', '.join(cols)})")
    target.execute(
        f"CREATE TABLE IF NOT EXISTS {target.t('_plm_apply_log')} (batch text NOT NULL, win text NOT NULL, "
        f"events bigint, deletes bigint, duplicates bigint, corrupt bigint, oldest_lag_seconds double precision, "
        f"newest_lag_seconds double precision, applied_at {ts_type})"
        if target.dialect == "postgres" else
        f"CREATE TABLE IF NOT EXISTS {target.t('_plm_apply_log')} (batch TEXT NOT NULL, win TEXT NOT NULL, "
        f"events INTEGER, deletes INTEGER, duplicates INTEGER, corrupt INTEGER, oldest_lag_seconds REAL, "
        f"newest_lag_seconds REAL, applied_at TEXT)")
    target.commit()


def load_base(target, tables, base_dir):
    """Bulk-load the base extract (flat, single-file or part layout) into the mirror tables."""
    from .validate import tables_from_dir
    data = tables_from_dir(base_dir)
    applied = target.stamp(now_utc())
    loaded = {}
    for name in tables:
        spec = TABLES[name]
        cols = spec.col_names + list(BOOKKEEPING)
        sql = f"INSERT INTO {target.t(name)} ({', '.join(cols)}) VALUES ({', '.join([target.ph] * len(cols))})"
        rows = [[to_db(r[c.name], c.type, target.dialect) for c in spec.cols] + [0, None, applied]
                for r in data[name]]
        for start in range(0, len(rows), 1000):
            target.many(sql, rows[start:start + 1000])
        loaded[name] = len(rows)
    target.commit()
    return loaded


# ---- reading batches --------------------------------------------------------------------------------

def _from_json(value, type_):
    if value is None:
        return None
    if type_ == "date":
        return date.fromisoformat(value)
    if type_ == "ts":
        return datetime.fromisoformat(value.rstrip("Z"))
    return value


def _json_row(table, data):
    return {c.name: _from_json(data[c.name], c.type) for c in TABLES[table].cols}


def read_window_records(files, tables):
    """Read event and delta files into normalised records. Returns (records, counters).

    Each record: dict(table, op, key, row, lsn, ts, uid). Corrupt lines are counted, not raised."""
    records, seen, counts = [], set(), Counter()
    for path in files:
        path = Path(path)
        if path.suffix == ".jsonl":
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        counts["corrupt"] += 1
                        continue
                    if e.get("table") not in tables:
                        continue
                    uid = e.get("event_id") or f"{e['table']}:{e['lsn']}"
                    if uid in seen:
                        counts["duplicates"] += 1
                        continue
                    seen.add(uid)
                    row = e.get("after") if e["op"] != "d" else e.get("before")
                    if row is None:
                        row = e.get("data")
                    key = tuple(_from_json(e["key"][c], TABLES[e["table"]].col(c).type) for c in TABLES[e["table"]].pk)
                    ts = datetime.fromisoformat((e.get("ts") or e.get("modified_at")).rstrip("Z"))
                    records.append({"table": e["table"], "op": e["op"], "key": key, "lsn": e["lsn"], "ts": ts,
                                    "row": _json_row(e["table"], row) if e["op"] != "d" else None, "uid": uid})
        elif path.suffix == ".csv":
            parsed = parse_batch_file(path.name)
            table = parsed[0] if parsed else path.stem
            if table not in tables:
                continue
            spec = TABLES[table]
            with path.open(newline="", encoding="utf-8") as fh:
                for raw in csv.DictReader(fh):
                    uid = f"{table}:{raw['_seq']}"
                    if uid in seen:
                        counts["duplicates"] += 1
                        continue
                    seen.add(uid)
                    row = {c.name: parse_value(raw[c.name], c.type) for c in spec.cols}
                    records.append({"table": table, "op": raw["_op"], "key": tuple(row[c] for c in spec.pk),
                                    "lsn": int(raw["_seq"]), "ts": datetime.fromisoformat(raw["_ts"].rstrip("Z")),
                                    "row": row if raw["_op"] != "d" else None, "uid": uid})
    records.sort(key=lambda r: (r["ts"], r["table"], r["lsn"]))
    return records, counts


# ---- applying ---------------------------------------------------------------------------------------

def _upsert_sql(target, table):
    spec = TABLES[table]
    cols = spec.col_names + list(BOOKKEEPING)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in spec.pk)
    return (f"INSERT INTO {target.t(table)} AS t ({', '.join(cols)}) VALUES ({', '.join([target.ph] * len(cols))}) "
            f"ON CONFLICT ({', '.join(spec.pk)}) DO UPDATE SET {updates} WHERE t._lsn < excluded._lsn")


def _delete_sql(target, table):
    spec = TABLES[table]
    where = " AND ".join(f"{c} = {target.ph}" for c in spec.pk)
    return f"DELETE FROM {target.t(table)} WHERE {where} AND _lsn < {target.ph}"


def apply_records(target, records, batch="", window="", counts=None):
    """Apply one window of records in a single transaction and log it. Returns a summary dict."""
    counts = Counter(counts or {})
    applied_at = now_utc()
    stamp = target.stamp(applied_at)
    try:
        for r in records:
            spec = TABLES[r["table"]]
            if r["op"] == "d":
                key = [to_db(v, spec.col(c).type, target.dialect) for v, c in zip(r["key"], spec.pk)]
                target.execute(_delete_sql(target, r["table"]), key + [r["lsn"]])
                counts["deletes"] += 1
            else:
                values = [to_db(r["row"][c.name], c.type, target.dialect) for c in spec.cols]
                target.execute(_upsert_sql(target, r["table"]),
                               values + [r["lsn"], target.stamp(r["ts"]), stamp])
                counts["upserts"] += 1
        oldest = (applied_at.replace(tzinfo=None) - min(r["ts"] for r in records)).total_seconds() if records else None
        newest = (applied_at.replace(tzinfo=None) - max(r["ts"] for r in records)).total_seconds() if records else None
        target.execute(
            f"INSERT INTO {target.t('_plm_apply_log')} (batch, win, events, deletes, duplicates, corrupt, "
            f"oldest_lag_seconds, newest_lag_seconds, applied_at) VALUES ({', '.join([target.ph] * 9)})",
            [batch, window or "day", len(records), counts["deletes"], counts["duplicates"], counts["corrupt"],
             oldest, newest, stamp])
        target.commit()
    except Exception:
        target.rollback()
        raise
    return {"batch": batch, "window": window or "day", "events": len(records), "upserts": counts["upserts"],
            "deletes": counts["deletes"], "duplicates": counts["duplicates"], "corrupt": counts["corrupt"],
            "oldest_lag_seconds": oldest, "newest_lag_seconds": newest}


def window_groups(batch_dir):
    """Group a batch folder's event and delta files by micro-batch window, in time order."""
    groups = defaultdict(list)
    for path in sorted(Path(batch_dir).rglob("*")):
        if path.suffix not in (".jsonl", ".csv") or path.name.endswith(".tmp"):
            continue
        if path.parent.name not in ("cdc", "changelog", "events", "delta"):
            continue
        parsed = parse_batch_file(path.name)
        groups[parsed[2] if parsed else ""].append(path)
    return dict(sorted(groups.items()))


def apply_batch(target, batch_dir, tables):
    """Apply every window of one batch folder, oldest window first. Returns a list of summaries."""
    out = []
    for window, files in window_groups(batch_dir).items():
        records, counts = read_window_records(files, set(tables))
        out.append(apply_records(target, records, Path(batch_dir).name, window, counts))
    return out


def apply_batches(target, batches_dir, tables, first=None, last=None):
    summaries = []
    for batch in sorted(Path(batches_dir).glob("batch=*")):
        day = batch.name.split("=", 1)[1]
        if (first and day < first) or (last and day > last):
            continue
        summaries += apply_batch(target, batch, tables)
    return summaries


def watch(target, landing_dir, tables, interval=1.0, idle_polls=3, max_seconds=600):
    """Poll a landing folder and apply each micro-batch window as soon as its files appear.

    Stops after idle_polls polls with nothing new. Returns the list of window summaries."""
    landing, done, idle, summaries = Path(landing_dir), set(), 0, []
    started = time.time()
    while idle < idle_polls and time.time() - started < max_seconds:
        fresh = {}
        for batch in sorted(landing.glob("batch=*")):
            for window, files in window_groups(batch).items():
                new = [f for f in files if f not in done]
                if new:
                    fresh[(batch.name, window)] = new
        if not fresh:
            idle += 1
        else:
            idle = 0
            for (batch, window), files in sorted(fresh.items()):
                records, counts = read_window_records(files, set(tables))
                summaries.append(apply_records(target, records, batch, window, counts))
                done.update(files)
        time.sleep(interval)
    return summaries


# ---- verifying --------------------------------------------------------------------------------------

def verify(target, tables, state_dir, withdrawn_file=None, max_examples=5):
    """Compare mirror tables with the truth. Returns a report; problems == 0 means they are identical."""
    from .validate import tables_from_dir
    truth = tables_from_dir(state_dir)
    report, problems = {}, 0
    for name in tables:
        spec = TABLES[name]
        expected = {tuple(format_value(r[c], spec.col(c).type) for c in spec.pk):
                    tuple(format_value(r[c.name], c.type) for c in spec.cols) for r in truth[name]}
        actual = {}
        for row in target.execute(f"SELECT {', '.join(spec.col_names)} FROM {target.t(name)}").fetchall():
            values = tuple(db_text(v, c.type) for v, c in zip(row, spec.cols))
            actual[tuple(values[spec.col_names.index(c)] for c in spec.pk)] = values
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        different = sorted(k for k in set(expected) & set(actual) if expected[k] != actual[k])
        # Mass delivered in grams is the same fact in another unit, so it is reported but is not a problem.
        equivalent = [k for k in different if _same_after_unit_conversion(name, spec, expected[k], actual[k])]
        different = [k for k in different if k not in equivalent]
        problems += len(missing) + len(extra) + len(different)
        report[name] = {"expected_rows": len(expected), "actual_rows": len(actual), "missing": len(missing),
                        "extra": len(extra), "different": len(different), "equivalent_after_unit_conversion": len(equivalent),
                        "examples": {"missing": missing[:max_examples], "extra": extra[:max_examples],
                                     "different": different[:max_examples]}}
    if withdrawn_file and Path(withdrawn_file).exists():
        withdrawn = [json.loads(line) for line in Path(withdrawn_file).read_text().splitlines() if line.strip()]
        still_there = 0
        for table, column, field in (("master_part", "part_number", "part_number"), ("sap_pdm_material", "matnr", "matnr"),
                                     ("windchill_part", "wt_id", "wt_id")):
            if table in tables:
                for w in withdrawn:
                    still_there += target.execute(f"SELECT COUNT(*) FROM {target.t(table)} WHERE {column} = {target.ph}",
                                                  [w[field]]).fetchone()[0]
        report["withdrawn_parts"] = {"deleted_in_source": len(withdrawn), "still_present_in_target": still_there}
        problems += still_there
    report["problems"] = problems
    return report


def _same_after_unit_conversion(table, spec, expected, actual):
    """True when a SAP row differs only because its mass is in grams (gewei G) instead of kilograms."""
    if table != "sap_pdm_material":
        return False
    names = spec.col_names
    g, m = names.index("gewei"), names.index("mass_value")
    if actual[g] != "G" or expected[g] != "KG":
        return False
    if abs(float(actual[m]) * 0.001 - float(expected[m])) > 1e-6:
        return False
    return all(a == e for i, (a, e) in enumerate(zip(actual, expected)) if i not in (g, m))


def lag_summary(target):
    """Lag statistics from the apply log. Only meaningful for batches whose timestamps were retimed to the
    release time (replay --retime); otherwise the timestamps are simulated history."""
    rows = target.execute(f"SELECT oldest_lag_seconds, newest_lag_seconds, events FROM {target.t('_plm_apply_log')} "
                          f"WHERE events > 0").fetchall()
    if not rows:
        return {"windows": 0}
    oldest = sorted(r[0] for r in rows)
    newest = sorted(r[1] for r in rows)

    def pct(values, p):
        return round(values[min(len(values) - 1, int(len(values) * p))], 3)

    return {"windows": len(rows), "events": sum(r[2] for r in rows),
            "apply_lag_seconds_oldest_event": {"p50": pct(oldest, 0.5), "p95": pct(oldest, 0.95), "max": round(oldest[-1], 3)},
            "apply_lag_seconds_newest_event": {"p50": pct(newest, 0.5), "p95": pct(newest, 0.95), "max": round(newest[-1], 3)}}
