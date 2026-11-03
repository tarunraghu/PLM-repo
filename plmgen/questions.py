"""Ground-truth questions with known answers, computed from the true state.

Use them to check Silver and Gold tables, to benchmark a Genie space or a RAG agent, and to
test that parsing recovers what documents say. Answers come from the database, never from the
documents or from any dirty extract.

Each line of questions.jsonl: id, category, question, answer, answer_type, tables, as_of.
"""

import json
from collections import Counter, defaultdict
from pathlib import Path

from .schema import IN_FLIGHT_STATES
from .util import entity_rng, idx_from_bom_key, pn_idx, pn_of, wt_id_of

PER_CATEGORY = 12


def _active_children(db):
    children = defaultdict(dict)
    for line in db.rows("bom_mgmt_ebom_line"):
        if line["effective_to"] is None:
            children[pn_of(idx_from_bom_key(line["parent_item_key"]))][pn_of(idx_from_bom_key(line["child_item_key"]))] = \
                line["quantity"]
    return children


def _explode(children, root):
    """Distinct descendants and the total quantity of each needed to build one root."""
    totals, stack = Counter(), [(root, 1)]
    while stack:
        node, qty = stack.pop()
        for child, per in sorted(children.get(node, {}).items()):
            totals[child] += qty * per
            stack.append((child, qty * per))
    return totals


def _height(children, node, memo):
    if node not in memo:
        memo[node] = 0 if not children.get(node) else 1 + max(_height(children, c, memo) for c in children[node])
    return memo[node]


def build_questions(db, cfg, doc_facts=None, per_category=PER_CATEGORY):
    g = entity_rng(cfg.seed, "questions", cfg.as_of)
    parts = {p["part_number"]: p for p in db.rows("master_part")}
    names = sorted(parts, key=pn_idx)
    children = _active_children(db)
    parents = defaultdict(list)
    for parent, kids in children.items():
        for child in kids:
            parents[child].append(parent)
    suppliers = {s["supplier_id"]: s for s in db.rows("master_supplier")}
    plant_of = {m["matnr"]: m["werks"] for m in db.rows("sap_pdm_material")}
    history = defaultdict(list)
    for h in db.rows("master_part_lifecycle_history"):
        history[h["part_number"]].append(h)
    revisions = defaultdict(list)
    for r in db.rows("windchill_part_revision"):
        revisions[r["wt_id"]].append(r)
    changes = {c["change_number"]: c for c in db.rows("windchill_change_notice")}
    affected = defaultdict(list)
    by_part_changes = defaultdict(list)
    for a in db.rows("windchill_change_affected_item"):
        pn = pn_of(a["wt_id"] - 1_000_000)
        affected[a["change_number"]].append(pn)
        by_part_changes[pn].append(a["change_number"])
    links = defaultdict(list)
    for link in db.rows("project_docs_document_part_link"):
        links[link["part_number"]].append(link["document_id"])
    docs = {d["document_id"]: d for d in db.rows("project_docs_document")}
    supplier_rows = defaultdict(list)
    for r in db.rows("master_supplier_part"):
        supplier_rows[r["part_number"]].append(r)
    superseded = {s["old_wt_id"]: s["new_wt_id"] for s in db.rows("windchill_part_supersession")}

    questions = []

    def add(category, question, answer, tables):
        kind = "dict" if isinstance(answer, dict) else "list" if isinstance(answer, list) else "scalar"
        questions.append({"id": f"Q{len(questions) + 1:04d}", "category": category, "question": question,
                          "answer": answer, "answer_type": kind, "tables": tables, "as_of": cfg.as_of})

    def pick(pool, n):
        pool = list(pool)
        return [pool[g.randrange(len(pool))] for _ in range(min(n, len(pool)))] if pool else []

    def distinct(pool, n):
        pool, out = list(pool), []
        while pool and len(out) < n:
            out.append(pool.pop(g.randrange(len(pool))))
        return out

    # Lifecycle and revision.
    for pn in distinct(names, per_category):
        add("part_lifecycle", f"What is the current lifecycle state of part {pn}?", parts[pn]["canonical_lifecycle"],
            ["master_part"])
    for pn in distinct([p for p in names if len(history[p]) >= 2], per_category):
        rows = history[pn]
        row = rows[g.randrange(len(rows) - 1)]
        day = row["effective_from"]
        add("lifecycle_history", f"What was the lifecycle state of part {pn} on {day.isoformat()}?",
            row["lifecycle_state"], ["master_part_lifecycle_history"])
    for pn in distinct(names, per_category):
        add("current_revision", f"What is the current revision of part {pn}?",
            [r for r in revisions[wt_id_of(pn_idx(pn))] if r["is_current"]][0]["version"],
            ["windchill_part", "windchill_part_revision"])
    for pn in distinct([p for p in names if len(revisions[wt_id_of(pn_idx(p))]) >= 2], per_category):
        rows = revisions[wt_id_of(pn_idx(pn))]
        row = rows[g.randrange(len(rows) - 1)]
        add("revision_history", f"Which revision of part {pn} was effective on {row['effective_from'].isoformat()}?",
            row["version"], ["windchill_part_revision"])

    # BOM.
    for pn in distinct([p for p in names if len(parents[p]) >= 1], per_category):
        add("where_used", f"Which assemblies directly contain part {pn} in the current engineering BOM?",
            sorted(parents[pn]), ["bom_mgmt_ebom_line", "master_part_xref"])
    assemblies = [p for p in names if children.get(p)]
    for pn in distinct(assemblies, per_category):
        add("direct_children", f"List the direct children of {pn} in the current engineering BOM with quantities.",
            {c: q for c, q in sorted(children[pn].items())}, ["bom_mgmt_ebom_line", "master_part_xref"])
    memo = {}
    deep = [p for p in assemblies if _height(children, p, memo) >= 3]
    for pn in distinct(deep or assemblies, per_category):
        add("bom_explosion", f"How many distinct parts are in the full multi-level BOM of {pn}?",
            len(_explode(children, pn)), ["bom_mgmt_ebom_line", "master_part_xref"])
        add("bom_depth", f"How many levels does the BOM of {pn} have, counting {pn} as level 1?",
            _height(children, pn, memo) + 1, ["bom_mgmt_ebom_line"])
    for pn in distinct(deep or assemblies, per_category):
        totals = _explode(children, pn)
        if totals:
            child = sorted(totals)[g.randrange(len(totals))]
            add("rolled_up_quantity", f"What total quantity of part {child} is needed to build one {pn}?",
                totals[child], ["bom_mgmt_ebom_line"])

    # Changes.
    for cn in distinct(sorted(changes), per_category):
        add("change_affected_parts", f"Which parts does change {cn} affect?", sorted(affected[cn]),
            ["windchill_change_affected_item"])
        add("change_state", f"What is the state of change {cn}?", changes[cn]["state"], ["windchill_change_notice"])
    implemented = {cn for cn, c in changes.items() if c["state"] == "Implemented"}
    for pn in distinct([p for p in names if any(cn in implemented for cn in by_part_changes[p])], per_category):
        add("part_changes", f"Which implemented changes have touched part {pn}?",
            sorted(cn for cn in by_part_changes[pn] if cn in implemented), ["windchill_change_affected_item",
                                                                           "windchill_change_notice"])
    add("in_flight_changes", "Which change notices are currently in flight (Open, In Approval or Approved)?",
        sorted(cn for cn, c in changes.items() if c["state"] in IN_FLIGHT_STATES), ["windchill_change_notice"])

    # Supersession and suppliers.
    obsolete = [p for p in names if wt_id_of(pn_idx(p)) in superseded]
    for pn in distinct(obsolete, per_category):
        add("obsolete_replacement", f"Which part replaced obsolete part {pn}?",
            pn_of(superseded[wt_id_of(pn_idx(pn))] - 1_000_000), ["windchill_part_supersession"])
    for pn in distinct(names, per_category):
        primary = [r for r in supplier_rows[pn] if r["is_primary"]][0]
        add("primary_supplier", f"Who is the primary supplier of part {pn}?",
            suppliers[primary["supplier_id"]]["supplier_name"], ["master_supplier_part", "master_supplier"])
        add("approved_suppliers", f"How many approved suppliers does part {pn} have?",
            sum(1 for r in supplier_rows[pn] if r["approval_status"] == "Approved"), ["master_supplier_part"])

    # Documents and export control.
    for pn in distinct([p for p in names if links[p]], per_category):
        add("documents_for_part", f"How many documents reference part {pn}?", len(links[pn]),
            ["project_docs_document_part_link"])
        add("controlled_documents", f"Which documents referencing part {pn} are export controlled (not EAR99)?",
            sorted(d for d in links[pn] if docs[d]["export_control_class"] != "EAR99"), ["project_docs_document"])
    class_counts = Counter(d["export_control_class"] for d in docs.values())
    add("export_control", "How many documents are classified ITAR-USML-XIX?", class_counts.get("ITAR-USML-XIX", 0),
        ["project_docs_document"])

    # Aggregates.
    add("lifecycle_counts", "How many parts are in each lifecycle state?",
        dict(sorted(Counter(p["canonical_lifecycle"] for p in parts.values()).items())), ["master_part"])
    add("change_state_counts", "How many change notices are in each state?",
        dict(sorted(Counter(c["state"] for c in changes.values()).items())), ["windchill_change_notice"])
    add("plant_counts", "How many parts are made at each plant?",
        dict(sorted(Counter(plant_of[m] for m in plant_of).items())), ["sap_pdm_material"])
    primary_lead = [r["lead_time_days"] for rows in supplier_rows.values() for r in rows if r["is_primary"]]
    add("average_lead_time", "What is the average lead time in days across primary supplier relationships, to one decimal?",
        round(sum(primary_lead) / len(primary_lead), 1) if primary_lead else None, ["master_supplier_part"])

    # Document contents: what the files themselves say.
    if doc_facts:
        usable = [f for f in doc_facts if not f.get("defect") and f["format"] in ("pdf", "docx")]
        for kind, field, template in (
                ("Spec", "material", "According to {doc}, what material is part {pn} made of?"),
                ("Spec", "finish", "According to {doc}, what finish does part {pn} have?"),
                ("Drawing", "scale", "According to {doc}, what is the drawing scale for part {pn}?"),
                ("Certificate", "lot", "According to {doc}, what lot number was certified for part {pn}?"),
                ("Work Instruction", "torque_nm", "According to {doc}, what torque in Nm applies to part {pn}?")):
            for f in distinct([f for f in usable if f["document_type"] == kind], per_category // 2):
                part = f["parts"][g.randrange(len(f["parts"]))]
                add("document_content", template.format(doc=f["document_id"], pn=part["part_number"]), part[field],
                    ["project_docs_document"])
        for f in distinct([f for f in usable if f["document_type"] == "Test Report"], per_category):
            add("document_content", f"According to {f['document_id']}, what is the overall test result?",
                f["overall_result"], ["project_docs_document"])
    return questions


def write_questions(path, questions):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for q in questions:
            fh.write(json.dumps(q, separators=(",", ":")) + "\n")


def read_doc_facts(path):
    """Read doc_facts.jsonl, keeping the latest record per document."""
    path = Path(path)
    if not path.exists():
        return []
    latest = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            latest[record["document_id"]] = record
    return [latest[k] for k in sorted(latest)]
