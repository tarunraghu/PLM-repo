# PLM Modern Data Architecture - POC

A hands-on proof of concept for the PLM target-state architecture on Databricks (reference: [Product Lifecycle Management Architecture.png](Product%20Lifecycle%20Management%20Architecture.png)). It is for learning and de-risking before a client project, not for demos.

**What the POC has to prove:** data arrives in **micro-batches**, not as a continuous stream, yet **updates and deletes to the product lifecycle show up in Lakebase immediately** (within a window plus apply time) and Lakebase ends up exactly equal to the source. This repository contains a generator that produces the data to test that, and a harness that applies it to a Postgres-compatible target and checks the result.

The centrepiece is `plmgen`, a reusable generator of synthetic PLM data. It simulates years of business activity, then keeps going one day at a time, delivering the data the way each source system in the diagram would.

## Quick start

```bash
pip install -e .                                   # needs Python 3.10+ and Pillow
python -m plmgen init --profile small --workspace data/plm
python -m plmgen advance --workspace data/plm --days 1 --micro-batch-minutes 15
python -m plmgen validate --workspace data/plm
```

`init` simulates four years of history (about 5,000 parts) and writes the initial extracts. Each `advance` adds days of activity and writes one batch per day, optionally split into micro-batch windows. Run `advance` as often as you like to keep your pipelines fed.

| Profile | Parts | Time to `init` | Notes |
|---|---|---|---|
| `tiny` | 300 | about 10 s | for tests |
| `small` (default) | 5,000 | about 2 min (6 s with `--no-docs`) | most of the time is writing document files |
| `medium` | 50,000 | about 40 s with `--no-docs` | 670 MB of memory. Advancing 7 days takes about 14 s |
| `large` | 250,000 | not measured | expect several minutes and a few GB of memory |

Python 3.12 was used for development and testing.

## Proving immediate lifecycle updates and deletes on Lakebase

```bash
pip install -e .[lakebase]                          # adds the PostgreSQL driver
URL="postgresql://USER:TOKEN@HOST:5432/DATABASE?sslmode=require"      # or sqlite:///local.db to rehearse

python -m plmgen lakebase load   --url $URL --workspace data/plm      # base extract
python -m plmgen advance --workspace data/plm --days 3 --micro-batch-minutes 15
python -m plmgen release --batch data/plm/out/batches/batch=2026-11-04 --target data/plm/landing --interval 5 --retime &
python -m plmgen lakebase watch  --url $URL --landing data/plm/landing   # applies each window as it lands
python -m plmgen lakebase verify --url $URL --workspace data/plm         # target == truth? deleted parts gone? lag?
```

The lifecycle data includes real **updates** (a part moves Design, In Review, Released, Obsolete, or is sent back from review to Design) and real **deletes** (a cancelled part is removed from every source, with its dependants). `verify` compares the target with the truth row by row, checks that each deleted part is really absent, and reports lag from the apply log. See [docs/usage-guide.md](docs/usage-guide.md) for how to read the result.

Tested here against SQLite and a local PostgreSQL 16 server. **Not yet run against a Lakebase instance.**

## What you get

```
data/plm/
  state/                     the truth: one typed CSV per table, plus manifest.json
  out/base/<source>/<table>/ the initial extract, split into part-00001.csv, part-00002.csv, ... (2,000 rows each)
  out/base/project_docs/     document files (PDF, DOCX, scanned PNG) plus a metadata JSONL
  out/batches/batch=DATE/    one folder per advanced day (see below)
  truth/questions.jsonl      questions with known answers, for Genie and RAG benchmarks
  truth/doc_facts.jsonl      what each document says, for checking text extraction
  truth/withdrawn_parts.jsonl  every part deleted from the sources, to check a target really removed it
```

Each source reaches the lakehouse the way the diagram describes:

| Source | Delivered as | Where, inside `out/batches/batch=DATE/` |
|---|---|---|
| SAP PDM | CDC events, Debezium-style JSON lines | `sap_pdm/cdc/events[-HHMM].jsonl` |
| Part Release | workflow events, JSON lines | `part_release/events/events[-HHMM].jsonl` |
| PTC Windchill | REST/OData API, plus a change log | `windchill/changelog/changes[-HHMM].jsonl` and `python -m plmgen serve` |
| BOM Management | batch extracts with `_op`, `_seq`, `_ts` | `bom_mgmt/delta/*[-HHMM].csv` |
| Master data | batch extracts | `master/delta/*[-HHMM].csv` |
| Project Docs | file drops with a metadata JSONL | `project_docs/files/` and `project_docs/metadata/` |

With `--micro-batch-minutes 15`, each source's day is split into one file per 15-minute window (`-0615` means 06:15 to 06:30), and events are timestamped across the working day. Without it there is one file per source per day.

## Commands

| Command | Purpose |
|---|---|
| `init` | create a workspace and simulate the history (`--profile`, `--seed`, `--as-of`, `--dirty`, `--no-docs`) |
| `advance --days N` | simulate N more days, one batch per day (`--micro-batch-minutes`, `--activity 3` for a busy burst) |
| `validate` | check keys, time order and business rules on the truth, or on the delivered extract (`--base`) |
| `status` | summary of the workspace and its batches |
| `release` | deliver one batch window by window, optionally restamped to the moment of release (`--retime`) |
| `lakebase init \| load \| apply \| watch \| verify` | apply micro-batches to a Postgres-compatible target and check it against the truth |
| `drip` | deliver the base extract's part files in rounds, to load it incrementally |
| `serve` | mock Windchill REST/OData API with paging, a modified-since filter, bearer auth, throttling and errors |
| `replay` | release an events file in small files at a chosen rate |
| `questions` | regenerate the ground-truth question set |
| `schema` | emit Spark or Postgres DDL, Spark schemas, or the data dictionary |

Run any command with `--help`. Invalid settings are rejected with a message.

## Properties you can rely on

- **Referentially consistent.** Every key, date and business rule holds in the truth, and the validator proves it by reading the CSVs back. Joins work through the golden cross-reference tables.
- **Incremental equals one-shot.** History to day N then `advance` N+k days gives byte-identical data to simulating N+k days at once, including after deletes. This is tested.
- **Reproducible.** The same seed and settings always give the same data.
- **Deletes are real.** Deleted keys are never reused, because the workspace saves its ID counters.
- **Dirty mode.** `--dirty` adds a fixed, recorded set of problems to what is *delivered*, never to the truth: duplicate and out-of-order events, corrupt lines, unexpected fields, orphan keys, lifecycle-breaking BOM lines, time-travelling changes, mass in grams, missing or truncated documents, orphan files. Every batch records what it injected in `_batch.json`.
- **A clean answer key.** Applying the micro-batches, with duplicates dropped and corrupt lines skipped, reproduces the true state exactly, even in dirty mode.

## Documents

- [docs/usage-guide.md](docs/usage-guide.md): how to use each output for Databricks testing, scenario by scenario.
- [docs/data-model.md](docs/data-model.md): sources, keys, lifecycle, how the simulation behaves, and its limits.
- [docs/data-dictionary.md](docs/data-dictionary.md): every table and column (generated).
- [docs/gotchas-and-limitations.md](docs/gotchas-and-limitations.md): platform findings for Databricks, Lakebase and Genie.
- [sql/lakebase/001_schema.sql](sql/lakebase/001_schema.sql): the curated operational schema for Lakebase. It executes cleanly on PostgreSQL 16 (21 tables, 29 indexes) but has not been run on a Lakebase instance.
- [sql/bronze/typed_tables.sql](sql/bronze/typed_tables.sql): generated Spark DDL that mirrors the extracts.

## Tests

```bash
pip install -e .[dev]
python -m unittest discover -s tests
```

56 tests, about 3 minutes. They cover the properties above, the validator against deliberate corruption, event replay, document content, the mock API, the CLI, and the Lakebase harness on SQLite and on a real local PostgreSQL (skipped when `pgserver` is not installed).

## 3-week plan

**Week 1 - foundations and ingestion.** Land `out/base` in a Unity Catalog volume, load Bronze with typed schemas, build Silver, and validate against the truth.
**Week 2 - micro-batches and Lakebase.** Ingest the daily micro-batches into Bronze and Silver, then apply the lifecycle changes to Lakebase and prove updates and deletes reflect and match the source. Measure the lag.
**Week 3 - documents, governance and answers.** Parse documents, apply Unity Catalog security using the export-control class, and benchmark Genie and any RAG agent against `truth/questions.jsonl`.
