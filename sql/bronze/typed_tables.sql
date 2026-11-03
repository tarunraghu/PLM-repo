-- Generated from plmgen/schema.py (spark). Do not edit by hand.

CREATE TABLE master_uom (
    uom_code STRING NOT NULL,
    uom_name STRING NOT NULL,
    dimension STRING NOT NULL
)
COMMENT 'Units of measure.';

CREATE TABLE master_uom_conversion (
    from_uom STRING NOT NULL,
    to_uom STRING NOT NULL,
    factor DOUBLE NOT NULL
)
COMMENT 'Conversion factors between units.';

CREATE TABLE master_supplier (
    supplier_id STRING NOT NULL,
    supplier_name STRING NOT NULL,
    country STRING NOT NULL,
    status STRING NOT NULL
)
COMMENT 'Golden supplier list.';

CREATE TABLE master_supplier_xref (
    supplier_id STRING NOT NULL,
    source_system STRING NOT NULL,
    native_key STRING NOT NULL
)
COMMENT 'Maps each golden supplier to its native key in each source.';

CREATE TABLE master_plant (
    plant_code STRING NOT NULL,
    plant_name STRING NOT NULL,
    country STRING NOT NULL
)
COMMENT 'Plants.';

CREATE TABLE master_part (
    part_number STRING NOT NULL,
    part_name STRING NOT NULL,
    master_system STRING NOT NULL,
    canonical_lifecycle STRING NOT NULL,
    lifecycle_since DATE NOT NULL,
    unit_of_measure STRING NOT NULL,
    mass_kg DOUBLE NOT NULL,
    primary_supplier_id STRING NOT NULL,
    export_control_class STRING NOT NULL,
    created_date DATE NOT NULL
)
COMMENT 'Golden part master. master_system names the source that owns the part.';

CREATE TABLE master_part_lifecycle_history (
    part_number STRING NOT NULL,
    lifecycle_state STRING NOT NULL,
    effective_from DATE NOT NULL,
    effective_to DATE
)
COMMENT 'Lifecycle state history per part. The open row is the current state.';

CREATE TABLE master_part_xref (
    part_number STRING NOT NULL,
    source_system STRING NOT NULL,
    native_key STRING NOT NULL
)
COMMENT 'Maps each golden part to its native key in each source.';

CREATE TABLE master_supplier_part (
    supplier_id STRING NOT NULL,
    part_number STRING NOT NULL,
    approval_status STRING NOT NULL,
    is_primary BOOLEAN NOT NULL,
    lead_time_days BIGINT NOT NULL
)
COMMENT 'Approved and candidate sources per part; exactly one primary.';

CREATE TABLE sap_pdm_vendor (
    lifnr STRING NOT NULL,
    name1 STRING NOT NULL,
    land1 STRING NOT NULL
)
COMMENT 'SAP vendor master.';

CREATE TABLE sap_pdm_material (
    matnr STRING NOT NULL,
    maktx STRING NOT NULL,
    mtart STRING NOT NULL,
    meins STRING NOT NULL,
    gewei STRING NOT NULL,
    mass_value DOUBLE NOT NULL,
    estat STRING NOT NULL,
    werks STRING NOT NULL,
    lifnr STRING NOT NULL,
    erdat DATE NOT NULL,
    aedat DATE NOT NULL
)
COMMENT 'SAP material master (CDC source).';

CREATE TABLE windchill_part (
    wt_id BIGINT NOT NULL,
    number STRING NOT NULL,
    name STRING NOT NULL,
    version STRING NOT NULL,
    state STRING NOT NULL,
    created_on DATE NOT NULL,
    modified_at TIMESTAMP NOT NULL
)
COMMENT 'Windchill part.';

CREATE TABLE windchill_part_revision (
    wt_id BIGINT NOT NULL,
    version STRING NOT NULL,
    is_current BOOLEAN NOT NULL,
    effective_from DATE NOT NULL,
    effective_to DATE
)
COMMENT 'Revision history. An implemented change creates the next revision.';

CREATE TABLE windchill_part_supersession (
    old_wt_id BIGINT NOT NULL,
    new_wt_id BIGINT NOT NULL,
    effective_on DATE NOT NULL
)
COMMENT 'A replaced part points to its replacement.';

CREATE TABLE windchill_change_notice (
    change_number STRING NOT NULL,
    title STRING NOT NULL,
    state STRING NOT NULL,
    state_since DATE NOT NULL,
    created_on DATE NOT NULL,
    modified_at TIMESTAMP NOT NULL
)
COMMENT 'Engineering change notice.';

CREATE TABLE windchill_change_affected_item (
    change_number STRING NOT NULL,
    wt_id BIGINT NOT NULL,
    before_version STRING NOT NULL,
    after_version STRING NOT NULL
)
COMMENT 'Parts a change touches.';

CREATE TABLE windchill_change_bom_delta (
    change_number STRING NOT NULL,
    parent_part_number STRING NOT NULL,
    child_part_number STRING NOT NULL,
    delta_type STRING NOT NULL,
    old_quantity BIGINT,
    new_quantity BIGINT,
    status STRING NOT NULL
)
COMMENT 'Proposed BOM edits for a change, relative to the BOM on the change date.';

CREATE TABLE windchill_cad_document (
    cad_doc_id STRING NOT NULL,
    wt_id BIGINT NOT NULL,
    file_name STRING NOT NULL,
    cad_format STRING NOT NULL,
    uploaded_on DATE NOT NULL,
    modified_at TIMESTAMP NOT NULL
)
COMMENT 'CAD file metadata.';

CREATE TABLE part_release_task (
    task_id STRING NOT NULL,
    change_number STRING NOT NULL,
    step BIGINT NOT NULL,
    approver_role STRING NOT NULL,
    decision STRING NOT NULL,
    assigned_on DATE NOT NULL,
    decided_on DATE,
    modified_at TIMESTAMP NOT NULL
)
COMMENT 'Approval tasks of the release workflow.';

CREATE TABLE part_release_release (
    release_id STRING NOT NULL,
    change_number STRING NOT NULL,
    wt_id BIGINT NOT NULL,
    released_version STRING NOT NULL,
    released_on DATE NOT NULL
)
COMMENT 'A part revision released by an implemented change.';

CREATE TABLE bom_mgmt_ebom_line (
    ebom_line_id STRING NOT NULL,
    parent_item_key STRING NOT NULL,
    child_item_key STRING NOT NULL,
    quantity BIGINT NOT NULL,
    find_number BIGINT NOT NULL,
    effective_from DATE NOT NULL,
    effective_to DATE,
    modified_at TIMESTAMP NOT NULL
)
COMMENT 'Engineering BOM line. An open effective_to means the line is active.';

CREATE TABLE bom_mgmt_ebom_line_effectivity (
    ebom_line_id STRING NOT NULL,
    seq BIGINT NOT NULL,
    effectivity_type STRING NOT NULL,
    from_value STRING NOT NULL,
    to_value STRING NOT NULL
)
COMMENT 'Date or serial-number effectivity for a line. Values are strings and parsed by type.';

CREATE TABLE bom_mgmt_mbom_line (
    mbom_line_id STRING NOT NULL,
    parent_item_key STRING NOT NULL,
    child_item_key STRING NOT NULL,
    quantity BIGINT NOT NULL,
    plant_code STRING NOT NULL,
    source_ebom_line_id STRING,
    modified_at TIMESTAMP NOT NULL
)
COMMENT 'Manufacturing BOM line.';

CREATE TABLE project_docs_project (
    project_id STRING NOT NULL,
    project_name STRING NOT NULL,
    program_code STRING NOT NULL
)
COMMENT 'Projects.';

CREATE TABLE project_docs_document (
    document_id STRING NOT NULL,
    project_id STRING NOT NULL,
    document_type STRING NOT NULL,
    title STRING NOT NULL,
    file_format STRING NOT NULL,
    file_path STRING NOT NULL,
    uploaded_on DATE NOT NULL,
    uploaded_by STRING NOT NULL,
    export_control_class STRING NOT NULL
)
COMMENT 'Document metadata. file_path points to the file in the landing volume.';

CREATE TABLE project_docs_document_part_link (
    document_id STRING NOT NULL,
    part_number STRING NOT NULL,
    part_revision STRING NOT NULL
)
COMMENT 'Parts a document refers to, with the revision current at upload.';
