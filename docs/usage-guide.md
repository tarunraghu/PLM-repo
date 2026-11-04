# Usage guide

How to use each output to exercise a part of the architecture. Commands are shown for a workspace in `data/plm`.

Statements about Databricks features come from the platform documentation as of November 2026. Anything marked **untested** or **verify** has not been run against a Databricks workspace from here. The Lakebase harness has been run on SQLite and on a local PostgreSQL 16 server, not on Lakebase itself.

## The daily loop

```bash
python -m plmgen advance --workspace data/plm --days 1 --micro-batch-minutes 15   # a new day of activity
python -m plmgen validate --workspace data/plm                                    # the truth is still clean
```

Then land `out/batches/batch=DATE/` in your workspace, run your pipelines, and compare their results with `state/` and `truth/`. Use `--dirty` on `init` and `advance` to add problems to the delivered data, and `--activity 3` to simulate a burst.

To get files into a Unity Catalog volume you can use the Databricks CLI, for example `databricks fs cp --recursive data/plm/out/base dbfs:/Volumes/<catalog>/<schema>/<volume>/base` (**untested**).

## Scenarios

### 1. Bronze batch load, all at once or in parts

Each table in `out/base/<source>/<table>/` is split into numbered files (`part-00001.csv`, `part-00002.csv`, ...) of 2,000 rows each (`init --base-part-rows N` changes that, and `0` writes one file per table). Every part has a header, and rows run oldest first. The CSVs are plain text, so declare types: `python -m plmgen schema --format ddl-spark` gives Spark DDL, and `--format spark-json` gives a schema per table. Empty fields are nulls.

To load incrementally, let the parts arrive over time in a landing folder:

```bash
python -m plmgen drip --source data/plm/out/base --target data/plm/landing --interval 30
```

Round 1 delivers `part-00001.csv` of every table, round 2 delivers `part-00002.csv`, and so on. Files appear atomically, so a reader that tracks loaded files, such as Auto Loader or `COPY INTO`, picks up only the new ones. A part load is not a change feed: the parts are slices of one snapshot. For real changes, use the daily batches.

Check: row counts across all parts equal `row_counts` in `out/base/_base.json`. Partway through a drip, foreign keys may be unresolved because a child can arrive before its parent.

### 2. Micro-batch ingestion

With `advance --micro-batch-minutes N`, each day's changes are split into one file per window, named by the window start: `events-0615.jsonl` covers 06:15 up to the next window. Events are timestamped across the working day, so windows are meaningful. Empty windows produce no file.

| What | Where | Key fields |
|---|---|---|
| SAP CDC | `sap_pdm/cdc/events-HHMM.jsonl` | `event_id`, `lsn`, `table`, `op` (`c`, `u`, `d`), `key`, `before`, `after`, `ts` |
| Workflow events | `part_release/events/events-HHMM.jsonl` | `event_type`, `sequence`, `aggregate_id`, `payload` |
| Windchill change log | `windchill/changelog/changes-HHMM.jsonl` | `entity`, `op`, `lsn`, `modified_at`, `key`, `data` |
| BOM and master deltas | `bom_mgmt/delta/<table>-HHMM.csv`, `master/delta/...` | `_op`, `_seq`, `_ts`, then the table's columns |

To feed a job as the windows would arrive, release a batch window by window:

```bash
python -m plmgen release --batch data/plm/out/batches/batch=2026-11-04 --target data/plm/landing --interval 5
```

Run your ingestion as a micro-batch job, for example an Auto Loader stream triggered on an interval or with an `availableNow` trigger (**verify** the options on your runtime). Whatever you use, the rules that make the result correct are the same, and `plmgen/lakebase.py` shows them in working code:

1. **Skip corrupt lines.** A bad line must not fail the batch.
2. **Drop duplicates** by `event_id` (or `_seq` for delta files).
3. **Apply in time order**, using `lsn` or `_seq` to break ties.
4. **Upsert only if newer.** Store the last applied `lsn` on each row and ignore older events.
5. **Delete only if not newer.** A delete removes the row only when the stored `lsn` is lower.

Check: the target equals `state/` after you apply every window. The tests do exactly this.

### 3. Proving that lifecycle updates and deletes reflect on Lakebase

**What is being proved.** A micro-batch feed is not instantaneous, so "immediate" has to be defined. Treat the end-to-end delay of a change as: the window length (a change waits for its window to close), plus delivery, plus the apply time. The harness measures the apply part; you choose the window. Agree a target with your stakeholders, for example "visible within two minutes", then show it.

**The lifecycle changes in the data:**

| Change | What the source emits |
|---|---|
| A part changes state (Design, In Review, Released, Obsolete) | `u` on `sap_pdm_material` (`estat`), `u` on `windchill_part` (`state`), new row in `master_part_lifecycle_history` |
| A review sends a part back to Design | the same, with the history row going from In Review back to Design |
| A part is cancelled and deleted | `d` on `sap_pdm_material`, `windchill_part`, `windchill_part_revision`, `master_part` and its history, with child rows removed first |
| A SAP material is deleted and recreated | `d` then `c` on `sap_pdm_material` |
| An implemented change releases a revision | new `windchill_part_revision` row, `u` on `windchill_part.version` |

Every deleted part is listed in `truth/withdrawn_parts.jsonl`.

**Steps (replace the URL to point at Lakebase):**

```bash
URL=sqlite:///data/lakebase_rehearsal.db        # rehearse locally first
python -m plmgen lakebase load   --url $URL --workspace data/plm     # base extract into mirror tables
python -m plmgen advance --workspace data/plm --days 1 --micro-batch-minutes 5 --activity 5
python -m plmgen release --batch data/plm/out/batches/batch=2026-11-04 --target data/plm/landing --interval 2 --retime &
python -m plmgen lakebase watch  --url $URL --landing data/plm/landing
python -m plmgen lakebase verify --url $URL --workspace data/plm
```

- `--retime` restamps each event to the moment of release, so the lag the applier reports is its own apply latency, not the age of the simulated history.
- `watch` applies each window as soon as its files appear, one transaction per window.
- `verify` compares every mirror table with the truth, confirms each deleted part is gone, and prints lag percentiles taken from the apply log. It exits non-zero if anything differs.
- For the lakehouse side, the target's lag to Delta is separate. Lakehouse Sync replicates Lakebase tables to Delta as history (**verify** how it represents deletes, and its latency).

**Pass criteria:** `problems` is 0, `still_present_in_target` is 0, and the lag is inside the target you agreed.

**What the harness does and does not cover:**

- It applies to mirror tables that follow the source tables. Mapping them to your curated operational schema (`sql/lakebase/001_schema.sql`) is the real project work.
- Lakebase credentials are short-lived OAuth tokens (one hour, per the documentation), so a long watch needs a refreshed URL. The harness does not refresh it.
- A delete followed by a late, older update for the same key is not guarded against, because no tombstones are kept. The generator does not produce that case.
- The lag figures from the local rehearsal are not a Lakebase measurement. Take those against a real instance.

### 4. REST / OData pull from Windchill

```bash
python -m plmgen serve --workspace data/plm --port 8080 --token secret --throttle-every 20 --error-every 50
```

Entities are served from the current state: `Parts`, `ChangeNotices`, `CadDocuments` (with a `ModifyStamp gt <timestamp>` filter and paging through `@odata.nextLink`), plus `PartRevisions`, `AffectedItems`, `BomDeltas` and `Supersessions`. Run `advance` and the API shows the new data.

Check: your client handles 401, 429 (with `Retry-After`) and 503, never loses a row between pages, and keeps a watermark that survives restarts. Deleted rows are not returned by the API; they appear in the change log as `d`.

### 5. Batch extracts (BOM and master data)

Apply delta files in `_seq` order: `c` and `u` upsert, `d` deletes. An EBOM line is never deleted; it is end-dated with `effective_to`, so history is preserved. MBOM lines and the rows of a withdrawn part are deleted, so expect `d` operations.

### 6. Unstructured documents

Files are in `out/base/project_docs/files/` (initial) and `out/batches/batch=DATE/project_docs/files/`, with a `metadata/*.jsonl` per batch holding `file_name`, `bytes`, `sha256`, the document fields, and its part links. Formats are PDF (with a text layer), DOCX, and scanned-looking PNG with no text layer.

On Databricks, `ai_parse_document` reads files from volumes and handles PDF, images and DOCX. A typical path is to read the files, parse, chunk into a Delta table, and index for Vector Search. How a managed agent connects to the result is not verified here.

`truth/doc_facts.jsonl` records what each document says, so extraction can be scored. The question set includes document-content questions.

Check: sha256 matches the metadata, extracted part numbers match the links, and scans are read through OCR.

### 7. Governance

Documents and parts carry `export_control_class` (EAR99, ECCN-3A001, ITAR-USML-XIX), and a document takes the most restrictive class of its parts. Use it for column masks and row filters. Plants (`werks`) and suppliers give you a second dimension for row-level security.

### 8. Genie and RAG benchmarks

`truth/questions.jsonl` has about 250 questions across lifecycle, history, where-used, BOM explosion, rolled-up quantities, changes, supersession, suppliers, documents, export control and aggregates. Each has an `answer`, an `answer_type`, and the tables involved. Genie works over Unity Catalog tables only, so benchmark it on Silver or Gold, not on Lakebase or Lakehouse RT.

## What each dirty problem should trigger

| Injected problem | Where | Expected handling |
|---|---|---|
| Duplicate events | `sap_pdm`, `part_release` | drop by `event_id` |
| Out-of-order events | same | order by `lsn` or `sequence` |
| Corrupt JSON line | same | quarantine the line; the batch keeps running |
| Unexpected `extra` field | same | schema evolution or rescue column; no failure |
| Duplicate delta rows | `bom_mgmt`, `master` | dedupe by `_seq` |
| Mass in grams | `sap_pdm_material` | convert with `master_uom_conversion` |
| Orphan EBOM child key | `bom_mgmt_ebom_line` | fails the foreign key to the cross-reference |
| Released assembly with a Design or Obsolete child | `bom_mgmt_ebom_line` | data-quality rule |
| Change dated before its parts | `windchill_change_notice` | data-quality rule |
| Change on a part that is not Released | `windchill_change_affected_item` | data-quality rule |
| Missing file, truncated file, wrong part number in text, orphan file | `project_docs` | checksum check, link check, text-to-link check, orphan report |

Rates scale with file size, so a tiny micro-batch file is rarely touched and a busy one usually is. Each batch's `_batch.json` lists the exact counts injected.

## What it does not do

It does not connect to Databricks, create tables there, or run pipelines. It does not generate real engineering content such as CAD geometry. Reconciling attribute conflicts between sources is left to Silver, because the sources agree except where a problem was injected.
