# Data dictionary

Generated from `plmgen/schema.py`. Do not edit by hand.

## sap_pdm

### `sap_pdm_vendor`

SAP vendor master.

Primary key: `lifnr`

| Column | Type | Null | Notes |
|---|---|---|---|
| `lifnr` | str |  |  |
| `name1` | str |  |  |
| `land1` | str |  |  |

### `sap_pdm_material`

SAP material master (CDC source).

Primary key: `matnr`

Foreign keys: `lifnr` → `sap_pdm_vendor.lifnr`, `werks` → `master_plant.plant_code`, `meins` → `master_uom.uom_code`, `gewei` → `master_uom.uom_code`

| Column | Type | Null | Notes |
|---|---|---|---|
| `matnr` | str |  |  |
| `maktx` | str |  |  |
| `mtart` | str |  |  |
| `meins` | str |  |  |
| `gewei` | str |  |  |
| `mass_value` | float |  | Mass in the unit given by gewei. |
| `estat` | str |  | SAP status: ENTW Design, PRUE In Review, FREI Released, OBSO Obsolete. |
| `werks` | str |  |  |
| `lifnr` | str |  |  |
| `erdat` | date |  |  |
| `aedat` | date |  |  |

## windchill

### `windchill_part`

Windchill part.

Primary key: `wt_id`

Foreign keys: `number` → `master_part.part_number`

| Column | Type | Null | Notes |
|---|---|---|---|
| `wt_id` | int |  |  |
| `number` | str |  |  |
| `name` | str |  |  |
| `version` | str |  |  |
| `state` | str |  | Windchill state: INWORK, UNDERREVIEW, RELEASED, OBSOLETE (parts); change states for notices. |
| `created_on` | date |  |  |
| `modified_at` | ts |  | Last modification timestamp (UTC). Use as a watermark for incremental pulls. |

### `windchill_part_revision`

Revision history. An implemented change creates the next revision.

Primary key: `wt_id, version`

Foreign keys: `wt_id` → `windchill_part.wt_id`

| Column | Type | Null | Notes |
|---|---|---|---|
| `wt_id` | int |  |  |
| `version` | str |  |  |
| `is_current` | bool |  |  |
| `effective_from` | date |  |  |
| `effective_to` | date | yes | Exclusive end date. Empty means still active or current. |

### `windchill_part_supersession`

A replaced part points to its replacement.

Primary key: `old_wt_id`

Foreign keys: `old_wt_id` → `windchill_part.wt_id`, `new_wt_id` → `windchill_part.wt_id`

| Column | Type | Null | Notes |
|---|---|---|---|
| `old_wt_id` | int |  |  |
| `new_wt_id` | int |  |  |
| `effective_on` | date |  |  |

### `windchill_change_notice`

Engineering change notice.

Primary key: `change_number`

| Column | Type | Null | Notes |
|---|---|---|---|
| `change_number` | str |  |  |
| `title` | str |  |  |
| `state` | str |  | Windchill state: INWORK, UNDERREVIEW, RELEASED, OBSOLETE (parts); change states for notices. |
| `state_since` | date |  | Date the current change state began. |
| `created_on` | date |  |  |
| `modified_at` | ts |  | Last modification timestamp (UTC). Use as a watermark for incremental pulls. |

### `windchill_change_affected_item`

Parts a change touches.

Primary key: `change_number, wt_id`

Foreign keys: `change_number` → `windchill_change_notice.change_number`, `wt_id` → `windchill_part.wt_id`

| Column | Type | Null | Notes |
|---|---|---|---|
| `change_number` | str |  |  |
| `wt_id` | int |  |  |
| `before_version` | str |  |  |
| `after_version` | str |  |  |

### `windchill_change_bom_delta`

Proposed BOM edits for a change, relative to the BOM on the change date.

Primary key: `change_number, parent_part_number, child_part_number`

Foreign keys: `change_number` → `windchill_change_notice.change_number`, `parent_part_number` → `master_part.part_number`, `child_part_number` → `master_part.part_number`

| Column | Type | Null | Notes |
|---|---|---|---|
| `change_number` | str |  |  |
| `parent_part_number` | str |  |  |
| `child_part_number` | str |  |  |
| `delta_type` | str |  |  |
| `old_quantity` | int | yes |  |
| `new_quantity` | int | yes |  |
| `status` | str |  | Proposed, Applied or Skipped for BOM deltas; Active or OnHold for suppliers. |

### `windchill_cad_document`

CAD file metadata.

Primary key: `cad_doc_id`

Foreign keys: `wt_id` → `windchill_part.wt_id`

| Column | Type | Null | Notes |
|---|---|---|---|
| `cad_doc_id` | str |  |  |
| `wt_id` | int |  |  |
| `file_name` | str |  |  |
| `cad_format` | str |  |  |
| `uploaded_on` | date |  |  |
| `modified_at` | ts |  | Last modification timestamp (UTC). Use as a watermark for incremental pulls. |

## bom_mgmt

### `bom_mgmt_ebom_line`

Engineering BOM line. An open effective_to means the line is active.

Primary key: `ebom_line_id`

Foreign keys: `parent_item_key` → `xref:bom_mgmt`, `child_item_key` → `xref:bom_mgmt`

| Column | Type | Null | Notes |
|---|---|---|---|
| `ebom_line_id` | str |  |  |
| `parent_item_key` | str |  |  |
| `child_item_key` | str |  |  |
| `quantity` | int |  |  |
| `find_number` | int |  |  |
| `effective_from` | date |  |  |
| `effective_to` | date | yes | Exclusive end date. Empty means still active or current. |
| `modified_at` | ts |  | Last modification timestamp (UTC). Use as a watermark for incremental pulls. |

### `bom_mgmt_ebom_line_effectivity`

Date or serial-number effectivity for a line. Values are strings and parsed by type.

Primary key: `ebom_line_id, seq`

Foreign keys: `ebom_line_id` → `bom_mgmt_ebom_line.ebom_line_id`

| Column | Type | Null | Notes |
|---|---|---|---|
| `ebom_line_id` | str |  |  |
| `seq` | int |  |  |
| `effectivity_type` | str |  |  |
| `from_value` | str |  |  |
| `to_value` | str |  |  |

### `bom_mgmt_mbom_line`

Manufacturing BOM line.

Primary key: `mbom_line_id`

Foreign keys: `parent_item_key` → `xref:bom_mgmt`, `child_item_key` → `xref:bom_mgmt`, `plant_code` → `master_plant.plant_code`, `source_ebom_line_id` → `bom_mgmt_ebom_line.ebom_line_id`

| Column | Type | Null | Notes |
|---|---|---|---|
| `mbom_line_id` | str |  |  |
| `parent_item_key` | str |  |  |
| `child_item_key` | str |  |  |
| `quantity` | int |  |  |
| `plant_code` | str |  |  |
| `source_ebom_line_id` | str | yes |  |
| `modified_at` | ts |  | Last modification timestamp (UTC). Use as a watermark for incremental pulls. |

## part_release

### `part_release_task`

Approval tasks of the release workflow.

Primary key: `task_id`

Foreign keys: `change_number` → `windchill_change_notice.change_number`

| Column | Type | Null | Notes |
|---|---|---|---|
| `task_id` | str |  |  |
| `change_number` | str |  |  |
| `step` | int |  |  |
| `approver_role` | str |  |  |
| `decision` | str |  |  |
| `assigned_on` | date |  |  |
| `decided_on` | date | yes |  |
| `modified_at` | ts |  | Last modification timestamp (UTC). Use as a watermark for incremental pulls. |

### `part_release_release`

A part revision released by an implemented change.

Primary key: `release_id`

Foreign keys: `change_number` → `windchill_change_notice.change_number`, `wt_id` → `windchill_part.wt_id`

| Column | Type | Null | Notes |
|---|---|---|---|
| `release_id` | str |  |  |
| `change_number` | str |  |  |
| `wt_id` | int |  |  |
| `released_version` | str |  |  |
| `released_on` | date |  |  |

## project_docs

### `project_docs_project`

Projects.

Primary key: `project_id`

| Column | Type | Null | Notes |
|---|---|---|---|
| `project_id` | str |  |  |
| `project_name` | str |  |  |
| `program_code` | str |  |  |

### `project_docs_document`

Document metadata. file_path points to the file in the landing volume.

Primary key: `document_id`

Foreign keys: `project_id` → `project_docs_project.project_id`

| Column | Type | Null | Notes |
|---|---|---|---|
| `document_id` | str |  |  |
| `project_id` | str |  |  |
| `document_type` | str |  |  |
| `title` | str |  |  |
| `file_format` | str |  |  |
| `file_path` | str |  |  |
| `uploaded_on` | date |  |  |
| `uploaded_by` | str |  |  |
| `export_control_class` | str |  | EAR99, ECCN-3A001 or ITAR-USML-XIX. A document takes the most restrictive class of its parts. |

### `project_docs_document_part_link`

Parts a document refers to, with the revision current at upload.

Primary key: `document_id, part_number`

Foreign keys: `document_id` → `project_docs_document.document_id`, `part_number` → `master_part.part_number`

| Column | Type | Null | Notes |
|---|---|---|---|
| `document_id` | str |  |  |
| `part_number` | str |  |  |
| `part_revision` | str |  |  |

## master

### `master_uom`

Units of measure.

Primary key: `uom_code`

| Column | Type | Null | Notes |
|---|---|---|---|
| `uom_code` | str |  |  |
| `uom_name` | str |  |  |
| `dimension` | str |  |  |

### `master_uom_conversion`

Conversion factors between units.

Primary key: `from_uom, to_uom`

Foreign keys: `from_uom` → `master_uom.uom_code`, `to_uom` → `master_uom.uom_code`

| Column | Type | Null | Notes |
|---|---|---|---|
| `from_uom` | str |  |  |
| `to_uom` | str |  |  |
| `factor` | float |  |  |

### `master_supplier`

Golden supplier list.

Primary key: `supplier_id`

| Column | Type | Null | Notes |
|---|---|---|---|
| `supplier_id` | str |  |  |
| `supplier_name` | str |  |  |
| `country` | str |  |  |
| `status` | str |  | Proposed, Applied or Skipped for BOM deltas; Active or OnHold for suppliers. |

### `master_supplier_xref`

Maps each golden supplier to its native key in each source.

Primary key: `source_system, native_key`

Foreign keys: `supplier_id` → `master_supplier.supplier_id`

| Column | Type | Null | Notes |
|---|---|---|---|
| `supplier_id` | str |  |  |
| `source_system` | str |  |  |
| `native_key` | str |  |  |

### `master_plant`

Plants.

Primary key: `plant_code`

| Column | Type | Null | Notes |
|---|---|---|---|
| `plant_code` | str |  |  |
| `plant_name` | str |  |  |
| `country` | str |  |  |

### `master_part`

Golden part master. master_system names the source that owns the part.

Primary key: `part_number`

Foreign keys: `primary_supplier_id` → `master_supplier.supplier_id`, `unit_of_measure` → `master_uom.uom_code`

| Column | Type | Null | Notes |
|---|---|---|---|
| `part_number` | str |  |  |
| `part_name` | str |  |  |
| `master_system` | str |  | Source that owns the part and wins attribute conflicts. |
| `canonical_lifecycle` | str |  |  |
| `lifecycle_since` | date |  | Date the current lifecycle state began. |
| `unit_of_measure` | str |  |  |
| `mass_kg` | float |  |  |
| `primary_supplier_id` | str |  |  |
| `export_control_class` | str |  | EAR99, ECCN-3A001 or ITAR-USML-XIX. A document takes the most restrictive class of its parts. |
| `created_date` | date |  |  |

### `master_part_lifecycle_history`

Lifecycle state history per part. The open row is the current state.

Primary key: `part_number, effective_from`

Foreign keys: `part_number` → `master_part.part_number`

| Column | Type | Null | Notes |
|---|---|---|---|
| `part_number` | str |  |  |
| `lifecycle_state` | str |  |  |
| `effective_from` | date |  |  |
| `effective_to` | date | yes | Exclusive end date. Empty means still active or current. |

### `master_part_xref`

Maps each golden part to its native key in each source.

Primary key: `source_system, native_key`

Foreign keys: `part_number` → `master_part.part_number`

| Column | Type | Null | Notes |
|---|---|---|---|
| `part_number` | str |  |  |
| `source_system` | str |  |  |
| `native_key` | str |  |  |

### `master_supplier_part`

Approved and candidate sources per part; exactly one primary.

Primary key: `supplier_id, part_number`

Foreign keys: `supplier_id` → `master_supplier.supplier_id`, `part_number` → `master_part.part_number`

| Column | Type | Null | Notes |
|---|---|---|---|
| `supplier_id` | str |  |  |
| `part_number` | str |  |  |
| `approval_status` | str |  |  |
| `is_primary` | bool |  |  |
| `lead_time_days` | int |  |  |
