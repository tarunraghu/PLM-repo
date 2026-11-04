"""A small in-memory database with change capture.

Every insert, update and delete goes through here, is checked against the schema,
and is logged as a ChangeRecord. The simulation writes only through this class,
so CDC events fall out of the change log with no extra work.

Rows are plain dicts holding typed Python values. Iteration order is never
relied on: lookups return rows sorted by primary key, and the CSV writer sorts
too, so results do not depend on how a database was built.
"""

import csv
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .schema import TABLES, format_value, parse_value

_PY_TYPES = {"str": str, "int": int, "float": (float, int), "date": date, "ts": datetime, "bool": bool}


@dataclass
class ChangeRecord:
    seq: int
    table: str
    op: str          # c, u, d
    pk: tuple
    before: dict
    after: dict
    ts: datetime


class TableStore:
    def __init__(self, spec):
        self.spec = spec
        self.rows = {}
        self.indexes = {name: {} for name in spec.indexes}

    def pk_of(self, row):
        return tuple(row[c] for c in self.spec.pk)

    def _ikey(self, name, row):
        cols = self.spec.indexes[name]
        return row[cols[0]] if len(cols) == 1 else tuple(row[c] for c in cols)

    def index_add(self, pk, row):
        for name in self.indexes:
            self.indexes[name].setdefault(self._ikey(name, row), {})[pk] = True

    def index_remove(self, pk, row):
        for name in self.indexes:
            bucket = self.indexes[name].get(self._ikey(name, row))
            if bucket is not None:
                bucket.pop(pk, None)
                if not bucket:
                    del self.indexes[name][self._ikey(name, row)]


class Database:
    def __init__(self):
        self.t = {name: TableStore(spec) for name, spec in TABLES.items()}
        self.changes = []
        self.now = datetime(2000, 1, 1)
        self._clock_rng = None
        self._mean_gap = 0.045
        self._day_end = None
        self._seq = 0

    # ---- clock ------------------------------------------------------------------------------
    def begin_day(self, day, clock_rng, mean_gap=0.045):
        """Start a simulated day. Timestamps move forward from 06:00 with random gaps averaging
        mean_gap seconds, so a day's activity spreads across the working day when the mean is large."""
        self.now = datetime.combine(day, time(6, 0))
        self._clock_rng = clock_rng
        self._mean_gap = mean_gap
        self._day_end = datetime.combine(day, time(23, 59, 59))

    def _stamp(self):
        if self._clock_rng is not None:
            step = timedelta(seconds=max(0.001, self._clock_rng.expovariate(1.0 / self._mean_gap)))
            # Past the end of the day, keep ordering with microsecond steps instead of spilling into tomorrow.
            self.now = self.now + step if self.now + step <= self._day_end else self.now + timedelta(microseconds=1)

    # ---- validation -------------------------------------------------------------------------
    def _normalise(self, table, row):
        spec = self.t[table].spec
        out = {}
        for col in spec.cols:
            value = row.get(col.name)
            if value is None:
                if not col.nullable:
                    raise ValueError(f"{table}.{col.name} cannot be null")
                out[col.name] = None
                continue
            if col.type == "float" and isinstance(value, int) and not isinstance(value, bool):
                value = float(value)
            ok = isinstance(value, _PY_TYPES[col.type])
            if col.type == "int" and isinstance(value, bool):
                ok = False
            if col.type == "date" and isinstance(value, datetime):
                ok = False
            if not ok:
                raise TypeError(f"{table}.{col.name} expected {col.type}, got {value!r}")
            out[col.name] = value
        extra = set(row) - set(spec.col_names)
        if extra:
            raise KeyError(f"{table} has no columns {sorted(extra)}")
        return out

    # ---- writes -----------------------------------------------------------------------------
    def _log(self, table, op, pk, before, after):
        self._seq += 1
        self.changes.append(ChangeRecord(self._seq, table, op, pk, before, after, self.now))

    def insert(self, table, row, log=True):
        store = self.t[table]
        if "modified_at" in store.spec.col_names and log:
            self._stamp()
            row = {**row, "modified_at": self.now}
        row = self._normalise(table, row)
        pk = store.pk_of(row)
        if pk in store.rows:
            raise KeyError(f"duplicate key {pk} in {table}")
        store.rows[pk] = row
        store.index_add(pk, row)
        if log:
            if "modified_at" not in store.spec.col_names:
                self._stamp()
            self._log(table, "c", pk, None, dict(row))
        return row

    def update(self, table, pk, **changes):
        store = self.t[table]
        pk = pk if isinstance(pk, tuple) else (pk,)
        before = store.rows[pk]
        after = self._normalise(table, {**before, **changes})
        if all(after[c] == before[c] for c in after if c != "modified_at"):
            return before
        if "modified_at" in store.spec.col_names:
            self._stamp()
            after["modified_at"] = self.now
        if store.pk_of(after) != pk:
            raise KeyError("primary keys cannot change")
        store.index_remove(pk, before)
        store.rows[pk] = after
        store.index_add(pk, after)
        if "modified_at" not in store.spec.col_names:
            self._stamp()
        self._log(table, "u", pk, dict(before), dict(after))
        return after

    def delete(self, table, pk):
        store = self.t[table]
        pk = pk if isinstance(pk, tuple) else (pk,)
        before = store.rows.pop(pk)
        store.index_remove(pk, before)
        self._stamp()
        self._log(table, "d", pk, dict(before), None)

    # ---- reads ------------------------------------------------------------------------------
    def get(self, table, pk):
        return self.t[table].rows.get(pk if isinstance(pk, tuple) else (pk,))

    def find(self, table, index, value):
        """Rows matching an index value, sorted by primary key."""
        bucket = self.t[table].indexes[index].get(value, {})
        rows = self.t[table].rows
        return [rows[pk] for pk in sorted(bucket)]

    def count(self, table):
        return len(self.t[table].rows)

    def rows(self, table):
        """All rows sorted by primary key."""
        rows = self.t[table].rows
        return [rows[pk] for pk in sorted(rows)]

    def drain(self):
        changes, self.changes = self.changes, []
        return changes

    # ---- persistence ------------------------------------------------------------------------
    def save(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        for name, spec in TABLES.items():
            write_rows(directory / f"{name}.csv", spec, self.rows(name))

    @classmethod
    def load(cls, directory):
        db = cls()
        for name, spec in TABLES.items():
            for row in read_rows(Path(directory) / f"{name}.csv", spec):
                db.insert(name, row, log=False)
        return db


def write_rows(path, spec, rows):
    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(spec.col_names)
        for row in rows:
            writer.writerow([format_value(row[c.name], c.type) for c in spec.cols])


def read_rows(path, spec):
    with Path(path).open(newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            yield {c.name: parse_value(raw[c.name], c.type) for c in spec.cols}
