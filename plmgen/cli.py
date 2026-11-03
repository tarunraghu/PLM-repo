"""Command line: init, advance, validate, status, schema, questions, serve, replay."""

import argparse
import json
import shutil
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

from . import schema as schema_mod
from .config import PROFILES, Config
from .db import Database
from .docs import write_documents
from .engine import Workspace, daterange
from .outputs import Emitter, write_base
from .questions import build_questions, read_doc_facts, write_questions
from .validate import tables_from_dir, tables_from_db, validate
from .world import World

DOC_CHUNK = 500


def _say(message):
    print(message, file=sys.stderr)


def _write_doc_jobs(jobs, target, cfg, dirty, facts_file, totals):
    summary = write_documents(jobs, target, cfg.seed, dirty, facts_file)
    for key, value in summary.items():
        totals[key] += value
    return summary


def _refresh_truth(ws, db, cfg):
    facts = read_doc_facts(ws.truth / "doc_facts.jsonl")
    questions = build_questions(db, cfg, facts)
    write_questions(ws.truth / "questions.jsonl", questions)
    return len(questions)


# ---- init ---------------------------------------------------------------------------------------

def cmd_init(args):
    ws = Workspace(args.workspace)
    if ws.exists() and not args.force:
        _say(f"{ws.root} already holds a workspace. Use --force to replace it, or `advance` to extend it.")
        return 2
    cfg = Config.from_profile(
        args.profile, seed=args.seed, as_of=args.as_of, start=args.start, history_days=args.history_days,
        parts=args.parts, suppliers=args.suppliers, changes=args.changes, documents=args.documents,
        assemblies_ratio=args.assemblies_ratio, bom_depth=args.bom_depth, min_children=args.min_children,
        max_children=args.max_children, activity=args.activity, scan_share=args.scan_share)
    if args.start is None and args.history_days is not None:
        cfg.start = (cfg.as_of_date - timedelta(days=cfg.history_days)).isoformat()
    problems = cfg.validate()
    if problems:
        _say("Invalid settings: " + "; ".join(problems))
        return 2
    for part in ("state", "out", "truth"):
        shutil.rmtree(ws.root / part, ignore_errors=True)

    db = Database()
    world = World(db, cfg)
    world.bootstrap(cfg.start_date)
    manifest = {"lsn": {}, "event_seq": 0, "history": {"events": bool(args.history_events), "dirty": bool(args.dirty)}}
    emitter = Emitter(ws.out, manifest, cfg.seed, args.dirty)
    base_docs = ws.out / "base" / "project_docs"
    facts_file = ws.truth / "doc_facts.jsonl"
    totals, pending = Counter(), []
    for n, day in enumerate(daterange(cfg.start_date, cfg.as_of_date), 1):
        world.step_day(day)
        changes = db.drain()
        if args.history_events:
            emitter.emit_day(day, changes, folder=ws.out / "history" / f"batch={day.isoformat()}")
        pending += world.doc_jobs
        world.doc_jobs = []
        if len(pending) >= DOC_CHUNK and not args.no_docs:
            _write_doc_jobs(pending, base_docs, cfg, args.dirty, facts_file, totals)
            pending = []
        if n % 365 == 0:
            _say(f"  simulated through {day} ({db.count('master_part')} parts)")
    if pending and not args.no_docs:
        _write_doc_jobs(pending, base_docs, cfg, args.dirty, facts_file, totals)

    issues, _ = write_base(tables_from_db(db), cfg, ws.out / "base", args.dirty)
    ws.save(db, cfg, manifest)
    n_questions = _refresh_truth(ws, db, cfg)
    report = validate(tables_from_db(db), cfg.as_of_date, cfg.bom_depth)
    result = {"workspace": str(ws.root), "as_of": cfg.as_of, "profile": cfg.profile, "dirty": bool(args.dirty),
              "rows": {n: db.count(n) for n in schema_mod.TABLES if db.count(n)},
              "documents": dict(totals), "injected_into_base": issues, "questions": n_questions,
              "state_violations": report["total_violations"]}
    print(json.dumps(result, indent=2))
    return 0 if report["total_violations"] == 0 else 1


# ---- advance ------------------------------------------------------------------------------------

def cmd_advance(args):
    ws = Workspace(args.workspace)
    if not ws.exists():
        _say(f"No workspace at {ws.root}. Run `init` first.")
        return 2
    db, world, cfg, manifest = ws.load()
    first = cfg.as_of_date + timedelta(days=1)
    last = cfg.as_of_date + timedelta(days=args.days)
    emitter = Emitter(ws.out, manifest, cfg.seed, args.dirty)
    facts_file = ws.truth / "doc_facts.jsonl"
    saved_activity = cfg.activity
    if args.activity:
        cfg.activity = args.activity
    days, events, doc_total = [], 0, 0
    for day in daterange(first, last):
        world.step_day(day)
        changes = db.drain()
        jobs, world.doc_jobs = world.doc_jobs, []
        folder = ws.out / "batches" / f"batch={day.isoformat()}"
        extra = {}
        if jobs and not args.no_docs:
            extra["documents"] = write_documents(jobs, folder / "project_docs", cfg.seed, args.dirty, facts_file)
            doc_total += len(jobs)
        summary = emitter.emit_day(day, changes, extra=extra)
        events += summary["changes"]
        days.append(day.isoformat())
    cfg.activity = saved_activity
    cfg.as_of = last.isoformat()
    manifest.setdefault("runs", []).append({"from": first.isoformat(), "to": last.isoformat(), "dirty": bool(args.dirty),
                                            "activity": args.activity or saved_activity})
    ws.save(db, cfg, manifest)
    n_questions = _refresh_truth(ws, db, cfg)
    report = validate(tables_from_db(db), cfg.as_of_date, cfg.bom_depth)
    print(json.dumps({"advanced": {"from": first.isoformat(), "to": last.isoformat(), "days": len(days)},
                      "change_records": events, "new_documents": doc_total, "as_of": cfg.as_of,
                      "questions": n_questions, "state_violations": report["total_violations"]}, indent=2))
    return 0 if report["total_violations"] == 0 else 1


# ---- the rest -----------------------------------------------------------------------------------

def cmd_validate(args):
    if args.dir:
        directory, as_of = Path(args.dir), args.as_of
        if not as_of:
            _say("--as-of is required with --dir")
            return 2
        from datetime import date
        as_of_date, max_depth = date.fromisoformat(as_of), args.max_depth
    else:
        ws = Workspace(args.workspace)
        if not ws.exists():
            _say(f"No workspace at {ws.root}.")
            return 2
        manifest = ws.read_manifest()
        cfg = Config.from_json(manifest["config"])
        directory = ws.out / "base" if args.base else ws.state
        as_of_date = date_from(manifest["config"]["as_of"]) if not args.base else date_from(
            json.loads((ws.out / "base" / "_base.json").read_text())["as_of"])
        max_depth = cfg.bom_depth
    report = validate(tables_from_dir(directory), as_of_date, max_depth)
    print(json.dumps(report, indent=2))
    return 0 if report["total_violations"] == 0 or args.allow_violations else 1


def date_from(text):
    from datetime import date
    return date.fromisoformat(text)


def cmd_status(args):
    ws = Workspace(args.workspace)
    if not ws.exists():
        _say(f"No workspace at {ws.root}.")
        return 2
    manifest = ws.read_manifest()
    cfg = manifest["config"]
    batches = sorted((ws.out / "batches").glob("batch=*")) if (ws.out / "batches").exists() else []
    questions = (ws.truth / "questions.jsonl")
    print(json.dumps({
        "workspace": str(ws.root), "profile": cfg["profile"], "seed": cfg["seed"], "start": cfg["start"],
        "as_of": cfg["as_of"], "batches": len(batches),
        "first_batch": batches[0].name if batches else None, "last_batch": batches[-1].name if batches else None,
        "runs": manifest.get("runs", []), "event_lsn": manifest.get("lsn", {}),
        "questions": sum(1 for _ in questions.open()) if questions.exists() else 0}, indent=2))
    return 0


def cmd_schema(args):
    if args.format == "ddl-spark":
        text = schema_mod.ddl("spark")
    elif args.format == "ddl-postgres":
        text = schema_mod.ddl("postgres")
    elif args.format == "dictionary":
        text = schema_mod.dictionary_markdown()
    else:
        text = json.dumps({name: schema_mod.spark_schema_json(name) for name in schema_mod.TABLES}, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        _say(f"wrote {args.out}")
    else:
        print(text)
    return 0


def cmd_questions(args):
    ws = Workspace(args.workspace)
    if not ws.exists():
        _say(f"No workspace at {ws.root}.")
        return 2
    db, _, cfg, _ = ws.load()
    questions = build_questions(db, cfg, read_doc_facts(ws.truth / "doc_facts.jsonl"), args.per_category)
    write_questions(ws.truth / "questions.jsonl", questions)
    print(json.dumps({"questions": len(questions), "by_category": dict(Counter(q["category"] for q in questions))}, indent=2))
    return 0


def cmd_serve(args):
    from .api import make_server
    ws = Workspace(args.workspace)
    if not ws.exists():
        _say(f"No workspace at {ws.root}.")
        return 2
    server = make_server(ws.root, args.host, args.port, args.token, args.throttle_every, args.error_every,
                         args.latency_ms, args.page_size, verbose=True)
    _say(f"Mock Windchill API on http://{args.host}:{args.port}/odata/ProdMgmt/Parts  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_replay(args):
    from .replay import stream_files
    files = stream_files(args.source, args.target, args.events_per_file, args.rate)
    print(json.dumps({"files_written": len(files), "target": str(args.target)}, indent=2))
    return 0


# ---- argument parsing ----------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="plmgen", description="Synthetic PLM data generator for Databricks hands-on work.")
    sub = p.add_subparsers(dest="command", required=True)

    i = sub.add_parser("init", help="create a workspace and simulate the initial history")
    i.add_argument("--workspace", default="data/plm")
    i.add_argument("--profile", choices=sorted(PROFILES), default="small")
    i.add_argument("--seed", type=int)
    i.add_argument("--as-of", help="last simulated day, ISO date (default 2026-11-03)")
    i.add_argument("--start", help="first simulated day (default: as-of minus history-days)")
    i.add_argument("--history-days", type=int)
    i.add_argument("--parts", type=int, help="target number of parts by the end of the history")
    i.add_argument("--suppliers", type=int)
    i.add_argument("--changes", type=int)
    i.add_argument("--documents", type=int)
    i.add_argument("--assemblies-ratio", type=float)
    i.add_argument("--bom-depth", type=int)
    i.add_argument("--min-children", type=int)
    i.add_argument("--max-children", type=int)
    i.add_argument("--activity", type=float, help="multiplier on arrival rates")
    i.add_argument("--scan-share", type=float, help="share of documents that are scanned images (default 0.10; slow)")
    i.add_argument("--dirty", action="store_true", help="inject known problems into the delivered data")
    i.add_argument("--no-docs", action="store_true", help="skip writing document files")
    i.add_argument("--history-events", action="store_true", help="also write the whole history as daily event batches")
    i.add_argument("--force", action="store_true", help="replace an existing workspace")
    i.set_defaults(func=cmd_init)

    a = sub.add_parser("advance", help="simulate more days and write one batch per day")
    a.add_argument("--workspace", default="data/plm")
    a.add_argument("--days", type=int, default=1)
    a.add_argument("--activity", type=float, help="activity multiplier for this run only (a burst)")
    a.add_argument("--dirty", action="store_true")
    a.add_argument("--no-docs", action="store_true")
    a.set_defaults(func=cmd_advance)

    v = sub.add_parser("validate", help="check keys, time order and business rules")
    v.add_argument("--workspace", default="data/plm")
    v.add_argument("--base", action="store_true", help="validate the delivered base extract instead of the truth")
    v.add_argument("--dir", help="validate any folder of table CSVs (flat or one folder per source)")
    v.add_argument("--as-of", help="required with --dir")
    v.add_argument("--max-depth", type=int)
    v.add_argument("--allow-violations", action="store_true", help="exit 0 even when violations are found")
    v.set_defaults(func=cmd_validate)

    s = sub.add_parser("status", help="show the workspace summary")
    s.add_argument("--workspace", default="data/plm")
    s.set_defaults(func=cmd_status)

    sc = sub.add_parser("schema", help="emit DDL, Spark schemas or the data dictionary")
    sc.add_argument("--format", choices=["ddl-spark", "ddl-postgres", "spark-json", "dictionary"], default="dictionary")
    sc.add_argument("--out")
    sc.set_defaults(func=cmd_schema)

    q = sub.add_parser("questions", help="regenerate ground-truth questions")
    q.add_argument("--workspace", default="data/plm")
    q.add_argument("--per-category", type=int, default=12)
    q.set_defaults(func=cmd_questions)

    sv = sub.add_parser("serve", help="run the mock Windchill REST/OData API")
    sv.add_argument("--workspace", default="data/plm")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8080)
    sv.add_argument("--token", help="require this bearer token")
    sv.add_argument("--throttle-every", type=int, default=0, help="answer 429 on every Nth request")
    sv.add_argument("--error-every", type=int, default=0, help="answer 503 on every Nth request")
    sv.add_argument("--latency-ms", type=int, default=0)
    sv.add_argument("--page-size", type=int, default=100)
    sv.set_defaults(func=cmd_serve)

    r = sub.add_parser("replay", help="release an events file in small files over time, to feed a streaming job")
    r.add_argument("--source", required=True, help="a JSONL events file, for example from out/batches/")
    r.add_argument("--target", required=True, help="folder to drop the small files into")
    r.add_argument("--events-per-file", type=int, default=50)
    r.add_argument("--rate", type=float, help="events per second (default: as fast as possible)")
    r.set_defaults(func=cmd_replay)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
