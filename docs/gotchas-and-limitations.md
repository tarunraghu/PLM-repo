# Gotchas and Limitations

Working notes from the POC. Each item notes its source. **Verify** marks items that are still unconfirmed against current Databricks docs, so confirm them before committing to a design.

Last checked: 2026-11-03 against the Databricks documentation pages listed in each section.

## Component map (easy to confuse)

| Name in diagram | What it actually is | Notes |
|---|---|---|
| Lakehouse (RT) | A SQL warehouse feature for real-time dashboards over managed Delta tables | Beta. Requires contacting the Databricks account team to enable. |
| Lakebase ODS | Managed PostgreSQL for transactional reads and writes | Separate from Lakehouse RT. Dashboards do not query it. |
| Lakeflow / Lakebridge | Ingestion pipelines / migration tooling | Not covered in detail in this POC. |
| Genie | Natural-language analytics over Unity Catalog tables | Needs a pro or serverless SQL warehouse. |

Source: [Lakehouse Real-Time](https://docs.databricks.com/aws/en/compute/sql-warehouse/real-time), [Lakebase connect overview](https://docs.databricks.com/aws/en/oltp/projects/connect-overview).

## Lakebase (managed PostgreSQL)

- **Postgres is the operational store, not the analytics store.** Use it for low-latency reads and writes such as change orders and approvals. Do not run BOM explosions or large aggregations there; push those to Delta.
- **Token expiry is checked only at login.** OAuth tokens expire after one hour. An open connection stays active after its token expires, but a new connection needs a valid token. Long-running services should refresh the token before opening a connection, or refresh it on a background thread. Source: [Lakebase authentication](https://docs.databricks.com/aws/en/oltp/projects/authentication).
- **Connection lifetime limits.** Connections have a 24-hour idle timeout and a 3-day maximum lifetime. Pools must recycle connections before those limits. Source: [Lakebase connect overview](https://docs.databricks.com/aws/en/oltp/projects/connect-overview).
- **Scale-to-zero cold start.** A third-party summary says scale-to-zero is on by default with a 5-minute timeout, and that suspending the endpoint kills pooled connections. The first connection after a wake-up can be slow. **Verify** the default timeout in the computes docs, and add retry logic.
- **Sizing.** Autoscaling endpoints range from 0.5 to 32 CU, and fixed-size endpoints from 36 to 112 CU. Each CU is roughly 2 GB RAM. Source: a third-party summary, so **verify** before sizing.
- **Maximum concurrent connections are not confirmed.** Check the Databricks limits page for the instance size you use. **Verify.**
- **Schema changes are manual.** Lakebase is Postgres, so migrations need a tool such as Flyway, Liquibase or plain versioned SQL. Keep `sql/lakebase/` as versioned migrations.
- **Permissions are split.** Unity Catalog governs Delta tables. Postgres roles govern Lakebase. A grant in one does not apply in the other. Document both and test them with a non-admin user.
- **Password roles do not expire.** Native Postgres password roles avoid token expiry but lose OAuth's automatic expiry and central management. Prefer OAuth for services.

## Moving data between Lakebase and Delta (direction matters)

- **Synced tables go Unity Catalog → Lakebase.** They copy Delta tables into Postgres for low-latency reads. The Postgres copy is read-only, and Databricks recommends only read queries against it. Modes: Snapshot, Triggered and Continuous. Triggered and Continuous need change data feed on the source table (`delta.enableChangeDataFeed = true`). Source: [Serve lakehouse data with synced tables](https://docs.databricks.com/aws/en/oltp/projects/sync-tables).
- **Lakehouse Sync goes Lakebase → Unity Catalog.** It replicates Postgres tables into Delta as SCD Type 2 history. It requires replica identity set to `FULL` and does not support partitioned tables. Source: [Lakehouse Sync](https://docs.databricks.com/aws/en/oltp/projects/lakehouse-sync).
- **Write-back to Delta needs Lakehouse Sync.** Change orders written to Lakebase reach Gold through Lakehouse Sync, not synced tables. Lag depends on the sync mode, and no official latency figure was found. **Verify** before writing it into a client SLA.
- **Read-your-writes goes to Lakebase.** Anything written and read back through the API must read from Lakebase, not from Gold, or users see stale data.

## Lakehouse RT (SQL warehouse, Beta)

- **Beta, and the feature set will change.** Databricks says performance and supported features will change before general availability. Access requires the account team. Source: [Lakehouse Real-Time](https://docs.databricks.com/aws/en/compute/sql-warehouse/real-time).
- **Genie is not supported on dashboards published from a Lakehouse RT warehouse.** Use AI/BI dashboards instead. Source: same page.
- **ANSI SQL mode only for reads.** Port any non-ANSI SQL before moving queries here.
- **Managed tables only.** Convert external tables to managed tables before querying them through Lakehouse RT.
- **Do not migrate existing dashboards by default.** Databricks advises building new dashboards that fit the current limits.

## Delta Lake medallion

- **Small files and bad partitioning.** Streaming ingestion from CDC creates many small files. Plan compaction (OPTIMIZE) and liquid clustering or Z-ordering from the start.
- **Idempotent CDC merges.** Use MERGE keyed on the business key plus a sequence or change timestamp. Duplicated or out-of-order events from Kafka will corrupt Silver without this.
- **Revision and effectivity are not simple timestamps.** Part revisions, BOM effectivity and change-order status need SCD Type 2 or explicit effective-date ranges. Do not model them as "latest row wins" tables.
- **Units and attributes differ by source.** SAP PDM and Windchill will disagree on units of measure, part numbering and lifecycle state names. Put a mapping layer in Silver and keep the raw values in Bronze.

## Genie and AI/BI

- **Genie needs Unity Catalog.** All data must be registered in Unity Catalog. Source: [Genie set-up](https://docs.databricks.com/en/genie/set-up.html).
- **Table limit: 30 tables or views per Genie space.** Plan space boundaries early. Source: same page.
- **Pro or serverless SQL warehouse required.** Source: same page.
- **Genie has no row-level data access.** Object-level access comes from Unity Catalog privileges. Source: same page.
- **Genie throughput limits.** Up to 20 questions per minute per workspace through the UI, and up to 10,000 conversations per space. Source: same page.
- **Genie is not on Lakehouse RT dashboards or the Lakebase ODS.** Natural-language questions run over Silver or Gold Delta tables. The search did not confirm Lakebase as a Genie data source, so treat it as unsupported until verified.
- **Genie answers only over governed, documented tables.** Add table and column comments, and curate the space. Ungoverned tables produce confident wrong answers.
- **Ontology is not automatic.** The "ontology modeling and reasoning" box needs an explicit model (parts, assemblies, documents, suppliers, changes) written as data, not inferred by the LLM.
- **Generated code must be reviewed.** Do not let generated code write to the ODS without review.

## Unity Catalog and governance

- **Row-level and column-level security add query cost.** Row filters and column masks are enforced at query time. Test performance with realistic filter functions.
- **Group design matters more than table design.** Map client roles (engineer, planner, supplier, auditor) to groups first. Retrofitting grants later is painful.
- **Lineage covers Databricks workloads.** It does not cover the source PLM systems or external apps unless you integrate them. Set expectations with the client.
- **Audit logs are per workspace.** Confirm where the client wants audit data retained.

## Source system integration

- **SAP PDM and Windchill are not plain databases.** Access usually goes through vendor APIs, OData, extracts or middleware. Check licensing and API access early; it often takes weeks to get credentials and test systems.
- **CDC is often not available.** Many PLM systems do not expose change logs. Plan for full extracts with hash-based change detection as the fallback.
- **Project documents are unstructured.** Treat them as a metadata-plus-file problem. Store files in a volume or object store and keep only metadata in tables.
- **Rate limits and bulk windows.** Vendor APIs throttle, and extracts may only be allowed off-hours. Build backfill and incremental modes separately.

## Serving and write-back

- **Pick the API style early.** REST, GraphQL and OData each carry different caching and pagination behavior. Decide with the client's consuming apps in mind.

## Open questions for the client

1. Which PLM systems have API access, and what are the rate limits and change logs?
2. What are the latency targets for reads and writes in Lakebase, and for Lakebase to Gold?
3. Are AI/BI dashboards on Lakehouse RT acceptable, given Beta status and no Genie?
4. What is the data residency and retention requirement?
5. Who owns part numbering and lifecycle state definitions?
6. Which workflows must remain in the source systems during the transition?
