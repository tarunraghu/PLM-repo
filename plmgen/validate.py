"""Check a set of tables for key, time, lifecycle and business-rule violations.

Works on typed rows, either straight from a Database or read back from CSV, so it
checks what the lakehouse will actually load. Returns a report; clean data must
produce zero violations.
"""

from collections import Counter, defaultdict
from datetime import date, datetime, time
from pathlib import Path

from .schema import (ALLOWED_CHILD, EXPORT_RANK, SAP_STATUS, TABLES, WINDCHILL_STATE)
from .db import read_rows
from .util import pn_idx, pn_of, rev_index, rev_label, wt_id_of

# Columns that must never be dated after the as-of date.
PAST_DATE_COLUMNS = [
    ("master_part", "created_date"), ("master_part", "lifecycle_since"),
    ("master_part_lifecycle_history", "effective_from"), ("master_part_lifecycle_history", "effective_to"),
    ("sap_pdm_material", "erdat"), ("sap_pdm_material", "aedat"),
    ("windchill_part", "created_on"), ("windchill_part", "modified_at"),
    ("windchill_part_revision", "effective_from"), ("windchill_part_revision", "effective_to"),
    ("windchill_part_supersession", "effective_on"), ("windchill_change_notice", "created_on"),
    ("windchill_change_notice", "state_since"), ("windchill_cad_document", "uploaded_on"),
    ("part_release_task", "assigned_on"), ("part_release_task", "decided_on"),
    ("part_release_release", "released_on"), ("project_docs_document", "uploaded_on"),
    ("bom_mgmt_ebom_line", "effective_from"), ("bom_mgmt_ebom_line", "effective_to"),
]
# Review can send a part back to Design. Everything else only moves forward.
LEGAL_TRANSITIONS = {("Design", "In Review"), ("In Review", "Design"), ("In Review", "Released"),
                     ("Released", "Obsolete")}


def tables_from_db(db):
    return {name: db.rows(name) for name in TABLES}


def tables_from_dir(directory):
    """Read every table from a folder. Three layouts are understood:
    flat files (state/), one file per table under a source folder, or a folder of part files per table."""
    directory = Path(directory)
    tables = {}
    for name, spec in TABLES.items():
        flat = directory / f"{name}.csv"
        single = directory / spec.source / f"{name}.csv"
        parts = directory / spec.source / name
        if flat.exists():
            tables[name] = list(read_rows(flat, spec))
        elif single.exists():
            tables[name] = list(read_rows(single, spec))
        elif parts.is_dir():
            tables[name] = [row for part in sorted(parts.glob("*.csv")) for row in read_rows(part, spec)]
        else:
            raise FileNotFoundError(f"no data for {name} under {directory}")
    return tables


def _before(day, end_of_day):
    """True when a date or timestamp falls after the end of the as-of day."""
    if isinstance(day, datetime):
        return day > end_of_day
    return day > end_of_day.date()


def validate(t, as_of, max_depth=None):
    pk, unique, fk, structural = {}, {}, {}, {}

    def record(bucket, key, count):
        if count:
            bucket[key] = count

    def values(table, col):
        return {r[col] for r in t[table]}

    # ---- keys ---------------------------------------------------------------------------
    for name, spec in TABLES.items():
        keys = [tuple(r[c] for c in spec.pk) for r in t[name]]
        record(pk, name, len(keys) - len(set(keys)))
        for cols in spec.unique:
            keys = [tuple(r[c] for c in cols) for r in t[name]]
            record(unique, f"{name}({','.join(cols)})", len(keys) - len(set(keys)))

    bm_keys = {r["native_key"] for r in t["master_part_xref"] if r["source_system"] == "bom_mgmt"}
    for name, spec in TABLES.items():
        for col, target in spec.fks.items():
            if target.startswith("xref:"):
                allowed = bm_keys
            else:
                table, table_col = target.split(".")
                allowed = values(table, table_col)
            record(fk, f"{name}.{col} -> {target}",
                   sum(1 for r in t[name] if r[col] is not None and r[col] not in allowed))

    # ---- lookups --------------------------------------------------------------------------
    end_of_day = datetime.combine(as_of, time(23, 59, 59, 999999))
    parts = {p["part_number"]: p for p in t["master_part"]}
    materials = {m["matnr"]: m for m in t["sap_pdm_material"]}
    wt_parts = {p["wt_id"]: p for p in t["windchill_part"]}
    bm_to_part = {r["native_key"]: r["part_number"] for r in t["master_part_xref"] if r["source_system"] == "bom_mgmt"}
    part_to_bm = {part: key for key, part in bm_to_part.items()}
    changes = {c["change_number"]: c for c in t["windchill_change_notice"]}
    history = defaultdict(list)
    for h in t["master_part_lifecycle_history"]:
        history[h["part_number"]].append(h)
    for rows in history.values():
        rows.sort(key=lambda h: h["effective_from"])
    revisions = defaultdict(list)
    for r in t["windchill_part_revision"]:
        revisions[r["wt_id"]].append(r)
    for rows in revisions.values():
        rows.sort(key=lambda r: rev_index(r["version"]))
    rev_by_key = {(r["wt_id"], r["version"]): r for r in t["windchill_part_revision"]}

    def state_on(pn, day):
        for h in history[pn]:
            if h["effective_from"] <= day and (h["effective_to"] is None or day < h["effective_to"]):
                return h["lifecycle_state"]
        return None

    def effective_rev(wt, day):
        for r in reversed(revisions[wt]):   # latest first: on a changeover day the new revision wins
            if r["effective_from"] <= day and (r["effective_to"] is None or day <= r["effective_to"]):
                return r
        return None

    # ---- no future dates --------------------------------------------------------------------
    future = sum(1 for table, col in PAST_DATE_COLUMNS for r in t[table]
                 if r[col] is not None and _before(r[col], end_of_day))
    record(structural, "records dated after the as-of date", future)

    # ---- cross-reference and attribute agreement ----------------------------------------------
    for src, table, col in (("sap_pdm", "sap_pdm_material", "matnr"), ("windchill", "windchill_part", "wt_id")):
        existing = {str(v) for v in values(table, col)}
        record(structural, f"master_part_xref {src} keys missing in {table}",
               sum(1 for r in t["master_part_xref"] if r["source_system"] == src and r["native_key"] not in existing))
    existing = values("sap_pdm_vendor", "lifnr")
    record(structural, "master_supplier_xref keys missing in sap_pdm_vendor",
           sum(1 for r in t["master_supplier_xref"] if r["native_key"] not in existing))
    covered = {(r["part_number"], r["source_system"]) for r in t["master_part_xref"]}
    record(structural, "parts without a cross-reference in every source", sum(
        1 for pn in parts if any((pn, s) not in covered for s in ("sap_pdm", "windchill", "bom_mgmt"))))

    sap_key = {r["part_number"]: r["native_key"] for r in t["master_part_xref"] if r["source_system"] == "sap_pdm"}
    lifnr_of = {r["supplier_id"]: r["native_key"] for r in t["master_supplier_xref"]}
    wt_of = {r["part_number"]: int(r["native_key"]) for r in t["master_part_xref"] if r["source_system"] == "windchill"}
    primaries = defaultdict(list)
    for r in t["master_supplier_part"]:
        if r["is_primary"]:
            primaries[r["part_number"]].append(r)
    record(structural, "parts without exactly one approved primary supplier matching the master", sum(
        1 for pn, p in parts.items()
        if len(primaries[pn]) != 1 or primaries[pn][0]["supplier_id"] != p["primary_supplier_id"]
        or primaries[pn][0]["approval_status"] != "Approved"))
    bad_sap = bad_state_map = bad_created = 0
    for pn, p in parts.items():
        m = materials.get(sap_key.get(pn))
        w = wt_parts.get(wt_of.get(pn))
        if m is None or w is None:
            continue
        factor = 0.001 if m["gewei"] == "G" else 1.0
        bad_sap += (m["estat"] != SAP_STATUS[p["canonical_lifecycle"]]
                    or abs(m["mass_value"] * factor - p["mass_kg"]) > 1e-9
                    or m["lifnr"] != lifnr_of.get(p["primary_supplier_id"]))
        bad_state_map += w["state"] != WINDCHILL_STATE[p["canonical_lifecycle"]]
        bad_created += (m["erdat"] != p["created_date"] or w["created_on"] != p["created_date"])
    record(structural, "SAP material disagrees with the master (status, mass or supplier)", bad_sap)
    record(structural, "Windchill state disagrees with the master lifecycle", bad_state_map)
    record(structural, "creation dates differ between master, SAP and Windchill", bad_created)

    # ---- lifecycle history ------------------------------------------------------------------
    bad_history = 0
    for pn, p in parts.items():
        rows = history[pn]
        chain = all(rows[k]["effective_to"] == rows[k + 1]["effective_from"] for k in range(len(rows) - 1))
        legal = all((rows[k]["lifecycle_state"], rows[k + 1]["lifecycle_state"]) in LEGAL_TRANSITIONS
                    for k in range(len(rows) - 1))
        ok = (bool(rows) and chain and legal and rows[0]["effective_from"] == p["created_date"]
              and rows[0]["lifecycle_state"] == "Design" and rows[-1]["effective_to"] is None
              and rows[-1]["lifecycle_state"] == p["canonical_lifecycle"]
              and rows[-1]["effective_from"] == p["lifecycle_since"])
        bad_history += not ok
    record(structural, "parts with a broken or illegal lifecycle history", bad_history)

    # ---- revisions ----------------------------------------------------------------------------
    bad_revisions = 0
    for pn, p in parts.items():
        wt = wt_of.get(pn)
        rows = revisions[wt]
        labels_ok = [r["version"] for r in rows] == [rev_label(k) for k in range(len(rows))]
        chain_ok = all(rows[k]["effective_to"] == rows[k + 1]["effective_from"] for k in range(len(rows) - 1))
        current = [r for r in rows if r["is_current"]]
        ok = (bool(rows) and labels_ok and chain_ok and len(current) == 1 and current[0] is rows[-1]
              and rows[-1]["effective_to"] is None and rows[0]["effective_from"] == p["created_date"]
              and wt_parts[wt]["version"] == rows[-1]["version"])
        bad_revisions += not ok
    record(structural, "parts with a broken revision history", bad_revisions)

    # ---- supersession -----------------------------------------------------------------------
    superseded = {s["old_wt_id"]: s for s in t["windchill_part_supersession"]}
    bad_super = 0
    for s in t["windchill_part_supersession"]:
        old, new = parts[pn_of(s["old_wt_id"] - 1_000_000)], parts[pn_of(s["new_wt_id"] - 1_000_000)]
        bad_super += (old["canonical_lifecycle"] != "Obsolete" or s["old_wt_id"] == s["new_wt_id"]
                      or s["effective_on"] != old["lifecycle_since"] or s["effective_on"] < new["created_date"])
    record(structural, "supersessions that are invalid or dated wrongly", bad_super)
    record(structural, "Obsolete parts without a supersession", sum(
        1 for pn, p in parts.items() if p["canonical_lifecycle"] == "Obsolete" and wt_of[pn] not in superseded))

    # ---- EBOM -------------------------------------------------------------------------------
    lines = t["bom_mgmt_ebom_line"]
    active = [r for r in lines if r["effective_to"] is None]
    pairs = Counter((r["parent_item_key"], r["child_item_key"]) for r in active)
    record(structural, "parent-child pairs with more than one active ebom line", sum(1 for n in pairs.values() if n > 1))
    record(structural, "ebom lines that end before they start", sum(
        1 for r in lines if r["effective_to"] is not None and r["effective_to"] < r["effective_from"]))
    adjacency, indegree, nodes = defaultdict(list), Counter(), set()
    for r in active:
        if r["child_item_key"] in bm_keys and r["parent_item_key"] in bm_keys:
            adjacency[r["parent_item_key"]].append(r["child_item_key"])
            indegree[r["child_item_key"]] += 1
            nodes.update((r["parent_item_key"], r["child_item_key"]))
    queue = [n for n in sorted(nodes) if indegree[n] == 0]
    removed, depth_of = 0, {n: 0 for n in queue}
    while queue:
        node = queue.pop()
        removed += 1
        for child in adjacency[node]:
            depth_of[child] = max(depth_of.get(child, 0), depth_of[node] + 1)
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    record(structural, "ebom cycles (nodes left after topological sort)", len(nodes) - removed)
    if max_depth is not None:
        record(structural, "ebom paths deeper than the configured depth",
               sum(1 for d in depth_of.values() if d > max_depth - 1))

    bad_lifecycle = bad_dates = 0
    for r in lines:
        p, c = bm_to_part.get(r["parent_item_key"]), bm_to_part.get(r["child_item_key"])
        if p is None or c is None:
            continue
        bad_dates += r["effective_from"] < max(parts[p]["created_date"], parts[c]["created_date"])
        if r["effective_to"] is None:
            bad_lifecycle += parts[c]["canonical_lifecycle"] not in ALLOWED_CHILD[parts[p]["canonical_lifecycle"]]
    record(structural, "active ebom lines whose child lifecycle the parent does not allow", bad_lifecycle)
    record(structural, "ebom lines effective before both parts exist", bad_dates)

    bad_eff = 0
    for r in t["bom_mgmt_ebom_line_effectivity"]:
        if r["effectivity_type"] == "DATE":
            try:
                ok = date.fromisoformat(r["from_value"]) <= date.fromisoformat(r["to_value"])
            except ValueError:
                ok = False
        elif r["effectivity_type"] == "SERIAL":
            ok = r["from_value"].isdigit() and r["to_value"].isdigit() and int(r["from_value"]) <= int(r["to_value"])
        else:
            ok = False
        bad_eff += not ok
    record(structural, "effectivity rows with bad type or values", bad_eff)

    by_id = {r["ebom_line_id"]: r for r in lines}
    bad_mbom = 0
    for m in t["bom_mgmt_mbom_line"]:
        if m["source_ebom_line_id"] is None:
            continue
        e = by_id.get(m["source_ebom_line_id"])
        bad_mbom += (e is None or e["effective_to"] is not None or e["parent_item_key"] != m["parent_item_key"]
                     or e["child_item_key"] != m["child_item_key"])
    record(structural, "mbom lines that do not match an active source ebom line", bad_mbom)

    # ---- changes ----------------------------------------------------------------------------
    items = defaultdict(list)
    for a in t["windchill_change_affected_item"]:
        items[a["change_number"]].append(a)
    tasks = defaultdict(list)
    for task in t["part_release_task"]:
        tasks[task["change_number"]].append(task)
    for rows in tasks.values():
        rows.sort(key=lambda x: x["step"])
    releases = {(r["change_number"], r["wt_id"]): r for r in t["part_release_release"]}

    not_released = before_part = bad_before = 0
    spans = defaultdict(list)
    for cn, change in changes.items():
        created = change["created_on"]
        for a in items[cn]:
            pn = pn_of(a["wt_id"] - 1_000_000)
            not_released += state_on(pn, created) != "Released"
            before_part += created < parts[pn]["created_date"]
            rev = rev_by_key.get((a["wt_id"], a["before_version"]))
            bad_before += rev is None or effective_rev(a["wt_id"], created) is not rev
            end = date.max if change["state"] in ("Open", "In Approval", "Approved") else change["state_since"]
            spans[a["wt_id"]].append((created, end, cn))
    record(structural, "change affected items on parts that were not Released on the change date", not_released)
    record(structural, "changes dated before an affected part exists", before_part)
    record(structural, "change before-versions that were not the effective revision on the change date", bad_before)
    overlap = 0
    for rows in spans.values():
        rows.sort()
        overlap += sum(1 for k in range(len(rows) - 1) if rows[k][1] > rows[k + 1][0])
    record(structural, "parts with overlapping changes", overlap)
    record(structural, "changes with no affected items", sum(1 for cn in changes if not items[cn]))

    bad_tasks = 0
    for cn, change in changes.items():
        rows = tasks.get(cn, [])
        decisions = [x["decision"] for x in rows]
        state = change["state"]
        ok = {
            "Open": not rows,
            "In Approval": bool(rows) and decisions[-1] == "Pending" and set(decisions[:-1]) <= {"Approved"},
            "Approved": len(rows) >= 2 and set(decisions) == {"Approved"},
            "Implemented": len(rows) >= 2 and set(decisions) == {"Approved"},
            "Rejected": bool(rows) and decisions[-1] == "Rejected" and set(decisions[:-1]) <= {"Approved"},
        }[state]
        if ok and rows:
            ok = rows[0]["assigned_on"] >= change["created_on"] and [x["step"] for x in rows] == list(range(1, len(rows) + 1))
            for k, x in enumerate(rows):
                if x["decision"] != "Pending":
                    ok = ok and x["decided_on"] is not None and x["decided_on"] >= x["assigned_on"]
                if k + 1 < len(rows):
                    ok = ok and rows[k + 1]["assigned_on"] == x["decided_on"]
        bad_tasks += not ok
    record(structural, "approval tasks inconsistent with the change state or dates", bad_tasks)

    bad_release = missing_release = extra_release = 0
    for cn, change in changes.items():
        if change["state"] == "Implemented":
            last = max((x["decided_on"] for x in tasks.get(cn, []) if x["decided_on"]), default=None)
            for a in items[cn]:
                rel = releases.get((cn, a["wt_id"]))
                if rel is None:
                    missing_release += 1
                    continue
                rev = rev_by_key.get((a["wt_id"], rel["released_version"]))
                bad_release += (rel["released_on"] != change["state_since"] or rel["released_version"] != a["after_version"]
                                or rev is None or rev["effective_from"] != rel["released_on"]
                                or last is None or rel["released_on"] < last)
    extra_release = sum(1 for (cn, _) in releases if changes[cn]["state"] != "Implemented")
    record(structural, "implemented change items without a release", missing_release)
    record(structural, "releases for changes that are not implemented", extra_release)
    record(structural, "releases dated wrongly or not matching a new revision", bad_release)

    # ---- change BOM deltas ------------------------------------------------------------------
    by_pair = defaultdict(list)
    for r in lines:
        by_pair[(r["parent_item_key"], r["child_item_key"])].append(r)

    def active_on(key, day):
        return [r for r in by_pair.get(key, []) if r["effective_from"] <= day
                and (r["effective_to"] is None or day < r["effective_to"])]

    bad_delta = 0
    for d in t["windchill_change_bom_delta"]:
        change = changes[d["change_number"]]
        key = (part_to_bm.get(d["parent_part_number"]), part_to_bm.get(d["child_part_number"]))
        created, kind = change["created_on"], d["delta_type"]
        baseline = active_on(key, created)
        ok = (max(parts[d["parent_part_number"]]["created_date"], parts[d["child_part_number"]]["created_date"])
              <= created)
        if kind == "ADDED":
            ok = ok and not baseline and d["old_quantity"] is None and d["new_quantity"]
        else:
            ok = ok and len(baseline) == 1 and baseline[0]["quantity"] == d["old_quantity"]
        implemented = change["state"] == "Implemented"
        if ok and (d["status"] == "Proposed") == implemented:
            ok = False
        if ok and d["status"] == "Applied":
            day = change["state_since"]
            history_lines = by_pair.get(key, [])
            if kind == "ADDED":
                ok = any(r["effective_from"] == day and r["quantity"] == d["new_quantity"] for r in history_lines)
            elif kind == "REMOVED":
                ok = any(r["effective_to"] == day for r in history_lines)
            else:
                ok = (any(r["effective_to"] == day and r["quantity"] == d["old_quantity"] for r in history_lines)
                      and any(r["effective_from"] == day and r["quantity"] == d["new_quantity"] for r in history_lines))
        bad_delta += not ok
    record(structural, "change BOM deltas inconsistent with the BOM on the change date or their status", bad_delta)

    # ---- CAD and documents ------------------------------------------------------------------
    record(structural, "CAD documents uploaded before the part exists", sum(
        1 for c in t["windchill_cad_document"]
        if c["uploaded_on"] < parts[pn_of(c["wt_id"] - 1_000_000)]["created_date"]))
    doc_links = defaultdict(list)
    for link in t["project_docs_document_part_link"]:
        doc_links[link["document_id"]].append(link)
    bad_doc_class = bad_doc_date = bad_doc_rev = bad_doc_path = 0
    for doc in t["project_docs_document"]:
        links = doc_links[doc["document_id"]]
        classes = [parts[l["part_number"]]["export_control_class"] for l in links] or ["EAR99"]
        bad_doc_class += doc["export_control_class"] != max(classes, key=EXPORT_RANK.get)
        bad_doc_path += not doc["file_path"].endswith(f"{doc['document_id']}.{doc['file_format']}")
        for l in links:
            bad_doc_date += doc["uploaded_on"] < parts[l["part_number"]]["created_date"]
            rev = effective_rev(wt_of[l["part_number"]], doc["uploaded_on"])
            bad_doc_rev += rev is None or rev["version"] != l["part_revision"]
    record(structural, "documents whose export class is not the most restrictive of their parts", bad_doc_class)
    record(structural, "documents uploaded before a part they link to exists", bad_doc_date)
    record(structural, "document links whose revision was not effective at upload", bad_doc_rev)
    record(structural, "documents whose file_path does not match id and format", bad_doc_path)
    record(structural, "documents with no part links", sum(1 for d in t["project_docs_document"] if not doc_links[d["document_id"]]))

    conversions = {(r["from_uom"], r["to_uom"]) for r in t["master_uom_conversion"]}
    record(structural, "gram-based materials without a G to KG conversion",
           sum(1 for m in t["sap_pdm_material"] if m["gewei"] == "G" and ("G", "KG") not in conversions))

    report = {"primary_key": pk, "unique": unique, "foreign_key": fk, "structural": structural}
    report["total_violations"] = sum(sum(bucket.values()) for bucket in (pk, unique, fk, structural))
    return report
