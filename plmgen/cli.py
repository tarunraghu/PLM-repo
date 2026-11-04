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


def _log_withdrawn(world, path):
    """Append parts deleted since the last call to the truth log, so a target's missing rows can be checked."""
    gone, world.withdrawn = world.withdrawn, []
    if gone:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with Path(path).open("a", encoding="utf-8", newline="\n") as fh:
            for record in gone:
                fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    return len(gone)


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
    emitter = Emitter(ws.out, manifest, cfg.seed, args.dirty, args.micro_batch_minutes)
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
        _log_withdrawn(world, ws.truth / "withdrawn_parts.jsonl")
        if len(pending) >= DOC_CHUNK and not args.no_docs:
            _write_doc_jobs(pending, base_docs, cfg, args.dirty, facts_file, totals)
            pending = []
        if n % 365 == 0:
            _say(f"  simulated through {day} ({db.count('master_part')} parts)")
    if pending and not args.no_docs:
        _write_doc_jobs(pending, base_docs, cfg, args.dirty, facts_file, totals)

    issues, _ = write_base(tables_from_db(db), cfg, ws.out / "base", args.dirty, args.base_part_rows)
    ws.save(db, cfg, manifest, world)
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
    emitter = Emitter(ws.out, manifest, cfg.seed, args.dirty, args.micro_batch_minutes)
    facts_file = ws.truth / "doc_facts.jsonl"
    saved_activity = cfg.activity
    if args.activity:
        cfg.activity = args.activity
    days, events, doc_total = [], 0, 0
    for day in daterange(first, last):
        world.step_day(day)
        changes = db.drain()
        jobs, world.doc_jobs = world.doc_jobs, []
        _log_withdrawn(world, ws.truth / "withdrawn_parts.jsonl")
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
    ws.save(db, cfg, manifest, world)
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
            json.loads(next((ws.out / "base").glob("_base_*.json")).read_text())["as_of"])
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


def cmd_drip(args):
    from .replay import drip
    rounds = drip(args.source, args.target, args.interval, args.pattern, args.max_ticks)
    print(json.dumps({"rounds": len(rounds), "files_delivered": sum(len(r) for r in rounds),
                      "files_per_round": [len(r) for r in rounds], "target": str(args.target)}, indent=2))
    return 0


def _lb_tables(text):
    from .lakebase import LIFECYCLE_TABLES
    if text == "lifecycle":
        return list(LIFECYCLE_TABLES)
    if text == "all":
        return list(schema_mod.TABLES)
    names = [t.strip() for t in text.split(",") if t.strip()]
    unknown = [t for t in names if t not in schema_mod.TABLES]
    if unknown:
        raise SystemExit(f"unknown tables: {', '.join(unknown)}")
    return names


def _lb_totals(summaries):
    keys = ("events", "upserts", "deletes", "duplicates", "corrupt")
    return {"windows": len(summaries), **{k: sum(s[k] for s in summaries) for k in keys}}


def cmd_lb_init(args):
    from .lakebase import Target, create_tables
    target = Target(args.url, args.schema)
    tables = _lb_tables(args.tables)
    create_tables(target, tables)
    target.close()
    print(json.dumps({"created": tables, "target": args.url.split("@")[-1]}, indent=2))
    return 0


def cmd_lb_load(args):
    from .lakebase import Target, create_tables, load_base
    ws = Workspace(args.workspace)
    if not ws.exists():
        _say(f"No workspace at {ws.root}.")
        return 2
    target = Target(args.url, args.schema)
    tables = _lb_tables(args.tables)
    create_tables(target, tables)
    loaded = load_base(target, tables, ws.out / "base")
    target.close()
    print(json.dumps({"loaded_rows": loaded}, indent=2))
    return 0


def cmd_lb_apply(args):
    from .lakebase import Target, apply_batches, create_tables
    ws = Workspace(args.workspace)
    if not ws.exists():
        _say(f"No workspace at {ws.root}.")
        return 2
    target = Target(args.url, args.schema)
    tables = _lb_tables(args.tables)
    create_tables(target, tables)
    summaries = apply_batches(target, ws.out / "batches", tables, args.first, args.last)
    target.close()
    print(json.dumps(_lb_totals(summaries), indent=2))
    return 0


def cmd_lb_watch(args):
    from .lakebase import Target, create_tables, lag_summary, watch
    target = Target(args.url, args.schema)
    tables = _lb_tables(args.tables)
    create_tables(target, tables)
    summaries = watch(target, args.landing, tables, args.interval, args.idle_polls, args.max_seconds)
    lag = lag_summary(target)
    target.close()
    print(json.dumps({**_lb_totals(summaries), "lag": lag}, indent=2))
    return 0


def cmd_lb_verify(args):
    from .lakebase import Target, lag_summary, verify
    ws = Workspace(args.workspace)
    if not ws.exists():
        _say(f"No workspace at {ws.root}.")
        return 2
    target = Target(args.url, args.schema)
    report = verify(target, _lb_tables(args.tables), ws.state, ws.truth / "withdrawn_parts.jsonl")
    report["lag"] = lag_summary(target)
    target.close()
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["problems"] == 0 else 1


def cmd_release(args):
    from .replay import release_batch
    delivered = release_batch(args.batch, args.target, args.interval, args.retime, args.max_windows)
    print(json.dumps({"windows": len(delivered), "files": sum(len(f) for _, f in delivered),
                      "windows_delivered": [w for w, _ in delivered], "retimed": bool(args.retime)}, indent=2))
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
    i.add_argument("--base-part-rows", type=int, default=2000,
                   help="rows per part file in the base extract (default 2000); 0 writes one file per table")
    i.add_argument("--dirty", action="store_true", help="inject known problems into the delivered data")
    i.add_argument("--no-docs", action="store_true", help="skip writing document files")
    i.add_argument("--history-events", action="store_true", help="also write the whole history as daily event batches")
    i.add_argument("--micro-batch-minutes", type=int, default=0,
                   help="with --history-events, split each day's events into windows of this many minutes")
    i.add_argument("--force", action="store_true", help="replace an existing workspace")
    i.set_defaults(func=cmd_init)

    a = sub.add_parser("advance", help="simulate more days and write one batch per day")
    a.add_argument("--workspace", default="data/plm")
    a.add_argument("--days", type=int, default=1)
    a.add_argument("--activity", type=float, help="activity multiplier for this run only (a burst)")
    a.add_argument("--dirty", action="store_true")
    a.add_argument("--no-docs", action="store_true")
    a.add_argument("--micro-batch-minutes", type=int, default=0,
                   help="split each day's events into one file per window of this many minutes (0: one file per day)")
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

    d = sub.add_parser("drip", help="deliver the base extract's part files in rounds, to load it incrementally")
    d.add_argument("--source", required=True, help="a folder of part files, for example data/plm/out/base")
    d.add_argument("--target", required=True, help="landing folder to deliver into")
    d.add_argument("--interval", type=float, default=0.0, help="seconds between rounds (default: no pause)")
    d.add_argument("--pattern", default="*.csv", help="file name pattern to deliver")
    d.add_argument("--max-ticks", type=int, help="stop after this many rounds")
    d.set_defaults(func=cmd_drip)

    rl = sub.add_parser("release", help="deliver one batch window by window, as a micro-batch feed would")
    rl.add_argument("--batch", required=True, help="a batch folder, for example data/plm/out/batches/batch=2026-11-04")
    rl.add_argument("--target", required=True, help="landing folder; files land under <target>/<batch folder name>/")
    rl.add_argument("--interval", type=float, default=0.0, help="seconds between windows (default: no pause)")
    rl.add_argument("--retime", action="store_true",
                    help="restamp events to the moment of release, so a consumer can measure its own lag")
    rl.add_argument("--max-windows", type=int)
    rl.set_defaults(func=cmd_release)

    lb = sub.add_parser("lakebase", help="apply micro-batches to a Postgres-compatible target and verify it")
    lbs = lb.add_subparsers(dest="action", required=True)

    def lb_common(parser, workspace=True):
        parser.add_argument("--url", required=True,
                            help="sqlite:///file.db or postgresql://user:password@host:5432/database")
        parser.add_argument("--schema", default="plm_mirror", help="PostgreSQL schema for the mirror tables")
        parser.add_argument("--tables", default="lifecycle",
                            help="'lifecycle' (default), 'all', or a comma-separated list of table names")
        if workspace:
            parser.add_argument("--workspace", default="data/plm")

    x = lbs.add_parser("init", help="create the mirror tables")
    lb_common(x, workspace=False)
    x.set_defaults(func=cmd_lb_init)
    x = lbs.add_parser("load", help="load the base extract into the mirror tables")
    lb_common(x)
    x.set_defaults(func=cmd_lb_load)
    x = lbs.add_parser("apply", help="apply the workspace's batches, one transaction per micro-batch window")
    lb_common(x)
    x.add_argument("--first", help="first batch date to apply (YYYY-MM-DD)")
    x.add_argument("--last", help="last batch date to apply")
    x.set_defaults(func=cmd_lb_apply)
    x = lbs.add_parser("watch", help="apply windows as they land in a folder (see the release command)")
    lb_common(x, workspace=False)
    x.add_argument("--landing", required=True)
    x.add_argument("--interval", type=float, default=1.0, help="seconds between polls")
    x.add_argument("--idle-polls", type=int, default=3, help="stop after this many polls with nothing new")
    x.add_argument("--max-seconds", type=float, default=600)
    x.set_defaults(func=cmd_lb_watch)
    x = lbs.add_parser("verify", help="compare the mirror with the truth, including deleted parts")
    lb_common(x)
    x.set_defaults(func=cmd_lb_verify)

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
