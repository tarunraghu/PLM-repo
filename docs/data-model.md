# Data model and simulation

How the synthetic world is built, how the tables join, and what it does not cover. For every column see [data-dictionary.md](data-dictionary.md), which is generated from `plmgen/schema.py`.

## Sources and table prefixes

| Prefix | Source (diagram) | Tables |
|---|---|---|
| `sap_pdm_` | SAP PDM (ERP / product data) | `vendor`, `material` |
| `windchill_` | PTC Windchill (PLM and CAD) | `part`, `part_revision`, `part_supersession`, `change_notice`, `change_affected_item`, `change_bom_delta`, `cad_document` |
| `bom_mgmt_` | BOM Management (EBOM / MBOM) | `ebom_line`, `ebom_line_effectivity`, `mbom_line` |
| `part_release_` | Part Release (workflow and sign-off) | `task`, `release` |
| `project_docs_` | Project Docs (unstructured) | `project`, `document`, `document_part_link` |
| `master_` | Shared golden data and cross-references | `part`, `part_lifecycle_history`, `part_xref`, `supplier`, `supplier_xref`, `supplier_part`, `plant`, `uom`, `uom_conversion` |

## Keys

Each source uses its own key format. The golden key is the part number, and `master_part_xref` maps it to each source.

| Entity | Golden key | SAP PDM | Windchill | BOM tool |
|---|---|---|---|---|
| Part | `P-100001` | `matnr` (18 digits) | `wt_id` (integer); `number` equals the golden key | `BM-0000001` |
| Supplier | `SUP-00001` | `lifnr` (10 digits) | not used | not used |
| Change | n/a | n/a | `ECO-000001` | n/a |
| Plant | `PLT01` | `werks` | n/a | `plant_code` |

Joins between sources go through the cross-reference tables. For example, `sap_pdm_material.matnr` equals `master_part_xref.native_key` where `source_system = 'sap_pdm'`.

## Lifecycle mapping

| Canonical | SAP (`estat`) | Windchill (`state`) |
|---|---|---|
| Design | ENTW | INWORK |
| In Review | PRUE | UNDERREVIEW |
| Released | FREI | RELEASED |
| Obsolete | OBSO | OBSOLETE |

Parts move one way: Design, In Review, Released, Obsolete. The history is in `master_part_lifecycle_history`, where the open row is the current state.

## How the simulation behaves

It runs one day at a time. Each day:

1. **Parts arrive** at a steady rate. About 12% are assemblies, built from existing parts, favouring older and shared parts, so structures grow bottom-up and reach `--bom-depth` levels.
2. **Parts progress.** A part waits a randomised time in each state. It can only move to In Review when its children are Released or In Review, and to Released when all its children are Released. A Released part may later be obsoleted, but only by replacing it in every active parent with another Released part. That writes a supersession row and end-dates the old BOM lines.
3. **Changes arrive** against Released parts, one change at a time per part. A change goes Open, then In Approval (two to four sequential tasks, with a chance of rejection), then Approved, then Implemented. Implementing it creates the next revision of each part, writes a release, and applies any BOM deltas that are still valid.
4. **Design-state assemblies are edited freely**, with no change control.
5. **Master data drifts:** mass updates, primary-supplier swaps, alternate approvals, new suppliers, and the occasional SAP delete-and-recreate.
6. **Documents arrive**, linked to one to three parts, each recording the revision current at upload.

Every random draw comes from a stream keyed on the seed and either the day or one entity. Nothing depends on how many days were run before, which is why incremental runs equal one-shot runs.

## Rules the validator enforces

- Primary keys, unique keys and foreign keys hold across every table.
- No record is dated after the as-of date.
- Each part has exactly one approved primary supplier, matching the master and SAP.
- SAP status, SAP mass and Windchill state agree with the master lifecycle. Mass in grams with unit `G` is valid, given the conversion.
- Lifecycle history is contiguous, starts at creation, and follows only legal moves. Revision history is a gap-free chain, with one current revision matching the part.
- Obsolete parts have a supersession row dated on the day they became obsolete.
- The active EBOM is acyclic and within the depth limit. A parent only contains child states it allows: Released contains Released; In Review contains Released or In Review; Design contains any active state; Obsolete contains anything.
- MBOM lines that cite an EBOM line match an active one.
- Changes touch only parts that were Released on the change date, one change at a time per part, with a before-version that was the effective revision that day.
- Approval tasks follow the change state and step order. A release exists for each affected item of an implemented change, on the right date, creating the new revision.
- Each BOM delta is consistent with the BOM on the change date, and applied deltas show up in the BOM.
- Documents are uploaded after the parts they link to, cite a revision that was effective at upload, and carry the most restrictive export class of their parts.

## Delivered data and the truth

`state/` is the truth and is always clean. `out/` is what the sources deliver and may be dirty. The generator never loads dirty data back into the truth.

## Known limits

- One workflow per change; no parallel approvals, delegation or escalation.
- Lifecycle, mass and revision are the same across sources, apart from the injected issues. Real systems disagree, and Silver must pick the master system's value.
- CDC events exist for SAP only. Windchill uses a change log and a REST API, BOM and master data use delta extracts, and Part Release uses workflow events.
- Only about a third of parts appear in a BOM by default (`--assemblies-ratio`).
- Documents carry metadata and generated text, not engineering content. Scans are rendered text with noise, not real drawings.
- The Lakebase schema in `sql/lakebase/` is a curated operational schema, not a mirror of the extracts.
