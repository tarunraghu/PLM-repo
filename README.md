# PLM Modern Data Architecture - POC

A hands-on proof of concept for the PLM target-state architecture on Databricks (reference: [Product Lifecycle Management Architecture.png](Product%20Lifecycle%20Management%20Architecture.png)). It is for learning and de-risking before a client project, not for demos.

The centrepiece is `plmgen`, a reusable generator of synthetic PLM data. It simulates years of business activity, then keeps going one day at a time, delivering the data the way each source system in the diagram would.

## Quick start

```bash
pip install -e .                                   # needs Python 3.10+ and Pillow
python -m plmgen init --profile small --workspace data/plm
python -m plmgen advance --workspace data/plm --days 1
python -m plmgen validate --workspace data/plm
```

`init` simulates four years of history (about 5,000 parts) and writes the initial extracts. Each `advance` adds days of activity and writes one batch per day. Run `advance` as often as you like to keep your pipelines fed.

| Profile | Parts | Time to `init` | Notes |
|---|---|---|---|
| `tiny` | 300 | about 10 s | for tests |
| `small` (default) | 5,000 | about 2 min (6 s with `--no-docs`) | most of the time is writing document files |
| `medium` | 50,000 | about 40 s with `--no-docs` | 670 MB of memory. Advancing 7 days takes about 14 s |
| `large` | 250,000 | not measured | expect several minutes and a few GB of memory |

Python 3.12 was used for development and testing.

## What you get

```
data/plm/
  state/                     the truth: one typed CSV per table, plus manifest.json
  out/base/<source>/<table>/ the initial extract, split into part-00001.csv, part-00002.csv, ... (2,000 rows each)
  out/base/project_docs/     document files (PDF, DOCX, scanned PNG) plus a metadata JSONL
  out/batches/batch=DATE/    one folder per advanced day (see below)
  truth/questions.jsonl      questions with known answers, for Genie and RAG benchmarks
  truth/doc_facts.jsonl      what each document says, for checking text extraction
```

Each source reaches the lakehouse the way the diagram describes:

| Source | Delivered as | Where, inside `out/batches/batch=DATE/` |
|---|---|---|
| SAP PDM | CDC stream, Debezium-style JSON lines | `sap_pdm/cdc/events.jsonl` |
| Part Release | workflow events (event mesh), JSON lines | `part_release/events/events.jsonl` |
| PTC Windchill | REST/OData API, plus a change log | `windchill/changelog/changes.jsonl` and `python -m plmgen serve` |
| BOM Management | batch extracts with `_op`, `_seq`, `_ts` | `bom_mgmt/delta/*.csv` |
| Master data | batch extracts | `master/delta/*.csv` |
| Project Docs | file drops with a metadata JSONL | `project_docs/files/` and `project_docs/metadata/` |

## Commands

| Command | Purpose |
|---|---|
| `init` | create a workspace and simulate the history (`--profile`, `--seed`, `--as-of`, `--dirty`, `--no-docs`) |
| `advance --days N` | simulate N more days, one batch per day (`--activity 3` makes a busy burst) |
| `validate` | check keys, time order and business rules on the truth, or on the delivered extract (`--base`) |
| `status` | summary of the workspace and its batches |
| `serve` | mock Windchill REST/OData API with paging, a modified-since filter, bearer auth, throttling and errors |
| `drip` | deliver the base extract's part files in rounds (one part of every table per round), to load it incrementally |
| `replay` | release a batch's events in small files at a chosen rate, to feed a streaming job |
| `questions` | regenerate the ground-truth question set |
| `schema` | emit Spark or Postgres DDL, Spark schemas, or the data dictionary |

Run any command with `--help`. Invalid settings are rejected with a message.

## Properties you can rely on

- **Referentially consistent.** Every key, date and business rule holds in the truth, and the validator proves it by reading the CSVs back. Joins work through the golden cross-reference tables.
- **Incremental equals one-shot.** History to day N then `advance` N+k days gives byte-identical data to simulating N+k days at once. This is tested.
- **Reproducible.** The same seed and settings always give the same data.
- **Dirty mode.** `--dirty` adds a fixed, recorded set of problems to what is *delivered*, never to the truth: duplicate and out-of-order events, corrupt lines, unexpected fields, orphan keys, lifecycle-breaking BOM lines, time-travelling changes, mass in grams, missing or truncated documents, orphan files. Every batch records what it injected in `_batch.json`. See [docs/usage-guide.md](docs/usage-guide.md).
- **A clean answer key.** Replaying the CDC stream with dedupe and ordering reproduces the true SAP state exactly, even in dirty mode.

## Documents

- [docs/usage-guide.md](docs/usage-guide.md): how to use each output for Databricks testing, scenario by scenario.
- [docs/data-model.md](docs/data-model.md): sources, keys, lifecycle, how the simulation behaves, and its limits.
- [docs/data-dictionary.md](docs/data-dictionary.md): every table and column (generated).
- [docs/gotchas-and-limitations.md](docs/gotchas-and-limitations.md): platform findings for Databricks, Lakebase and Genie.
- [sql/lakebase/001_schema.sql](sql/lakebase/001_schema.sql): the curated operational schema for Lakebase. It parses with a real PostgreSQL parser but has not been run on an instance yet.
- [sql/bronze/typed_tables.sql](sql/bronze/typed_tables.sql): generated Spark DDL that mirrors the extracts.

## Tests

```bash
pip install -e .[dev]
python -m unittest discover -s tests
```

45 tests, about 90 seconds. They cover the properties above, the validator against deliberate corruption, event replay, document content, the mock API, and the CLI.

## 3-week plan

**Week 1 - foundations and ingestion.** Land `out/base` in a Unity Catalog volume, load Bronze with typed schemas, build Silver, and validate against the truth.
**Week 2 - streaming and the operational layer.** Stream the SAP CDC, workflow events and BOM deltas, and handle the dirty cases. Create the Lakebase instance, apply the DDL, and test write-back and sync.
**Week 3 - documents, governance and answers.** Parse documents, apply Unity Catalog security using the export-control class, and benchmark Genie and any RAG agent against `truth/questions.jsonl`.
