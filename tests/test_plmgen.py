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
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plmgen import cli, docs, schema
from plmgen.api import make_server
from plmgen.config import Config
from plmgen.db import Database
from plmgen.engine import Workspace, simulate_days, simulate_history
from plmgen.outputs import Emitter, inject_snapshot_problems, write_base
from plmgen.questions import build_questions
from plmgen.replay import apply_cdc, read_events, rows_to_json_state
from plmgen.util import idx_from_bom_key, pn_of, rev_index, rev_label
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
                db_b, _ = simulate_history(part)
                with tempfile.TemporaryDirectory() as tmp:
                    ws = Workspace(tmp)
                    ws.save(db_b, part, {})
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


if __name__ == "__main__":
    unittest.main()
