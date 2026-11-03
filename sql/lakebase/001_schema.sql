-- Lakebase ODS schema for the PLM POC (PostgreSQL).
-- Operational store for low-latency reads and write-back.
--   Lakebase -> Delta: Lakehouse Sync (needs replica identity FULL, no partitioned tables).
--   Delta -> Lakebase: synced tables for read-only copies of Gold.
-- Base schema. Not applied anywhere yet, so it is edited in place. Once a Lakebase
-- instance holds data, later changes go in new numbered files (002_*.sql, ...).
-- Replica identity must be FULL on tables that Lakehouse Sync reads. Set it after
-- creating the tables, for example: ALTER TABLE plm.change_order REPLICA IDENTITY FULL;

CREATE SCHEMA IF NOT EXISTS plm;

-- Units of measure and conversions. Silver and the ODS both convert through these.
CREATE TABLE plm.uom (
    uom_code   text PRIMARY KEY,
    uom_name   text NOT NULL,
    dimension  text NOT NULL CHECK (dimension IN ('count', 'mass', 'length', 'volume'))
);

CREATE TABLE plm.uom_conversion (
    from_uom  text NOT NULL REFERENCES plm.uom (uom_code),
    to_uom    text NOT NULL REFERENCES plm.uom (uom_code),
    factor    numeric(18, 9) NOT NULL CHECK (factor > 0),
    PRIMARY KEY (from_uom, to_uom)
);

CREATE TABLE plm.plant (
    plant_code  text PRIMARY KEY,
    plant_name  text NOT NULL,
    country     char(2)
);

CREATE TABLE plm.project (
    project_id    text PRIMARY KEY,
    project_name  text NOT NULL,
    program_code  text
);

CREATE TABLE plm.supplier (
    supplier_id    text PRIMARY KEY,
    supplier_name  text NOT NULL,
    country        char(2),
    status         text NOT NULL DEFAULT 'Active' CHECK (status IN ('Active', 'OnHold')),
    source_system  text NOT NULL,
    version        integer NOT NULL DEFAULT 1,      -- optimistic locking
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE plm.part (
    part_number          text PRIMARY KEY,
    part_name            text NOT NULL,
    lifecycle_state      text NOT NULL
        CHECK (lifecycle_state IN ('Design', 'In Review', 'Released', 'Obsolete')),
    lifecycle_since      date NOT NULL,
    plant_code           text REFERENCES plm.plant (plant_code),
    unit_of_measure      text NOT NULL REFERENCES plm.uom (uom_code),
    mass_kg              numeric(12, 4),           -- normalized to kg in ODS
    primary_supplier_id  text REFERENCES plm.supplier (supplier_id),
    export_control_class text NOT NULL DEFAULT 'EAR99'
        CHECK (export_control_class IN ('EAR99', 'ECCN-3A001', 'ITAR-USML-XIX')),
    source_system        text NOT NULL,
    created_date         date NOT NULL,
    version              integer NOT NULL DEFAULT 1,
    updated_at           timestamptz NOT NULL DEFAULT now()
);

-- Approved and candidate sources per part. Exactly one primary per part (see index below).
CREATE TABLE plm.supplier_part (
    supplier_id       text NOT NULL REFERENCES plm.supplier (supplier_id),
    part_number       text NOT NULL REFERENCES plm.part (part_number),
    approval_status   text NOT NULL CHECK (approval_status IN ('Approved', 'Pending', 'Blocked')),
    is_primary        boolean NOT NULL DEFAULT false,
    lead_time_days    integer CHECK (lead_time_days >= 0),
    PRIMARY KEY (supplier_id, part_number),
    CHECK (NOT is_primary OR approval_status = 'Approved')
);

CREATE UNIQUE INDEX supplier_part_one_primary
    ON plm.supplier_part (part_number) WHERE is_primary;

-- One row per revision. Current revision is flagged, not inferred from order.
CREATE TABLE plm.part_revision (
    part_number     text NOT NULL REFERENCES plm.part (part_number),
    revision        text NOT NULL,
    is_current      boolean NOT NULL DEFAULT false,
    effective_from  date NOT NULL,
    effective_to    date,
    PRIMARY KEY (part_number, revision),
    CHECK (effective_to IS NULL OR effective_to >= effective_from)
);

-- Only one current revision per part.
CREATE UNIQUE INDEX part_revision_one_current
    ON plm.part_revision (part_number) WHERE is_current;

-- Lifecycle state history. The open row (effective_to IS NULL) is the current state.
-- Legal moves are Design -> In Review -> Released -> Obsolete; the API enforces them.
CREATE TABLE plm.part_lifecycle_history (
    part_number      text NOT NULL REFERENCES plm.part (part_number),
    lifecycle_state  text NOT NULL
        CHECK (lifecycle_state IN ('Design', 'In Review', 'Released', 'Obsolete')),
    effective_from   date NOT NULL,
    effective_to     date,
    PRIMARY KEY (part_number, effective_from),
    CHECK (effective_to IS NULL OR effective_to >= effective_from)
);

CREATE UNIQUE INDEX part_lifecycle_one_open
    ON plm.part_lifecycle_history (part_number) WHERE effective_to IS NULL;

-- A replaced part points to its replacement. The replaced part must be Obsolete (enforced by Silver rules).
CREATE TABLE plm.part_supersession (
    old_part_number  text PRIMARY KEY REFERENCES plm.part (part_number),
    new_part_number  text NOT NULL REFERENCES plm.part (part_number),
    effective_on     date NOT NULL,
    CHECK (old_part_number <> new_part_number)
);

CREATE TABLE plm.bom_line (
    bom_line_id        text PRIMARY KEY,
    parent_part_number text NOT NULL REFERENCES plm.part (part_number),
    child_part_number  text NOT NULL REFERENCES plm.part (part_number),
    quantity           numeric(12, 4) NOT NULL CHECK (quantity > 0),
    find_number        integer,
    effective_from     date NOT NULL,
    effective_to       date,
    source_system      text NOT NULL,
    version            integer NOT NULL DEFAULT 1,
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CHECK (parent_part_number <> child_part_number)
);

CREATE INDEX bom_line_parent_idx ON plm.bom_line (parent_part_number);
CREATE INDEX bom_line_child_idx  ON plm.bom_line (child_part_number);  -- where-used

-- Only one active line per parent and child. effective_to is exclusive; an open value means active.
CREATE UNIQUE INDEX bom_line_one_active
    ON plm.bom_line (parent_part_number, child_part_number) WHERE effective_to IS NULL;

-- Manufacturing BOM. source_bom_line_id is empty for manufacturing-only items.
CREATE TABLE plm.mbom_line (
    mbom_line_id        text PRIMARY KEY,
    parent_part_number  text NOT NULL REFERENCES plm.part (part_number),
    child_part_number   text NOT NULL REFERENCES plm.part (part_number),
    quantity            numeric(12, 4) NOT NULL CHECK (quantity > 0),
    plant_code          text NOT NULL REFERENCES plm.plant (plant_code),
    source_bom_line_id  text REFERENCES plm.bom_line (bom_line_id)
);

-- Effectivity beyond the line's date range: by date window or by serial-number range.
-- A line with no rows here is effective for its whole date range.
CREATE TABLE plm.bom_line_effectivity (
    bom_line_id     text NOT NULL REFERENCES plm.bom_line (bom_line_id),
    seq             integer NOT NULL,
    effectivity_type text NOT NULL CHECK (effectivity_type IN ('DATE', 'SERIAL')),
    effective_from_date date,
    effective_to_date   date,
    serial_from     bigint,
    serial_to       bigint,
    PRIMARY KEY (bom_line_id, seq),
    CHECK (
        (effectivity_type = 'DATE'
            AND effective_from_date IS NOT NULL AND serial_from IS NULL
            AND (effective_to_date IS NULL OR effective_to_date >= effective_from_date))
        OR
        (effectivity_type = 'SERIAL'
            AND serial_from IS NOT NULL AND effective_from_date IS NULL
            AND serial_to >= serial_from)
    )
);

CREATE TABLE plm.change_order (
    change_id    text PRIMARY KEY,
    change_type  text NOT NULL CHECK (change_type IN ('ECR', 'ECO', 'ECN')),
    title        text NOT NULL,
    status       text NOT NULL
        CHECK (status IN ('Open', 'In Approval', 'Approved', 'Implemented', 'Rejected')),
    raised_date  date NOT NULL,
    status_since date NOT NULL,
    source_system text NOT NULL,
    version      integer NOT NULL DEFAULT 1,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE plm.change_affected_part (
    change_id      text NOT NULL REFERENCES plm.change_order (change_id),
    part_number    text NOT NULL REFERENCES plm.part (part_number),
    from_revision  text,
    to_revision    text,
    PRIMARY KEY (change_id, part_number)
);

-- Proposed BOM edits for a change, relative to the baseline BOM.
CREATE TABLE plm.change_bom_delta (
    change_id           text NOT NULL REFERENCES plm.change_order (change_id),
    parent_part_number  text NOT NULL REFERENCES plm.part (part_number),
    child_part_number   text NOT NULL REFERENCES plm.part (part_number),
    delta_type          text NOT NULL CHECK (delta_type IN ('ADDED', 'REMOVED', 'QTY_CHANGED')),
    old_quantity        numeric(12, 4),
    new_quantity        numeric(12, 4),
    status              text NOT NULL DEFAULT 'Proposed' CHECK (status IN ('Proposed', 'Applied', 'Skipped')),
    PRIMARY KEY (change_id, parent_part_number, child_part_number),
    CHECK (
        (delta_type = 'ADDED' AND old_quantity IS NULL AND new_quantity > 0)
        OR (delta_type = 'REMOVED' AND old_quantity > 0 AND new_quantity IS NULL)
        OR (delta_type = 'QTY_CHANGED' AND old_quantity > 0 AND new_quantity > 0
            AND new_quantity <> old_quantity)
    )
);

CREATE TABLE plm.change_approval (
    change_id      text NOT NULL REFERENCES plm.change_order (change_id),
    step           integer NOT NULL,
    approver_role  text NOT NULL,
    decision       text NOT NULL CHECK (decision IN ('Approved', 'Rejected', 'Pending')),
    assigned_at    timestamptz NOT NULL DEFAULT now(),
    decided_at     timestamptz,
    PRIMARY KEY (change_id, step),
    CHECK (decision = 'Pending' OR decided_at IS NOT NULL)
);

-- A part revision released by an implemented change.
CREATE TABLE plm.part_release (
    release_id         text PRIMARY KEY,
    change_id          text NOT NULL REFERENCES plm.change_order (change_id),
    part_number        text NOT NULL REFERENCES plm.part (part_number),
    released_revision  text NOT NULL,
    released_on        date NOT NULL,
    UNIQUE (change_id, part_number)
);

CREATE TABLE plm.document (
    document_id          text PRIMARY KEY,
    project_id           text REFERENCES plm.project (project_id),
    document_type        text NOT NULL,
    file_format          text NOT NULL CHECK (file_format IN ('pdf', 'docx', 'png')),
    title                text NOT NULL,
    file_path            text NOT NULL,                  -- pointer to the volume; file is not stored here
    uploaded_date        date NOT NULL,
    source_system        text NOT NULL,
    -- Derived: the most restrictive class of the parts this document links to.
    export_control_class text NOT NULL DEFAULT 'EAR99'
        CHECK (export_control_class IN ('EAR99', 'ECCN-3A001', 'ITAR-USML-XIX'))
);

CREATE TABLE plm.document_part_link (
    document_id    text NOT NULL REFERENCES plm.document (document_id),
    part_number    text NOT NULL REFERENCES plm.part (part_number),
    part_revision  text,                              -- revision that was current when the document was uploaded
    PRIMARY KEY (document_id, part_number)
);

-- Audit trail for write-back. Every API write lands here, so Delta can rebuild history.
CREATE TABLE plm.audit_event (
    event_id     bigserial PRIMARY KEY,
    entity       text NOT NULL,
    entity_key   text NOT NULL,
    action       text NOT NULL CHECK (action IN ('INSERT', 'UPDATE', 'DELETE')),
    actor        text NOT NULL,
    payload      jsonb,
    occurred_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX audit_event_entity_idx ON plm.audit_event (entity, entity_key);
