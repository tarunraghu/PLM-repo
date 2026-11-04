"""Single source of truth for every table: columns, types, keys and relationships.

Everything else (the database, CSV load and save, the validator, DDL and the
data dictionary) is driven from the definitions in this file.

Column spec: "name:type" with a trailing "?" when the column is nullable.
Types: str, int, float, date, ts (UTC timestamp), bool.
"""

from dataclasses import dataclass, field
from datetime import date, datetime

TYPES = ("str", "int", "float", "date", "ts", "bool")

SPARK_TYPES = {"str": "STRING", "int": "BIGINT", "float": "DOUBLE", "date": "DATE",
               "ts": "TIMESTAMP", "bool": "BOOLEAN"}
POSTGRES_TYPES = {"str": "text", "int": "bigint", "float": "double precision", "date": "date",
                  "ts": "timestamptz", "bool": "boolean"}

SOURCES = ("sap_pdm", "windchill", "bom_mgmt", "part_release", "project_docs", "master")

CANONICAL = ["Design", "In Review", "Released", "Obsolete"]
SAP_STATUS = {"Design": "ENTW", "In Review": "PRUE", "Released": "FREI", "Obsolete": "OBSO"}
WINDCHILL_STATE = {"Design": "INWORK", "In Review": "UNDERREVIEW", "Released": "RELEASED", "Obsolete": "OBSOLETE"}
# Which child lifecycle states a parent in a given state may contain.
ALLOWED_CHILD = {
    "Released": {"Released"},
    "In Review": {"Released", "In Review"},
    "Design": {"Released", "In Review", "Design"},
    "Obsolete": set(CANONICAL),
}
EXPORT_CLASSES = ["EAR99", "ECCN-3A001", "ITAR-USML-XIX"]
EXPORT_RANK = {c: rank for rank, c in enumerate(EXPORT_CLASSES)}
CHANGE_STATES = ["Open", "In Approval", "Approved", "Implemented", "Rejected"]
IN_FLIGHT_STATES = ("Open", "In Approval", "Approved")


@dataclass(frozen=True)
class Col:
    name: str
    type: str
    nullable: bool = False


@dataclass
class Table:
    name: str
    source: str
    cols: list
    pk: list
    doc: str = ""
    fks: dict = field(default_factory=dict)       # column -> "table.column" or "xref:<source>"
    unique: list = field(default_factory=list)    # extra unique column groups
    indexes: dict = field(default_factory=dict)   # index name -> tuple of columns

    @property
    def col_names(self):
        return [c.name for c in self.cols]

    def col(self, name):
        return next(c for c in self.cols if c.name == name)


def _cols(spec):
    cols = []
    for item in spec.replace("\n", " ").split(","):
        item = item.strip()
        if not item:
            continue
        name, _, type_ = item.partition(":")
        nullable = type_.endswith("?")
        type_ = type_.rstrip("?")
        assert type_ in TYPES, f"bad type in {item!r}"
        cols.append(Col(name, type_, nullable))
    return cols


def _table(name, source, cols, pk, doc="", fks=None, unique=None, indexes=None):
    assert source in SOURCES
    return Table(name, source, _cols(cols), pk.split(","), doc, fks or {}, [u.split(",") for u in (unique or [])],
                 {k: tuple(v.split(",")) for k, v in (indexes or {}).items()})


TABLES = {t.name: t for t in [
    _table("master_uom", "master", "uom_code:str, uom_name:str, dimension:str", "uom_code",
           "Units of measure."),
    _table("master_uom_conversion", "master", "from_uom:str, to_uom:str, factor:float", "from_uom,to_uom",
           "Conversion factors between units.",
           fks={"from_uom": "master_uom.uom_code", "to_uom": "master_uom.uom_code"}),
    _table("master_supplier", "master", "supplier_id:str, supplier_name:str, country:str, status:str", "supplier_id",
           "Golden supplier list."),
    _table("master_supplier_xref", "master", "supplier_id:str, source_system:str, native_key:str",
           "source_system,native_key", "Maps each golden supplier to its native key in each source.",
           fks={"supplier_id": "master_supplier.supplier_id"}),
    _table("master_plant", "master", "plant_code:str, plant_name:str, country:str", "plant_code", "Plants."),
    _table("master_part", "master",
           "part_number:str, part_name:str, master_system:str, canonical_lifecycle:str, lifecycle_since:date, "
           "unit_of_measure:str, mass_kg:float, primary_supplier_id:str, export_control_class:str, created_date:date",
           "part_number", "Golden part master. master_system names the source that owns the part.",
           fks={"primary_supplier_id": "master_supplier.supplier_id", "unit_of_measure": "master_uom.uom_code"}),
    _table("master_part_lifecycle_history", "master",
           "part_number:str, lifecycle_state:str, effective_from:date, effective_to:date?",
           "part_number,effective_from", "Lifecycle state history per part. The open row is the current state.",
           fks={"part_number": "master_part.part_number"}, indexes={"by_part": "part_number"}),
    _table("master_part_xref", "master", "part_number:str, source_system:str, native_key:str",
           "source_system,native_key", "Maps each golden part to its native key in each source.",
           unique=["part_number,source_system"], fks={"part_number": "master_part.part_number"},
           indexes={"by_part": "part_number"}),
    _table("master_supplier_part", "master",
           "supplier_id:str, part_number:str, approval_status:str, is_primary:bool, lead_time_days:int",
           "supplier_id,part_number", "Approved and candidate sources per part; exactly one primary.",
           fks={"supplier_id": "master_supplier.supplier_id", "part_number": "master_part.part_number"},
           indexes={"by_part": "part_number"}),
    _table("sap_pdm_vendor", "sap_pdm", "lifnr:str, name1:str, land1:str", "lifnr", "SAP vendor master."),
    _table("sap_pdm_material", "sap_pdm",
           "matnr:str, maktx:str, mtart:str, meins:str, gewei:str, mass_value:float, estat:str, werks:str, "
           "lifnr:str, erdat:date, aedat:date", "matnr", "SAP material master (CDC source).",
           fks={"lifnr": "sap_pdm_vendor.lifnr", "werks": "master_plant.plant_code",
                "meins": "master_uom.uom_code", "gewei": "master_uom.uom_code"}),
    _table("windchill_part", "windchill",
           "wt_id:int, number:str, name:str, version:str, state:str, created_on:date, modified_at:ts",
           "wt_id", "Windchill part.", unique=["number"], fks={"number": "master_part.part_number"}),
    _table("windchill_part_revision", "windchill",
           "wt_id:int, version:str, is_current:bool, effective_from:date, effective_to:date?",
           "wt_id,version", "Revision history. An implemented change creates the next revision.",
           fks={"wt_id": "windchill_part.wt_id"}, indexes={"by_part": "wt_id"}),
    _table("windchill_part_supersession", "windchill", "old_wt_id:int, new_wt_id:int, effective_on:date",
           "old_wt_id", "A replaced part points to its replacement.",
           fks={"old_wt_id": "windchill_part.wt_id", "new_wt_id": "windchill_part.wt_id"},
           indexes={"by_new": "new_wt_id"}),
    _table("windchill_change_notice", "windchill",
           "change_number:str, title:str, state:str, state_since:date, created_on:date, modified_at:ts",
           "change_number", "Engineering change notice."),
    _table("windchill_change_affected_item", "windchill",
           "change_number:str, wt_id:int, before_version:str, after_version:str", "change_number,wt_id",
           "Parts a change touches.",
           fks={"change_number": "windchill_change_notice.change_number", "wt_id": "windchill_part.wt_id"},
           indexes={"by_change": "change_number", "by_part": "wt_id"}),
    _table("windchill_change_bom_delta", "windchill",
           "change_number:str, parent_part_number:str, child_part_number:str, delta_type:str, old_quantity:int?, "
           "new_quantity:int?, status:str", "change_number,parent_part_number,child_part_number",
           "Proposed BOM edits for a change, relative to the BOM on the change date.",
           fks={"change_number": "windchill_change_notice.change_number",
                "parent_part_number": "master_part.part_number", "child_part_number": "master_part.part_number"},
           indexes={"by_change": "change_number", "by_parent": "parent_part_number",
                    "by_child": "child_part_number"}),
    _table("windchill_cad_document", "windchill",
           "cad_doc_id:str, wt_id:int, file_name:str, cad_format:str, uploaded_on:date, modified_at:ts",
           "cad_doc_id", "CAD file metadata.", fks={"wt_id": "windchill_part.wt_id"},
           indexes={"by_part": "wt_id"}),
    _table("part_release_task", "part_release",
           "task_id:str, change_number:str, step:int, approver_role:str, decision:str, assigned_on:date, "
           "decided_on:date?, modified_at:ts", "task_id", "Approval tasks of the release workflow.",
           unique=["change_number,step"], fks={"change_number": "windchill_change_notice.change_number"},
           indexes={"by_change": "change_number"}),
    _table("part_release_release", "part_release",
           "release_id:str, change_number:str, wt_id:int, released_version:str, released_on:date", "release_id",
           "A part revision released by an implemented change.", unique=["change_number,wt_id"],
           fks={"change_number": "windchill_change_notice.change_number", "wt_id": "windchill_part.wt_id"},
           indexes={"by_change": "change_number"}),
    _table("bom_mgmt_ebom_line", "bom_mgmt",
           "ebom_line_id:str, parent_item_key:str, child_item_key:str, quantity:int, find_number:int, "
           "effective_from:date, effective_to:date?, modified_at:ts", "ebom_line_id",
           "Engineering BOM line. An open effective_to means the line is active.",
           fks={"parent_item_key": "xref:bom_mgmt", "child_item_key": "xref:bom_mgmt"},
           indexes={"by_parent": "parent_item_key", "by_child": "child_item_key"}),
    _table("bom_mgmt_ebom_line_effectivity", "bom_mgmt",
           "ebom_line_id:str, seq:int, effectivity_type:str, from_value:str, to_value:str", "ebom_line_id,seq",
           "Date or serial-number effectivity for a line. Values are strings and parsed by type.",
           fks={"ebom_line_id": "bom_mgmt_ebom_line.ebom_line_id"}, indexes={"by_line": "ebom_line_id"}),
    _table("bom_mgmt_mbom_line", "bom_mgmt",
           "mbom_line_id:str, parent_item_key:str, child_item_key:str, quantity:int, plant_code:str, "
           "source_ebom_line_id:str?, modified_at:ts", "mbom_line_id", "Manufacturing BOM line.",
           fks={"parent_item_key": "xref:bom_mgmt", "child_item_key": "xref:bom_mgmt",
                "plant_code": "master_plant.plant_code", "source_ebom_line_id": "bom_mgmt_ebom_line.ebom_line_id"},
           indexes={"by_source": "source_ebom_line_id", "by_parent": "parent_item_key", "by_child": "child_item_key"}),
    _table("project_docs_project", "project_docs", "project_id:str, project_name:str, program_code:str",
           "project_id", "Projects."),
    _table("project_docs_document", "project_docs",
           "document_id:str, project_id:str, document_type:str, title:str, file_format:str, file_path:str, "
           "uploaded_on:date, uploaded_by:str, export_control_class:str", "document_id",
           "Document metadata. file_path points to the file in the landing volume.",
           fks={"project_id": "project_docs_project.project_id"}),
    _table("project_docs_document_part_link", "project_docs", "document_id:str, part_number:str, part_revision:str",
           "document_id,part_number", "Parts a document refers to, with the revision current at upload.",
           fks={"document_id": "project_docs_document.document_id", "part_number": "master_part.part_number"},
           indexes={"by_doc": "document_id", "by_part": "part_number"}),
]}

COLUMN_DOCS = {
    "master_system": "Source that owns the part and wins attribute conflicts.",
    "lifecycle_since": "Date the current lifecycle state began.",
    "state_since": "Date the current change state began.",
    "modified_at": "Last modification timestamp (UTC). Use as a watermark for incremental pulls.",
    "effective_to": "Exclusive end date. Empty means still active or current.",
    "estat": "SAP status: ENTW Design, PRUE In Review, FREI Released, OBSO Obsolete.",
    "state": "Windchill state: INWORK, UNDERREVIEW, RELEASED, OBSOLETE (parts); change states for notices.",
    "mass_value": "Mass in the unit given by gewei.",
    "export_control_class": "EAR99, ECCN-3A001 or ITAR-USML-XIX. A document takes the most restrictive class of its parts.",
    "status": "Proposed, Applied or Skipped for BOM deltas; Active or OnHold for suppliers.",
}


# ---- typed conversion between CSV text and Python values ---------------------------------------

def parse_value(text, type_):
    if text == "" or text is None:
        return None
    if type_ == "str":
        return text
    if type_ == "int":
        return int(text)
    if type_ == "float":
        return float(text)
    if type_ == "date":
        return date.fromisoformat(text)
    if type_ == "ts":
        return datetime.fromisoformat(text.rstrip("Z"))
    if type_ == "bool":
        return text == "true"
    raise ValueError(type_)


def format_value(value, type_):
    if value is None:
        return ""
    if type_ == "date":
        return value.isoformat()
    if type_ == "ts":
        return value.isoformat(timespec="milliseconds") + "Z"
    if type_ == "bool":
        return "true" if value else "false"
    return str(value)


def to_json_value(value, type_):
    """JSON-friendly value for events and APIs."""
    if value is None:
        return None
    if type_ in ("date", "ts"):
        return format_value(value, type_)
    return value


# ---- generated artefacts -----------------------------------------------------------------------

def dependency_order():
    """Table names ordered so referenced tables come before the tables that reference them."""
    order, seen = [], set()

    def visit(name):
        if name in seen:
            return
        seen.add(name)
        for target in TABLES[name].fks.values():
            if not target.startswith("xref:"):
                visit(target.split(".")[0])
        order.append(name)

    for name in TABLES:
        visit(name)
    return order


def ddl(dialect):
    types = SPARK_TYPES if dialect == "spark" else POSTGRES_TYPES
    out = [f"-- Generated from plmgen/schema.py ({dialect}). Do not edit by hand."]
    for name in dependency_order():
        t = TABLES[name]
        lines = []
        for c in t.cols:
            null = "" if c.nullable else " NOT NULL"
            lines.append(f"    {c.name} {types[c.type]}{null}")
        if dialect == "postgres":
            lines.append(f"    PRIMARY KEY ({', '.join(t.pk)})")
        out.append(f"\nCREATE TABLE {name} (\n" + ",\n".join(lines) + "\n)" +
                   (f"\nCOMMENT '{t.doc}'" if dialect == "spark" and t.doc else "") + ";")
    return "\n".join(out) + "\n"


def spark_schema_json(name):
    t = TABLES[name]
    return {"type": "struct", "fields": [
        {"name": c.name, "type": {"str": "string", "int": "long", "float": "double", "date": "date",
                                  "ts": "timestamp", "bool": "boolean"}[c.type],
         "nullable": c.nullable or c.name not in t.pk, "metadata": {}} for c in t.cols]}


def dictionary_markdown():
    out = ["# Data dictionary", "", "Generated from `plmgen/schema.py`. Do not edit by hand.", ""]
    for source in SOURCES:
        tables = [t for t in TABLES.values() if t.source == source]
        out += [f"## {source}", ""]
        for t in tables:
            out += [f"### `{t.name}`", "", t.doc, "", f"Primary key: `{', '.join(t.pk)}`", ""]
            if t.fks:
                out += ["Foreign keys: " + ", ".join(f"`{c}` → `{target}`" for c, target in t.fks.items()), ""]
            out += ["| Column | Type | Null | Notes |", "|---|---|---|---|"]
            for c in t.cols:
                out.append(f"| `{c.name}` | {c.type} | {'yes' if c.nullable else ''} | {COLUMN_DOCS.get(c.name, '')} |")
            out.append("")
    return "\n".join(out)
