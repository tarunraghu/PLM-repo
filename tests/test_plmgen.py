"""Tests for the PLM generator. Run with:  python -m unittest discover -s tests -v

The central properties:
  * clean data has zero violations
  * incremental advance equals a one-shot run, byte for byte
  * streamed CDC events replay to exactly the true state, even when dirty
  * the validator catches each kind of corruption
"""

import copy
import hashlib
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plmgen import cli, docs, schema
from plmgen.api import make_server
from plmgen.config import Config
from plmgen.db import Database, read_rows
from plmgen.engine import Workspace, simulate_days, simulate_history
from plmgen.lakebase import (LIFECYCLE_TABLES, Target, apply_records, create_tables, lag_summary, verify, watch)
from plmgen.outputs import Emitter, inject_snapshot_problems, write_base
from plmgen.questions import build_questions
from plmgen.replay import apply_cdc, drip, read_events, release_batch, rows_to_json_state
from plmgen.util import idx_from_bom_key, pn_idx as pn_idx_of, pn_of, rev_index, rev_label
from plmgen.validate import tables_from_db, tables_from_dir, validate


def digest(directory):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(directory).glob("*.csv"))}


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TinyWorld(unittest.TestCase):
    """Shared tiny simulation, built once."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = Config.from_profile("tiny")
        cls.db, cls.world = simulate_history(cls.cfg)
        cls.tables = tables_from_db(cls.db)


class SchemaTests(unittest.TestCase):
    def test_every_foreign_key_points_at_a_real_column(self):
        for name, table in schema.TABLES.items():
            for col, target in table.fks.items():
                self.assertIn(col, table.col_names, name)
                if not target.startswith("xref:"):
                    ref_table, ref_col = target.split(".")
                    self.assertIn(ref_col, schema.TABLES[ref_table].col_names, f"{name}.{col}")

    def test_ddl_covers_every_table_in_both_dialects(self):
        for dialect in ("spark", "postgres"):
            text = schema.ddl(dialect)
            for name in schema.TABLES:
                self.assertIn(f"CREATE TABLE {name} (", text)

    def test_dependency_order_puts_parents_first(self):
        order = schema.dependency_order()
        for name, table in schema.TABLES.items():
            for target in table.fks.values():
                if not target.startswith("xref:") and target.split(".")[0] != name:
                    self.assertLess(order.index(target.split(".")[0]), order.index(name))

    def test_revision_labels_round_trip(self):
        for k in (0, 1, 25, 26, 27, 701, 702):
            self.assertEqual(rev_index(rev_label(k)), k)


class LakebaseDdlTests(unittest.TestCase):
    def test_ddl_parses_and_creates_tables_before_they_are_referenced(self):
        try:
            import pglast
        except ImportError:
            self.skipTest("pglast not installed")
        sql = (Path(__file__).resolve().parent.parent / "sql" / "lakebase" / "001_schema.sql").read_text(encoding="utf-8")
        created, problems = [], []
        for statement in pglast.parse_sql(sql):
            node = statement.stmt
            if type(node).__name__ != "CreateStmt":
                continue
            name = f"{node.relation.schemaname}.{node.relation.relname}"
            for element in node.tableElts or ():
                constraints = [element] if type(element).__name__ == "Constraint" else list(getattr(element, "constraints", None) or ())
                for c in constraints:
                    if getattr(c, "pktable", None) is not None:
                        ref = f"{c.pktable.schemaname}.{c.pktable.relname}"
                        if ref != name and ref not in created:
                            problems.append((name, ref))
            created.append(name)
        self.assertEqual(problems, [])
        self.assertGreaterEqual(len(created), 20)

    def test_generated_spark_ddl_matches_the_extract_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.from_profile("tiny")
            db, _ = simulate_history(cfg)
            write_base(tables_from_db(db), cfg, Path(tmp), dirty=False)
            for name, spec in schema.TABLES.items():
                header = (Path(tmp) / spec.source / f"{name}.csv").read_text().splitlines()[0].split(",")
                self.assertEqual(header, spec.col_names, name)


class DatabaseTests(unittest.TestCase):
    def test_changes_are_captured_and_types_enforced(self):
        db = Database()
        db.insert("master_uom", {"uom_code": "EA", "uom_name": "Each", "dimension": "count"})
        db.update("master_uom", "EA", uom_name="Piece")
        db.delete("master_uom", "EA")
        self.assertEqual([c.op for c in db.changes], ["c", "u", "d"])
        self.assertEqual(db.changes[1].before["uom_name"], "Each")
        with self.assertRaises(TypeError):
            db.insert("master_uom", {"uom_code": 1, "uom_name": "x", "dimension": "y"})
        with self.assertRaises(ValueError):
            db.insert("master_uom", {"uom_code": "X", "uom_name": None, "dimension": "y"})

    def test_noop_update_logs_nothing(self):
        db = Database()
        db.insert("master_uom", {"uom_code": "EA", "uom_name": "Each", "dimension": "count"})
        db.drain()
        db.update("master_uom", "EA", uom_name="Each")
        self.assertEqual(db.changes, [])

    def test_save_and_load_round_trip(self):
        cfg = Config.from_profile("tiny")
        db, _ = simulate_history(cfg)
        with tempfile.TemporaryDirectory() as tmp:
            db.save(tmp)
            loaded = Database.load(tmp)
            loaded.save(Path(tmp) / "again")
            self.assertEqual(digest(tmp), digest(Path(tmp) / "again"))


class SimulationTests(TinyWorld):
    def test_clean_history_has_no_violations(self):
        report = validate(self.tables, self.cfg.as_of_date, self.cfg.bom_depth)
        self.assertEqual(report["total_violations"], 0, report)

    def test_history_is_realistic_enough_to_test_against(self):
        states = {p["canonical_lifecycle"] for p in self.tables["master_part"]}
        self.assertEqual(states, set(schema.CANONICAL))
        self.assertTrue(any(c["state"] == "Implemented" for c in self.tables["windchill_change_notice"]))
        self.assertTrue(any(r["effective_to"] is not None for r in self.tables["bom_mgmt_ebom_line"]))
        self.assertTrue(any(d["status"] == "Applied" for d in self.tables["windchill_change_bom_delta"]))
        self.assertGreater(len(self.tables["windchill_part_supersession"]), 0)

    def test_same_seed_gives_identical_data(self):
        db2, _ = simulate_history(Config.from_profile("tiny"))
        with tempfile.TemporaryDirectory() as tmp:
            self.db.save(Path(tmp) / "a")
            db2.save(Path(tmp) / "b")
            self.assertEqual(digest(Path(tmp) / "a"), digest(Path(tmp) / "b"))

    def test_different_seeds_differ(self):
        db2, _ = simulate_history(Config.from_profile("tiny", seed=99))
        self.assertNotEqual([p["part_name"] for p in db2.rows("master_part")][:20],
                            [p["part_name"] for p in self.db.rows("master_part")][:20])

    def test_incremental_advance_equals_one_shot(self):
        for extra, seed in ((1, 42), (30, 42), (200, 7)):
            with self.subTest(extra_days=extra, seed=seed):
                full = Config.from_profile("tiny", seed=seed)
                full.as_of = (full.as_of_date + timedelta(days=extra)).isoformat()
                db_a, _ = simulate_history(full)
                part = Config.from_json({**full.to_json(), "as_of": (full.as_of_date - timedelta(days=extra)).isoformat()})
                db_b, world_b = simulate_history(part)
                with tempfile.TemporaryDirectory() as tmp:
                    ws = Workspace(tmp)
                    ws.save(db_b, part, {}, world_b)
                    db_c, world_c, _, _ = ws.load()
                    simulate_days(world_c, part.as_of_date + timedelta(days=1), full.as_of_date)
                    db_a.save(Path(tmp) / "a")
                    db_c.save(Path(tmp) / "c")
                    self.assertEqual(digest(Path(tmp) / "a"), digest(Path(tmp) / "c"))
                    self.assertEqual(validate(tables_from_db(db_c), full.as_of_date, full.bom_depth)["total_violations"], 0)

    def test_configuration_problems_are_reported(self):
        for override in ({"parts": 10}, {"suppliers": 1}, {"bom_depth": 1}, {"history_days": 100}):
            cfg = Config.from_profile("tiny", **override)
            self.assertTrue(cfg.validate(), override)


class ValidatorTests(TinyWorld):
    def detects(self, mutate):
        tables = copy.deepcopy(self.tables)
        mutate(tables)
        return validate(tables, self.cfg.as_of_date, self.cfg.bom_depth)["total_violations"] > 0

    def first(self, tables, name, predicate=lambda r: True):
        return next(r for r in tables[name] if predicate(r))

    def test_catches_each_kind_of_corruption(self):
        def state_changed(t):
            self.first(t, "master_part", lambda p: p["canonical_lifecycle"] == "Released")["canonical_lifecycle"] = "Design"

        def future_date(t):
            t["master_part"][0]["created_date"] = date(2099, 1, 1)

        def cycle(t):
            a = self.first(t, "bom_mgmt_ebom_line", lambda l: l["effective_to"] is None)
            t["bom_mgmt_ebom_line"].append({**a, "ebom_line_id": "EBOM-9999999",
                                            "parent_item_key": a["child_item_key"], "child_item_key": a["parent_item_key"]})

        def orphan(t):
            t["bom_mgmt_ebom_line"].append({**t["bom_mgmt_ebom_line"][0], "ebom_line_id": "EBOM-9999998",
                                            "child_item_key": "BM-9000001"})

        def change_before_part(t):
            t["windchill_change_notice"][0]["created_on"] = date(2000, 1, 1)

        def drop_release(t):
            t["part_release_release"].pop(0)

        def doc_class(t):
            d = t["project_docs_document"][0]
            d["export_control_class"] = "ITAR-USML-XIX" if d["export_control_class"] == "EAR99" else "EAR99"

        def revision_gap(t):
            self.first(t, "windchill_part_revision", lambda r: not r["is_current"])["effective_to"] = date(2001, 1, 1)

        def history_moved(t):
            t["master_part_lifecycle_history"][1]["effective_from"] = date(2000, 1, 1)

        def duplicate_key(t):
            t["master_part"].append(dict(t["master_part"][0]))

        def delta_flipped(t):
            self.first(t, "windchill_change_bom_delta", lambda d: d["delta_type"] == "ADDED")["delta_type"] = "REMOVED"

        def supersession_removed(t):
            t["windchill_part_supersession"].pop(0)

        for mutate in (state_changed, future_date, cycle, orphan, change_before_part, drop_release, doc_class,
                       revision_gap, history_moved, duplicate_key, delta_flipped, supersession_removed):
            with self.subTest(mutation=mutate.__name__):
                self.assertTrue(self.detects(mutate))

    def test_grams_with_a_unit_are_valid_data_not_a_violation(self):
        def grams(t):
            m = t["sap_pdm_material"][0]
            m["mass_value"] = round(m["mass_value"] * 1000, 6)
            m["gewei"] = "G"
        self.assertFalse(self.detects(grams))


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config.from_profile("tiny")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_base_extract_is_valid_when_read_back(self):
        db, _ = simulate_history(self.cfg)
        write_base(tables_from_db(db), self.cfg, self.root / "base", dirty=False)
        report = validate(tables_from_dir(self.root / "base"), self.cfg.as_of_date, self.cfg.bom_depth)
        self.assertEqual(report["total_violations"], 0, report)

    def test_part_files_reassemble_to_exactly_the_single_file_extract(self):
        db, _ = simulate_history(self.cfg)
        tables = tables_from_db(db)
        write_base(tables, self.cfg, self.root / "single", part_rows=0)
        write_base(tables, self.cfg, self.root / "parts", part_rows=50)
        single, parts = tables_from_dir(self.root / "single"), tables_from_dir(self.root / "parts")
        meta = json.loads((self.root / "parts" / "_base.json").read_text())
        for name, spec in schema.TABLES.items():
            key = lambda r: tuple(str(r[c]) for c in spec.pk)
            self.assertEqual(sorted(single[name], key=key), sorted(parts[name], key=key), name)
            files = sorted((self.root / "parts" / spec.source / name).glob("part-*.csv"))
            self.assertEqual(len(files), meta["parts"][name], name)
            self.assertEqual([f.name for f in files], [f"part-{n:05d}.csv" for n in range(1, len(files) + 1)])
            for f in files:
                lines = f.read_text().splitlines()
                self.assertEqual(lines[0].split(","), spec.col_names)
                self.assertLessEqual(len(lines) - 1, 50)
        self.assertGreater(meta["parts"]["master_part_xref"], 5)

    def test_parts_are_ordered_oldest_first(self):
        db, _ = simulate_history(self.cfg)
        write_base(tables_from_db(db), self.cfg, self.root / "parts", part_rows=40)
        rows = tables_from_dir(self.root / "parts")      # concatenated in part order
        for table, column in (("master_part", "created_date"), ("windchill_part", "created_on"),
                              ("windchill_change_notice", "created_on"), ("project_docs_document", "uploaded_on"),
                              ("master_part_lifecycle_history", "effective_from"),
                              ("windchill_part_revision", "effective_from")):
            values = [r[column] for r in rows[table]]
            self.assertEqual(values, sorted(values), table)

    def test_part_layout_validates_clean_and_dirty(self):
        db, _ = simulate_history(self.cfg)
        tables = tables_from_db(db)
        write_base(tables, self.cfg, self.root / "clean", part_rows=60)
        self.assertEqual(validate(tables_from_dir(self.root / "clean"), self.cfg.as_of_date)["total_violations"], 0)
        write_base(tables, self.cfg, self.root / "dirty", dirty=True, part_rows=60)
        self.assertGreater(validate(tables_from_dir(self.root / "dirty"), self.cfg.as_of_date)["total_violations"], 0)

    def test_drip_delivers_one_part_of_every_table_per_round(self):
        db, _ = simulate_history(self.cfg)
        write_base(tables_from_db(db), self.cfg, self.root / "base", part_rows=100)
        landing = self.root / "landing"
        rounds = drip(self.root / "base", landing, max_ticks=1)
        self.assertEqual(len(rounds), 1)
        self.assertTrue(all(p.name == "part-00001.csv" for p in rounds[0]))
        self.assertEqual(len(rounds[0]), len(schema.TABLES))
        everything = drip(self.root / "base", self.root / "all")
        self.assertGreater(len(everything), 3)
        self.assertEqual(sorted(p.relative_to(self.root / "all") for r in everything for p in r),
                         sorted(p.relative_to(self.root / "base") for p in (self.root / "base").rglob("part-*.csv")))
        # Loading round by round only ever adds rows, and the last round has everything.
        counts = []
        for n in range(1, len(everything) + 1):
            loaded = sum(len(list(read_rows(p, schema.TABLES[p.parent.name])))
                         for r in everything[:n] for p in r)
            counts.append(loaded)
        self.assertEqual(counts, sorted(counts))
        self.assertEqual(counts[-1], sum(len(rows) for rows in tables_from_db(db).values()))

    def test_dirty_base_extract_contains_exactly_what_was_injected(self):
        db, _ = simulate_history(self.cfg)
        issues, _ = write_base(tables_from_db(db), self.cfg, self.root / "base", dirty=True)
        report = validate(tables_from_dir(self.root / "base"), self.cfg.as_of_date, self.cfg.bom_depth)
        fk = report["foreign_key"]
        s = report["structural"]
        self.assertEqual(fk.get("bom_mgmt_ebom_line.child_item_key -> xref:bom_mgmt"), issues["orphan_ebom_lines"])
        self.assertEqual(s.get("active ebom lines whose child lifecycle the parent does not allow"),
                         issues["lifecycle_violating_bom_lines"])
        # Time-travelling changes also fail the "not Released on that date" check, so this is a lower bound.
        self.assertGreaterEqual(s.get("change affected items on parts that were not Released on the change date", 0),
                                issues["changes_on_unreleased_parts"])
        self.assertGreater(issues["time_travel_changes"], 0)
        self.assertGreater(s.get("changes dated before an affected part exists", 0), 0)
        self.assertGreater(issues["mass_in_grams_sap"], 0)
        self.assertGreater(report["total_violations"], 0)

    def test_dirty_data_never_touches_the_truth(self):
        db, _ = simulate_history(self.cfg)
        before = tables_from_db(db)
        inject_snapshot_problems(before, self.cfg)
        self.assertEqual(validate(tables_from_db(db), self.cfg.as_of_date, self.cfg.bom_depth)["total_violations"], 0)

    def replay_matches_truth(self, dirty):
        db, world = simulate_history(self.cfg)
        world.cfg.activity = 8.0     # busier days, so daily files are large enough to be corrupted
        state = {t: rows_to_json_state(t, db.rows(t)) for t in ("sap_pdm_material", "sap_pdm_vendor")}
        emitter = Emitter(self.root, {}, self.cfg.seed, dirty)
        first = self.cfg.as_of_date + timedelta(days=1)
        simulate_days(world, first, first + timedelta(days=60), on_day=lambda d, ch: emitter.emit_day(d, ch))
        files = sorted(self.root.glob("batches/batch=*/sap_pdm/cdc/events.jsonl"))
        ignored = apply_cdc(state, read_events(files))
        truth = {t: rows_to_json_state(t, db.rows(t)) for t in ("sap_pdm_material", "sap_pdm_vendor")}
        self.assertEqual(state, truth)
        return ignored, files

    def test_streamed_cdc_replays_to_the_true_state(self):
        ignored, files = self.replay_matches_truth(dirty=False)
        self.assertGreater(len(files), 0)
        self.assertEqual(ignored, {"duplicates": 0, "corrupt": 0})

    def test_dirty_cdc_still_replays_to_the_true_state(self):
        ignored, _ = self.replay_matches_truth(dirty=True)
        self.assertGreater(ignored["duplicates"], 0)
        self.assertGreater(ignored["corrupt"], 0)

    def test_every_source_is_delivered_in_its_own_shape(self):
        db, world = simulate_history(self.cfg)
        emitter = Emitter(self.root, {}, self.cfg.seed)
        first = self.cfg.as_of_date + timedelta(days=1)
        simulate_days(world, first, first + timedelta(days=90), on_day=lambda d, ch: emitter.emit_day(d, ch))
        kinds = {f["kind"] for p in self.root.glob("batches/*/_batch.json") for f in json.loads(p.read_text())["files"]}
        self.assertEqual(kinds, {"cdc", "events", "changelog", "delta"})
        events = [json.loads(line) for p in self.root.glob("batches/*/part_release/events/events.jsonl")
                  for line in p.read_text().splitlines()]
        self.assertTrue({"TaskAssigned", "TaskDecided"} <= {e["event_type"] for e in events})
        delta = next(self.root.glob("batches/*/bom_mgmt/delta/bom_mgmt_ebom_line.csv")).read_text().splitlines()[0]
        self.assertTrue(delta.startswith("_op,_seq,_ts,ebom_line_id"))

    def test_lsn_continues_across_runs(self):
        db, world = simulate_history(self.cfg)
        manifest = {}
        emitter = Emitter(self.root, manifest, self.cfg.seed)
        first = self.cfg.as_of_date + timedelta(days=1)
        simulate_days(world, first, first + timedelta(days=10), on_day=lambda d, ch: emitter.emit_day(d, ch))
        lsns = [json.loads(line)["lsn"] for p in sorted(self.root.glob("batches/*/sap_pdm/cdc/events.jsonl"))
                for line in p.read_text().splitlines()]
        self.assertEqual(lsns, list(range(1, len(lsns) + 1)))
        self.assertEqual(manifest["lsn"]["sap_pdm"], len(lsns))


class DocumentTests(TinyWorld):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_documents_state_the_facts_they_claim(self):
        facts_file = self.root / "facts.jsonl"
        docs.write_documents(self.world.doc_jobs, self.root, self.cfg.seed, truth_file=facts_file)
        facts = [json.loads(line) for line in facts_file.read_text().splitlines()]
        self.assertEqual(len(facts), len(self.world.doc_jobs))
        checked = 0
        for f in facts:
            path = self.root / "files" / f["file_name"]
            if f["format"] == "pdf":
                body = path.read_bytes().decode("latin-1")
            elif f["format"] == "docx":
                body = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8")
            else:
                continue
            for p in f["parts"]:
                for needle in (p["part_number"], p["revision"]):
                    self.assertIn(needle, body, f["document_id"])
                checked += 1
        self.assertGreater(checked, 50)

    def test_files_are_reproducible_and_metadata_has_a_checksum(self):
        docs.write_documents(self.world.doc_jobs, self.root / "a", self.cfg.seed)
        docs.write_documents(self.world.doc_jobs, self.root / "b", self.cfg.seed)
        for p in (self.root / "a" / "files").iterdir():
            self.assertEqual(p.read_bytes(), (self.root / "b" / "files" / p.name).read_bytes())
        meta_files = list((self.root / "a" / "metadata").iterdir())
        self.assertEqual(len(meta_files), 1)
        records = [json.loads(line) for line in meta_files[0].read_text().splitlines()]
        self.assertEqual(len(records), len(self.world.doc_jobs))
        for meta in records[:20]:
            data = (self.root / "a" / "files" / meta["file_name"]).read_bytes()
            self.assertEqual(meta["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(meta["bytes"], len(data))

    def test_all_three_formats_are_produced(self):
        docs.write_documents(self.world.doc_jobs, self.root, self.cfg.seed)
        suffixes = {p.suffix for p in (self.root / "files").iterdir()}
        self.assertEqual(suffixes, {".pdf", ".docx", ".png"})

    def test_real_parsers_accept_the_files_when_available(self):
        try:
            from pypdf import PdfReader
            import docx as docx_lib
        except ImportError:
            self.skipTest("pypdf / python-docx not installed")
        docs.write_documents(self.world.doc_jobs, self.root, self.cfg.seed)
        for p in sorted((self.root / "files").iterdir()):
            if p.suffix == ".pdf":
                self.assertGreater(len("".join(page.extract_text() for page in PdfReader(str(p)).pages)), 50)
            elif p.suffix == ".docx":
                self.assertGreater(len(docx_lib.Document(str(p)).paragraphs), 3)

    def test_dirty_documents_are_flagged_in_the_facts(self):
        pdf_jobs = [dict(j) for j in self.world.doc_jobs if j["format"] == "pdf"]
        jobs = [dict(j) for j in pdf_jobs * 8]   # enough volume that every defect appears
        for i, job in enumerate(jobs):
            job["document_id"] = f"DOC-{i + 1:07d}"
        facts_file = self.root / "facts.jsonl"
        summary = docs.write_documents(jobs, self.root, self.cfg.seed, dirty=True, truth_file=facts_file)
        defects = [json.loads(line)["defect"] for line in facts_file.read_text().splitlines()]
        self.assertEqual(defects.count("missing_file"), summary["missing_file"])
        self.assertEqual(defects.count("corrupt_file"), summary["corrupt_file"])
        self.assertGreater(summary["missing_file"] + summary["corrupt_file"] + summary["wrong_part_in_text"], 0)
        files = {p.name for p in (self.root / "files").iterdir()}
        self.assertEqual(sum(1 for f in files if f.startswith("ORPHAN")), summary["orphan_files"])


class QuestionTests(TinyWorld):
    def test_answers_agree_with_the_data(self):
        questions = build_questions(self.db, self.cfg)
        self.assertGreater(len(questions), 100)
        self.assertEqual(len({q["id"] for q in questions}), len(questions))
        parts = {p["part_number"]: p for p in self.tables["master_part"]}
        checked = 0
        for q in questions:
            if q["category"] == "part_lifecycle":
                pn = q["question"].split("part ")[1].rstrip("?")
                self.assertEqual(q["answer"], parts[pn]["canonical_lifecycle"])
                checked += 1
            if q["category"] == "where_used":
                pn = q["question"].split("part ")[1].split(" in the")[0]
                expected = sorted(pn_of(idx_from_bom_key(l["parent_item_key"])) for l in self.tables["bom_mgmt_ebom_line"]
                                  if l["effective_to"] is None and pn_of(idx_from_bom_key(l["child_item_key"])) == pn)
                self.assertEqual(q["answer"], expected)
                checked += 1
        self.assertGreater(checked, 10)

    def test_questions_are_reproducible(self):
        a = build_questions(self.db, self.cfg)
        b = build_questions(self.db, self.cfg)
        self.assertEqual(a, b)


class ApiTests(TinyWorld):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        Workspace(self.tmp.name).save(self.db, self.cfg, {})
        self.server = make_server(self.tmp.name, port=0, token="t", throttle_every=5, page_size=40)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def get(self, path, token="t"):
        while True:
            request = urllib.request.Request(self.base + path, headers={"Authorization": f"Bearer {token}"} if token else {})
            try:
                with urllib.request.urlopen(request) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    continue
                return e.code, json.loads(e.read() or b"{}")

    def test_paging_follows_next_link_without_gaps_or_repeats(self):
        rows, link = [], "/odata/ProdMgmt/Parts"
        while link:
            status, body = self.get(link)
            self.assertEqual(status, 200)
            rows += body["value"]
            link = body.get("@odata.nextLink")
        self.assertEqual(sorted(r["wt_id"] for r in rows), sorted(p["wt_id"] for p in self.tables["windchill_part"]))

    def test_watermark_returns_only_newer_rows_in_order(self):
        stamps = sorted(p["modified_at"] for p in self.tables["windchill_part"])
        since = stamps[len(stamps) // 2].isoformat() + "Z"
        status, body = self.get("/odata/ProdMgmt/Parts?$top=1000&$filter=" + urllib.request.quote(f"ModifyStamp gt {since}"))
        self.assertEqual(status, 200)
        got = [r["modified_at"] for r in body["value"]]
        self.assertTrue(all(g > since for g in got))
        self.assertEqual(got, sorted(got))

    def test_auth_and_errors(self):
        self.assertEqual(self.get("/odata/ProdMgmt/Parts", token=None)[0], 401)
        self.assertEqual(self.get("/odata/ProdMgmt/Nope")[0], 404)
        self.assertEqual(self.get("/odata/ProdMgmt/Parts?$filter=nonsense")[0], 400)


class CliTests(unittest.TestCase):
    def test_init_advance_validate_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = str(Path(tmp) / "ws")
            code, out, _ = run_cli("init", "--profile", "tiny", "--workspace", ws)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["state_violations"], 0)
            code, out, _ = run_cli("advance", "--workspace", ws, "--days", "7")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["advanced"]["days"], 7)
            self.assertEqual(run_cli("validate", "--workspace", ws)[0], 0)
            self.assertEqual(run_cli("validate", "--workspace", ws, "--base")[0], 0)
            status = json.loads(run_cli("status", "--workspace", ws)[1])
            self.assertEqual(status["batches"], 7)
            self.assertEqual(status["as_of"], "2026-11-10")
            self.assertTrue((Path(ws) / "truth" / "questions.jsonl").exists())

    def test_init_splits_the_base_extract_into_parts_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            self.assertEqual(run_cli("init", "--profile", "tiny", "--workspace", str(ws), "--no-docs",
                                     "--base-part-rows", "100")[0], 0)
            self.assertTrue((ws / "out" / "base" / "master" / "master_part" / "part-00001.csv").exists())
            self.assertFalse((ws / "out" / "base" / "master" / "master_part.csv").exists())
            self.assertEqual(run_cli("validate", "--workspace", str(ws), "--base")[0], 0)
            code, out, _ = run_cli("drip", "--source", str(ws / "out" / "base"), "--target", str(ws / "landing"),
                                   "--max-ticks", "2")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["rounds"], 2)
            self.assertTrue((ws / "landing" / "master" / "master_part" / "part-00002.csv").exists())
            # One file per table is still available.
            ws2 = Path(tmp) / "ws2"
            self.assertEqual(run_cli("init", "--profile", "tiny", "--workspace", str(ws2), "--no-docs",
                                     "--base-part-rows", "0")[0], 0)
            self.assertTrue((ws2 / "out" / "base" / "master" / "master_part.csv").exists())
            self.assertEqual(run_cli("validate", "--workspace", str(ws2), "--base")[0], 0)

    def test_existing_workspace_is_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = str(Path(tmp) / "ws")
            self.assertEqual(run_cli("init", "--profile", "tiny", "--workspace", ws)[0], 0)
            code, _, err = run_cli("init", "--profile", "tiny", "--workspace", ws)
            self.assertEqual(code, 2)
            self.assertIn("--force", err)
            self.assertEqual(run_cli("init", "--profile", "tiny", "--workspace", ws, "--force")[0], 0)

    def test_bad_settings_are_rejected_with_a_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = run_cli("init", "--profile", "tiny", "--workspace", tmp, "--suppliers", "1")
            self.assertEqual(code, 2)
            self.assertIn("suppliers", err)

    def test_advance_needs_a_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run_cli("advance", "--workspace", tmp)[0], 2)

    def test_schema_command_emits_each_format(self):
        for fmt in ("ddl-spark", "ddl-postgres", "dictionary", "spark-json"):
            code, out, _ = run_cli("schema", "--format", fmt)
            self.assertEqual(code, 0)
            self.assertIn("master_part", out)

    def test_dirty_run_leaves_truth_clean_but_delivers_problems(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = str(Path(tmp) / "ws")
            self.assertEqual(run_cli("init", "--profile", "tiny", "--workspace", ws, "--dirty")[0], 0)
            self.assertEqual(run_cli("validate", "--workspace", ws)[0], 0)
            self.assertEqual(run_cli("validate", "--workspace", ws, "--base")[0], 1)
            self.assertEqual(run_cli("validate", "--workspace", ws, "--base", "--allow-violations")[0], 0)


class LifecycleChangeTests(unittest.TestCase):
    """Real deletes and corrections to the product lifecycle, and a realistic intraday clock."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = Config.from_profile("small")
        cls.db, cls.world = simulate_history(cls.cfg)
        cls.tables = tables_from_db(cls.db)

    def test_withdrawn_parts_are_gone_from_every_table(self):
        gone = [w["part_number"] for w in self.world.withdrawn]
        self.assertGreater(len(gone), 50)
        wts = {w["wt_id"] for w in self.world.withdrawn}
        for table, spec in schema.TABLES.items():
            for row in self.tables[table]:
                for col in spec.col_names:
                    value = row[col]
                    self.assertNotIn(value, gone, f"{table}.{col}")
                    if col in ("wt_id", "old_wt_id", "new_wt_id"):
                        self.assertNotIn(value, wts, f"{table}.{col}")
        self.assertEqual(validate(self.tables, self.cfg.as_of_date, self.cfg.bom_depth)["total_violations"], 0)

    def test_only_unused_design_parts_are_withdrawn(self):
        for w in self.world.withdrawn:
            self.assertEqual(w["state"], "Design")

    def test_review_can_send_a_part_back_to_design(self):
        history = {}
        for h in self.tables["master_part_lifecycle_history"]:
            history.setdefault(h["part_number"], []).append(h)
        reverts = 0
        for rows in history.values():
            rows.sort(key=lambda h: h["effective_from"])
            reverts += sum(1 for a, b in zip(rows, rows[1:])
                           if (a["lifecycle_state"], b["lifecycle_state"]) == ("In Review", "Design"))
        self.assertGreater(reverts, 20)

    def test_deleted_part_numbers_are_never_reused(self):
        cfg = Config.from_profile("tiny")
        db, world = simulate_history(cfg)
        day = cfg.as_of_date + timedelta(days=1)
        db.begin_day(day, world.erng(day, "clock"))
        world.create_part(day)
        newest = world.parts_order[-1]
        world.withdraw_part(newest, day)
        issued = world.counters["part"]
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(tmp)
            ws.save(db, cfg, {}, world)
            _, reloaded, _, _ = ws.load()
        self.assertEqual(reloaded.counters["part"], issued)
        day2 = day + timedelta(days=1)
        reloaded.db.begin_day(day2, reloaded.erng(day2, "clock"))
        reloaded.create_part(day2)
        self.assertEqual(pn_idx_of(reloaded.parts_order[-1]), issued + 1)

    def test_events_are_spread_across_the_working_day(self):
        cfg = Config.from_profile("small")
        db, world = simulate_history(cfg, on_day=None)
        busy, hours = 0, set()
        day = cfg.as_of_date
        for offset in range(1, 61):
            world.step_day(day + timedelta(days=offset))
            changes = world.db.drain()
            hours.update(c.ts.hour for c in changes)
            if len(changes) > 5 and (changes[-1].ts - changes[0].ts) > timedelta(hours=4):
                busy += 1
        self.assertGreaterEqual(len(hours), 12)
        self.assertGreater(busy, 30)

    def test_micro_batch_files_hold_exactly_the_days_events(self):
        cfg = Config.from_profile("tiny")
        db, world = simulate_history(cfg)
        world.cfg.activity = 8.0
        with tempfile.TemporaryDirectory() as tmp:
            daily, windows = Emitter(Path(tmp) / "d", {}, cfg.seed), Emitter(Path(tmp) / "w", {}, cfg.seed, micro_minutes=15)
            first = cfg.as_of_date + timedelta(days=1)
            captured = []
            simulate_days(world, first, first + timedelta(days=10), on_day=lambda d, ch: captured.append((d, ch)))
            for day, changes in captured:
                daily.emit_day(day, changes)
                windows.emit_day(day, changes)

            def event_ids(root, pattern):
                return sorted(json.loads(line)["event_id"] for p in Path(root).glob(pattern)
                              for line in p.read_text().splitlines())

            self.assertEqual(event_ids(Path(tmp) / "d", "batches/*/sap_pdm/cdc/*.jsonl"),
                             event_ids(Path(tmp) / "w", "batches/*/sap_pdm/cdc/*.jsonl"))
            for p in (Path(tmp) / "w").glob("batches/*/sap_pdm/cdc/events-*.jsonl"):
                start = int(p.stem.split("-")[1][:2]) * 60 + int(p.stem.split("-")[1][2:])
                for line in p.read_text().splitlines():
                    ts = datetime.fromisoformat(json.loads(line)["ts"].rstrip("Z"))
                    self.assertTrue(start <= ts.hour * 60 + ts.minute < start + 15, p.name)
            many = list((Path(tmp) / "w").glob("batches/*/sap_pdm/cdc/events-*.jsonl"))
            self.assertGreater(len(many), len(list((Path(tmp) / "d").glob("batches/*/sap_pdm/cdc/events.jsonl"))))


class LakebaseHarnessTests(unittest.TestCase):
    """Applying micro-batches to a Postgres-compatible target must reproduce the truth exactly."""

    def build(self, root, dirty, days=20, window=15, profile="tiny", activity=15):
        ws = str(Path(root) / "ws")
        for args in (("init", "--profile", profile, "--workspace", ws, "--no-docs") + (("--dirty",) if dirty else ()),
                     ("advance", "--workspace", ws, "--days", str(days), "--no-docs", "--micro-batch-minutes", str(window),
                      "--activity", str(activity)) + (("--dirty",) if dirty else ())):
            self.assertEqual(run_cli(*args)[0], 0)
        return ws

    def test_clean_and_dirty_micro_batches_converge_on_the_truth(self):
        for dirty in (False, True):
            with self.subTest(dirty=dirty), tempfile.TemporaryDirectory() as tmp:
                ws = self.build(tmp, dirty)
                url = f"sqlite:///{Path(tmp) / 't.db'}"
                self.assertEqual(run_cli("lakebase", "load", "--url", url, "--workspace", ws)[0], 0)
                code, out, _ = run_cli("lakebase", "apply", "--url", url, "--workspace", ws)
                totals = json.loads(out)
                self.assertEqual(code, 0)
                self.assertGreater(totals["deletes"], 0)
                if dirty:
                    self.assertGreater(totals["duplicates"], 0)
                    self.assertGreater(totals["corrupt"], 0)
                code, out, _ = run_cli("lakebase", "verify", "--url", url, "--workspace", ws)
                report = json.loads(out)
                self.assertEqual(code, 0, report)
                self.assertEqual(report["problems"], 0)
                self.assertGreater(report["withdrawn_parts"]["deleted_in_source"], 0)
                self.assertEqual(report["withdrawn_parts"]["still_present_in_target"], 0)

    def test_verify_notices_a_row_that_never_reflected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self.build(tmp, False, days=3, window=60)
            url = f"sqlite:///{Path(tmp) / 't.db'}"
            run_cli("lakebase", "load", "--url", url, "--workspace", ws)
            run_cli("lakebase", "apply", "--url", url, "--workspace", ws)
            target = Target(url)
            victim = target.execute("SELECT part_number FROM master_part LIMIT 1").fetchone()[0]
            target.execute("UPDATE master_part SET canonical_lifecycle = 'Obsolete' WHERE part_number = ?", [victim])
            target.execute("DELETE FROM windchill_part WHERE number = ?", [victim])
            target.commit()
            target.close()
            code, out, _ = run_cli("lakebase", "verify", "--url", url, "--workspace", ws)
            report = json.loads(out)
            self.assertEqual(code, 1)
            self.assertGreaterEqual(report["master_part"]["different"], 1)
            self.assertGreaterEqual(report["windchill_part"]["missing"], 1)

    def test_replaying_a_batch_twice_or_late_changes_nothing(self):
        cfg = Config.from_profile("tiny")
        db, _ = simulate_history(cfg)
        part = db.rows("master_part")[0]
        target = Target("sqlite:///:memory:")
        create_tables(target, ["master_part"])
        newer = {**part, "canonical_lifecycle": "Released"}
        older = {**part, "canonical_lifecycle": "Design"}
        when = datetime(2026, 11, 4, 9, 0)

        def record(row, lsn, op="u"):
            return {"table": "master_part", "op": op, "key": (part["part_number"],), "lsn": lsn, "ts": when,
                    "row": None if op == "d" else row, "uid": f"e{lsn}"}

        def stored():
            return target.execute("SELECT canonical_lifecycle, _lsn FROM master_part").fetchall()

        apply_records(target, [record(newer, 10)])
        apply_records(target, [record(newer, 10)])                 # the same event again
        apply_records(target, [record(older, 5)])                  # an older event arriving late
        self.assertEqual(stored(), [("Released", 10)])
        apply_records(target, [record(newer, 4, "d")])             # a stale delete must not remove the row
        self.assertEqual(stored(), [("Released", 10)])
        apply_records(target, [record(newer, 11, "d")])            # a current delete does
        self.assertEqual(stored(), [])

    def test_released_micro_batches_are_applied_as_they_land_and_lag_is_measured(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = self.build(tmp, False, days=3, window=60)
            url = f"sqlite:///{Path(tmp) / 't.db'}"
            run_cli("lakebase", "load", "--url", url, "--workspace", ws)
            batches = sorted((Path(ws) / "out" / "batches").glob("batch=*"))
            delivered = [release_batch(b, Path(tmp) / "landing", retime=True) for b in batches]
            self.assertGreater(sum(len(d) for d in delivered), 10)
            target = Target(url)
            summaries = watch(target, Path(tmp) / "landing", list(LIFECYCLE_TABLES), interval=0.02, idle_polls=2)
            self.assertGreater(len(summaries), 10)
            lag = lag_summary(target)
            self.assertLess(lag["apply_lag_seconds_oldest_event"]["max"], 30)
            self.assertGreaterEqual(lag["apply_lag_seconds_oldest_event"]["p50"], 0)
            self.assertEqual(verify(target, list(LIFECYCLE_TABLES), Path(ws) / "state")["problems"], 0)
            target.close()
            sample = next((Path(tmp) / "landing").rglob("cdc/events-*.jsonl")).read_text().splitlines()[0]
            event = json.loads(sample)
            self.assertIn("orig_ts", event)
            self.assertNotEqual(event["ts"], event["orig_ts"])

    def test_postgres_runs_the_lakebase_ddl_and_the_applier_when_available(self):
        try:
            import pgserver
            import psycopg
        except ImportError:
            self.skipTest("pgserver / psycopg not installed")
        with tempfile.TemporaryDirectory() as tmp:
            ws = self.build(tmp, True, days=7, window=30)
            server = pgserver.get_server(Path(tmp) / "pg", cleanup_mode="stop")
            try:
                uri = server.get_uri()
                with psycopg.connect(uri) as conn:       # the curated Lakebase schema must execute as written
                    conn.execute((Path(__file__).resolve().parent.parent / "sql" / "lakebase" / "001_schema.sql").read_text(encoding="utf-8"))
                    self.assertGreaterEqual(conn.execute("SELECT count(*) FROM information_schema.tables "
                                                         "WHERE table_schema = 'plm'").fetchone()[0], 20)
                self.assertEqual(run_cli("lakebase", "load", "--url", uri, "--workspace", ws)[0], 0)
                self.assertEqual(run_cli("lakebase", "apply", "--url", uri, "--workspace", ws)[0], 0)
                code, out, _ = run_cli("lakebase", "verify", "--url", uri, "--workspace", ws)
                self.assertEqual(code, 0, out)
            finally:
                server.cleanup()


if __name__ == "__main__":
    unittest.main()
