# Usage guide

How to use each output to exercise a part of the architecture. Commands are shown for a workspace in `data/plm`.

Statements about Databricks features come from the platform documentation as of November 2026. Commands marked **untested** have not been run against a workspace from here.

## The daily loop

```bash
python -m plmgen advance --workspace data/plm --days 1      # a new day of activity
python -m plmgen validate --workspace data/plm              # the truth is still clean
```

Then land `out/batches/batch=DATE/` in your workspace, run your pipelines, and compare their results with `state/` and `truth/`. Use `--dirty` on `init` and `advance` to add problems to the delivered data, and `--activity 3` to simulate a burst.

To get files into a Unity Catalog volume you can use the Databricks CLI, for example `databricks fs cp --recursive data/plm/out/base dbfs:/Volumes/<catalog>/<schema>/<volume>/base` (**untested**).

## Scenarios

### 1. Bronze batch load, all at once or in parts

Each table in `out/base/<source>/<table>/` is split into numbered files (`part-00001.csv`, `part-00002.csv`, ...) of 2,000 rows each (`init --base-part-rows N` changes that, and `0` writes one file per table). Every part has a header, and rows run oldest first, so each part is a later slice of the same table. The CSVs are plain text, so declare types: `python -m plmgen schema --format ddl-spark` gives Spark DDL, and `--format spark-json` gives a schema per table. Empty fields are nulls.

To load incrementally, point a folder-based reader at a landing folder and let the parts arrive over time:

```bash
python -m plmgen drip --source data/plm/out/base --target data/plm/landing --interval 30
```

Round 1 delivers `part-00001.csv` of every table, round 2 delivers `part-00002.csv`, and so on. Files appear atomically. A reader that tracks which files it has already loaded, such as Auto Loader or `COPY INTO`, will then pick up only the new files each time.

Be aware that a part load is not a change feed. Parts are slices of one snapshot taken at the end of the history. They show what the data looks like when built up gradually, which is useful for testing file-tracking, idempotency and growing tables, but later parts do not update earlier rows. For true changes, use the daily batches from `advance`.

Check: row counts across all parts equal `row_counts` in `out/base/_base.json`. After the last round, join through `master_part_xref` and confirm every foreign key resolves. Partway through, foreign keys may legitimately be unresolved, because a child part can arrive before its parent.

### 2. Streaming CDC from SAP

`out/batches/batch=DATE/sap_pdm/cdc/events.jsonl` has one Debezium-style event per line: `event_id`, `lsn`, `table`, `op` (`c`, `u`, `d`), `key`, `before`, `after`. To simulate a live stream:

```bash
python -m plmgen replay --source data/plm/out/batches/batch=2026-11-04/sap_pdm/cdc/events.jsonl \
    --target data/plm/landing/sap_pdm --events-per-file 20 --rate 5
```

Point a streaming job (for example Auto Loader on JSON) at the landing folder, and MERGE into Silver. The reference behaviour is in `plmgen/replay.py` (`apply_cdc`): drop duplicate `event_id`s, apply events in `lsn` order whatever order they arrive, and apply deletes. A delete can be followed by a re-create of the same key.

Check: Silver `sap_pdm_material` equals the truth after you apply every batch. The tests do exactly this and match.

### 3. Workflow events (event mesh)

`part_release/events/events.jsonl` carries `TaskAssigned`, `TaskDecided` and `ReleaseIssued` events with `aggregate_id` (the change number), a `sequence` and a `payload`. If you have Kafka or Event Hubs, produce the lines to a topic and read them with a streaming source. Databricks also offers Zerobus Ingest, which writes straight to Unity Catalog tables over gRPC or REST and was generally available on AWS and Azure at the time of writing. It replaces a broker rather than complementing one, so test both if your client has Kafka.

Check: the latest decision per task matches `part_release_task`.

### 4. REST / OData pull from Windchill

```bash
python -m plmgen serve --workspace data/plm --port 8080 --token secret --throttle-every 20 --error-every 50
```

Entities are served from the current state: `Parts`, `ChangeNotices`, `CadDocuments` (with a `ModifyStamp gt <timestamp>` filter and paging through `@odata.nextLink`), plus `PartRevisions`, `AffectedItems`, `BomDeltas` and `Supersessions`. Run `advance` and the API shows the new data.

Check: your client handles 401, 429 (with `Retry-After`) and 503, never loses a row between pages, and keeps a watermark that survives restarts.

### 5. Batch extracts (BOM and master data)

`bom_mgmt/delta/*.csv` and `master/delta/*.csv` have `_op`, `_seq`, `_ts` and the table's columns. Apply them in `_seq` order: `c` and `u` upsert, `d` deletes. An EBOM line is never deleted; it is end-dated with `effective_to`, so history is preserved. MBOM lines are deleted when their EBOM line ends, so expect `d` operations there.

### 6. Unstructured documents

Files are in `out/base/project_docs/files/` (initial) and `out/batches/batch=DATE/project_docs/files/`, with a `metadata/*.jsonl` per batch holding `file_name`, `bytes`, `sha256`, the document fields, and its part links. Formats are PDF (with a text layer), DOCX, and scanned-looking PNG with no text layer.

On Databricks, `ai_parse_document` reads files from volumes and handles PDF, images and DOCX. A typical path is to read the files, parse, chunk into a Delta table, and index for Vector Search. How a managed agent connects to the result is not verified here.

`truth/doc_facts.jsonl` records what each document says (part numbers, revisions, mass, material, test results, lots and so on), so extraction can be scored. The question set includes document-content questions with expected answers.

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
| Corrupt JSON line | same | quarantine the line; the stream keeps running |
| Unexpected `extra` field | same | schema evolution or rescue column; no failure |
| Duplicate delta rows | `bom_mgmt`, `master` | dedupe by `_seq` |
| Mass in grams | `sap_pdm_material` | convert with `master_uom_conversion` |
| Orphan EBOM child key | `bom_mgmt_ebom_line` | fails the foreign key to the cross-reference |
| Released assembly with a Design or Obsolete child | `bom_mgmt_ebom_line` | data-quality rule |
| Change dated before its parts | `windchill_change_notice` | data-quality rule |
| Change on a part that is not Released | `windchill_change_affected_item` | data-quality rule |
| Missing file, truncated file, wrong part number in text, orphan file | `project_docs` | checksum check, link check, text-to-link check, orphan report |

Each batch's `_batch.json` lists the exact counts injected, and `python -m plmgen validate --workspace data/plm --base` reports the base extract's violations.

## What it does not do

It does not connect to Databricks, create tables, or run pipelines. It does not generate real engineering content such as CAD geometry. Reconciling attribute conflicts between sources is left to Silver, because the sources agree except where a problem was injected.
