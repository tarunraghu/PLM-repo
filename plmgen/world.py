"""The simulation: one day of PLM business activity at a time.

A World wraps a Database and applies rules day by day: parts are created and move
through their lifecycle, assemblies are built, changes go through approval and are
implemented, documents arrive, and master data drifts. The same code builds the
initial history and every later increment.

Determinism: every random draw comes either from a per-day stream or from a stream
keyed on one entity (see util.entity_rng). Nothing depends on how many days were run
before, so "history to day N, then N+k" equals "history to day N+k", and a World
rebuilt from saved tables behaves exactly like the one that wrote them. To keep this
true: never iterate a set or dict for a decision without sorting, and derive every
schedule from persisted facts (lifecycle_since, state_since, task dates).
"""

import heapq
import random
from collections import defaultdict
from datetime import timedelta

from .config import PLANTS
from .schema import (ALLOWED_CHILD, EXPORT_CLASSES, EXPORT_RANK, IN_FLIGHT_STATES, SAP_STATUS, WINDCHILL_STATE)
from .util import (CAD_FORMATS, COMPONENTS, COUNTRIES, DOC_TYPES, PART_SUFFIXES, PROJECT_NAMES, QTY_CHOICES, ROLES,
                   SUPPLIER_STEMS, SUPPLIER_SUFFIX, UNITS, bom_key, entity_rng, idx_from_bom_key, pn_idx, pn_of,
                   poisson, rev_label, sap_lifnr, sap_matnr, seq_of, supplier_id, supplier_idx, wt_id_of)

PLANT_CODES = [p[0] for p in PLANTS]
UOM = [("EA", "Each", "count"), ("KG", "Kilogram", "mass"), ("G", "Gram", "mass"),
       ("M", "Metre", "length"), ("L", "Litre", "volume")]
UOM_CONVERSIONS = [("EA", "EA", 1.0), ("KG", "KG", 1.0), ("G", "KG", 0.001), ("M", "M", 1.0), ("L", "L", 1.0)]
OBSOLETE_SHARE = 0.12   # share of parts that will eventually be obsoleted


class World:
    def __init__(self, db, cfg):
        self.db = db
        self.cfg = cfg
        self.seed = cfg.seed
        self.doc_jobs = []
        self.rebuild()

    # ---- derived state (rebuilt from tables, so a loaded database behaves like a live one) ----
    def rebuild(self):
        db = self.db
        self.parts_order = sorted((r["part_number"] for r in db.rows("master_part")), key=pn_idx)
        self.children = defaultdict(dict)    # active lines: parent -> {child: line_id}
        self.parents = defaultdict(dict)     # active lines: child -> {parent: line_id}
        parent_set = set()
        for line in db.rows("bom_mgmt_ebom_line"):
            parent = pn_of(idx_from_bom_key(line["parent_item_key"]))
            parent_set.add(parent)
            if line["effective_to"] is None:
                child = pn_of(idx_from_bom_key(line["child_item_key"]))
                self.children[parent][child] = line["ebom_line_id"]
                self.parents[child][parent] = line["ebom_line_id"]
        self.asm_order = sorted(parent_set, key=pn_idx)   # parts that have ever been an assembly
        self.asm_set = set(self.asm_order)
        self.height = {}
        for pn in self.parts_order:
            self._height(pn)
        self.in_flight = set()
        for change in db.rows("windchill_change_notice"):
            if change["state"] in IN_FLIGHT_STATES:
                for item in db.find("windchill_change_affected_item", "by_change", change["change_number"]):
                    self.in_flight.add(pn_of(item["wt_id"] - 1_000_000))
        self.counters = {
            "ebom": self._max_seq("bom_mgmt_ebom_line", "ebom_line_id"),
            "mbom": self._max_seq("bom_mgmt_mbom_line", "mbom_line_id"),
            "task": self._max_seq("part_release_task", "task_id"),
            "release": self._max_seq("part_release_release", "release_id"),
            "change": self._max_seq("windchill_change_notice", "change_number"),
            "doc": self._max_seq("project_docs_document", "document_id"),
            "cad": self._max_seq("windchill_cad_document", "cad_doc_id"),
            "supplier": self._max_seq("master_supplier", "supplier_id"),
        }
        self.next_part_idx = (pn_idx(self.parts_order[-1]) + 1) if self.parts_order else 1
        self.heap = []
        for pn in self.parts_order:
            self._schedule_part(pn)
        for change in db.rows("windchill_change_notice"):
            self._schedule_change(change["change_number"])

    def _max_seq(self, table, col):
        rows = self.db.rows(table)
        return max((seq_of(r[col]) for r in rows), default=0)

    def _height(self, pn):
        """Longest chain of active BOM lines below a part (a leaf is 0)."""
        if pn in self.height:
            return self.height[pn]
        kids = self.children.get(pn)
        self.height[pn] = 0 if not kids else 1 + max(self._height(c) for c in sorted(kids))
        return self.height[pn]

    def nid(self, kind):
        self.counters[kind] += 1
        return self.counters[kind]

    def erng(self, *key):
        return entity_rng(self.seed, *key)

    def state(self, pn):
        return self.db.get("master_part", pn)["canonical_lifecycle"]

    # ---- bootstrap -------------------------------------------------------------------------
    def bootstrap(self, day):
        db = self.db
        db.begin_day(day, self.erng(day, "clock"))
        for code, name, dimension in UOM:
            db.insert("master_uom", {"uom_code": code, "uom_name": name, "dimension": dimension})
        for src, dst, factor in UOM_CONVERSIONS:
            db.insert("master_uom_conversion", {"from_uom": src, "to_uom": dst, "factor": factor})
        for code, name, country in PLANTS:
            db.insert("master_plant", {"plant_code": code, "plant_name": name, "country": country})
        for _ in range(self.cfg.suppliers):
            self.add_supplier()
        for j in range(1, self.cfg.projects + 1):
            g = self.erng("project", j)
            db.insert("project_docs_project", {
                "project_id": f"PRJ-{j:03d}", "project_name": f"{g.choice(PROJECT_NAMES)} {j}",
                "program_code": f"PGM-{g.randint(1, 12):02d}"})

    def add_supplier(self):
        n = self.nid("supplier")
        g = self.erng("supplier", n)
        name = f"{g.choice(SUPPLIER_STEMS)} {g.choice(SUPPLIER_SUFFIX)} {n}"
        country = g.choice(COUNTRIES)
        self.db.insert("master_supplier", {"supplier_id": supplier_id(n), "supplier_name": name,
                                           "country": country, "status": "Active"})
        self.db.insert("master_supplier_xref", {"supplier_id": supplier_id(n), "source_system": "sap_pdm",
                                                "native_key": sap_lifnr(n)})
        self.db.insert("sap_pdm_vendor", {"lifnr": sap_lifnr(n), "name1": name, "land1": country})

    # ---- one day ---------------------------------------------------------------------------
    def step_day(self, day):
        db, cfg = self.db, self.cfg
        db.begin_day(day, self.erng(day, "clock"))
        arr = self.erng(day, "arrivals")
        mult = cfg.activity
        n_cur = len(self.parts_order)
        counts = {
            "supplier": poisson(arr, 0.02 * mult),
            "part": poisson(arr, cfg.parts_per_day * mult),
            "change": poisson(arr, cfg.changes_per_day * mult),
            "doc": poisson(arr, cfg.documents_per_day * mult),
            "mass": poisson(arr, n_cur * 0.0006 * mult),
            "supplier_change": poisson(arr, n_cur * 0.0002 * mult),
            "avl": poisson(arr, n_cur * 0.0004 * mult),
            "reload": poisson(arr, n_cur * 0.00005 * mult),
            "churn": poisson(arr, len(self.asm_order) * 0.003 * mult),
            "mbom_only": poisson(arr, len(self.asm_order) * 0.002 * mult),
        }
        for _ in range(counts["supplier"]):
            self.add_supplier()

        due_parts, due_changes = set(), set()
        while self.heap and self.heap[0][0] <= day:
            _, kind, key = heapq.heappop(self.heap)
            (due_parts if kind == "part" else due_changes).add(key)
        for pn in sorted(due_parts, key=pn_idx):
            self.process_part(pn, day)
        for cn in sorted(due_changes):
            self.advance_change(cn, day)

        for i in range(counts["part"]):
            self.create_part(day)
        for i in range(counts["change"]):
            self.create_change(day, self.erng(day, "change", i))
        for i in range(counts["churn"]):
            self.churn_bom(day, self.erng(day, "churn", i))
        for i in range(counts["mbom_only"]):
            self.add_mbom_only(day, self.erng(day, "mbom_only", i))
        for i in range(counts["mass"]):
            self.update_mass(day, self.erng(day, "mass", i))
        for i in range(counts["supplier_change"]):
            self.change_primary_supplier(day, self.erng(day, "supplier_change", i))
        for i in range(counts["avl"]):
            self.approve_alternate(self.erng(day, "avl", i))
        for i in range(counts["reload"]):
            self.reload_material(self.erng(day, "reload", i))
        for i in range(counts["doc"]):
            self.create_document(day, self.erng(day, "doc", i))

    # ---- parts -----------------------------------------------------------------------------
    def create_part(self, day):
        db = self.db
        idx = self.next_part_idx
        self.next_part_idx += 1
        g = self.erng("part", idx)
        pn = pn_of(idx)
        name = f"{g.choice(COMPONENTS)} {g.choice(PART_SUFFIXES)}"
        master_system = g.choices(["sap_pdm", "windchill"], weights=[40, 60])[0]
        unit = g.choice(UNITS)
        mass = round(g.uniform(0.02, 40.0), 3)
        sup = 1 + int(g.random() * db.count("master_supplier"))
        plant = g.choice(PLANT_CODES)
        mtart = g.choice(["ROH", "HALB", "FERT"])
        export = g.choices(EXPORT_CLASSES, weights=[90, 8, 2])[0]
        is_assembly = g.random() < self.cfg.assemblies_ratio
        n_alt = g.randint(0, 2)
        lead = g.randint(5, 90)

        db.insert("master_part", {
            "part_number": pn, "part_name": name, "master_system": master_system,
            "canonical_lifecycle": "Design", "lifecycle_since": day, "unit_of_measure": unit, "mass_kg": mass,
            "primary_supplier_id": supplier_id(sup), "export_control_class": export, "created_date": day})
        db.insert("master_part_lifecycle_history", {
            "part_number": pn, "lifecycle_state": "Design", "effective_from": day, "effective_to": None})
        for source, native in (("sap_pdm", sap_matnr(idx)), ("windchill", str(wt_id_of(idx))),
                               ("bom_mgmt", bom_key(idx))):
            db.insert("master_part_xref", {"part_number": pn, "source_system": source, "native_key": native})
        db.insert("master_supplier_part", {"supplier_id": supplier_id(sup), "part_number": pn,
                                           "approval_status": "Approved", "is_primary": True,
                                           "lead_time_days": lead})
        used = {sup}
        for _ in range(n_alt):
            alt = 1 + int(g.random() * db.count("master_supplier"))
            if alt in used:
                continue
            used.add(alt)
            db.insert("master_supplier_part", {
                "supplier_id": supplier_id(alt), "part_number": pn,
                "approval_status": g.choice(["Approved", "Pending"]), "is_primary": False,
                "lead_time_days": g.randint(7, 120)})
        db.insert("sap_pdm_material", {
            "matnr": sap_matnr(idx), "maktx": name, "mtart": mtart, "meins": unit, "gewei": "KG",
            "mass_value": mass, "estat": SAP_STATUS["Design"], "werks": plant, "lifnr": sap_lifnr(sup),
            "erdat": day, "aedat": day})
        db.insert("windchill_part", {
            "wt_id": wt_id_of(idx), "number": pn, "name": name, "version": rev_label(0),
            "state": WINDCHILL_STATE["Design"], "created_on": day})
        db.insert("windchill_part_revision", {
            "wt_id": wt_id_of(idx), "version": rev_label(0), "is_current": True,
            "effective_from": day, "effective_to": None})

        if g.random() < 0.6:
            for k in range(1, g.randint(1, 2) + 1):
                fmt = g.choice(sorted(CAD_FORMATS))
                n = self.nid("cad")
                db.insert("windchill_cad_document", {
                    "cad_doc_id": f"CAD-{n:07d}", "wt_id": wt_id_of(idx), "file_name": f"{pn}_{k}{CAD_FORMATS[fmt]}",
                    "cad_format": fmt, "uploaded_on": day})

        if is_assembly:
            kids = set()
            for _ in range(g.randint(self.cfg.min_children, self.cfg.max_children)):
                child = self.pick_child(g, pn, kids)
                if child:
                    kids.add(child)
            if kids:
                for child in sorted(kids):
                    self.add_line(pn, child, day, g)
                self.asm_order.append(pn)
                self.asm_set.add(pn)
        self.parts_order.append(pn)
        self._schedule_part(pn)

    def part_due(self, pn):
        row = self.db.get("master_part", pn)
        state, since = row["canonical_lifecycle"], row["lifecycle_since"]
        if state == "Design":
            return since + timedelta(days=self.erng(pn, "dwell", state, since).randint(10, 60))
        if state == "In Review":
            return since + timedelta(days=self.erng(pn, "dwell", state, since).randint(5, 30))
        if state == "Released":
            g = self.erng(pn, "obsolete", since)
            if g.random() < OBSOLETE_SHARE:
                return since + timedelta(days=g.randint(200, 1000))
        return None

    def _schedule_part(self, pn):
        due = self.part_due(pn)
        if due is not None:
            heapq.heappush(self.heap, (due, "part", pn))

    def process_part(self, pn, day):
        due = self.part_due(pn)
        if due is None or due > day:
            return
        state = self.state(pn)
        kid_states = {self.state(c) for c in sorted(self.children.get(pn, {}))}
        if state == "Design" and kid_states <= {"Released", "In Review"}:
            self.set_state(pn, "In Review", day)
        elif state == "In Review" and kid_states <= {"Released"}:
            self.set_state(pn, "Released", day)
        elif state == "Released" and self.obsolete(pn, day):
            pass
        else:
            heapq.heappush(self.heap, (day + timedelta(days=1), "part", pn))

    def set_state(self, pn, new_state, day):
        db = self.db
        idx = pn_idx(pn)
        db.update("master_part", pn, canonical_lifecycle=new_state, lifecycle_since=day)
        history = db.find("master_part_lifecycle_history", "by_part", pn)
        open_row = [h for h in history if h["effective_to"] is None][0]
        db.update("master_part_lifecycle_history", (pn, open_row["effective_from"]), effective_to=day)
        db.insert("master_part_lifecycle_history", {"part_number": pn, "lifecycle_state": new_state,
                                                    "effective_from": day, "effective_to": None})
        db.update("sap_pdm_material", sap_matnr(idx), estat=SAP_STATUS[new_state], aedat=day)
        db.update("windchill_part", wt_id_of(idx), state=WINDCHILL_STATE[new_state])
        self._schedule_part(pn)

    def obsolete(self, pn, day):
        """Obsolete a Released part by replacing it, in every active parent, with another Released part."""
        if pn in self.in_flight:
            return False
        g = entity_rng(self.seed, day, "obsolete", pn)
        parents = sorted(p for p in self.parents.get(pn, {}) if self.state(p) != "Obsolete")
        replacement = None
        for _ in range(25):
            y = self.parts_order[g.randrange(len(self.parts_order))]
            if y == pn or self.state(y) != "Released":
                continue
            if all(self._can_repoint(p, y) for p in parents):
                replacement = y
                break
        if replacement is None:
            return False
        for parent in parents:
            line = self.db.get("bom_mgmt_ebom_line", self.children[parent][pn])
            quantity = line["quantity"]
            self.end_line(parent, pn, day)
            if replacement not in self.children.get(parent, {}):
                self.add_line(parent, replacement, day, g, quantity=quantity)
        self.set_state(pn, "Obsolete", day)
        self.db.insert("windchill_part_supersession", {
            "old_wt_id": wt_id_of(pn_idx(pn)), "new_wt_id": wt_id_of(pn_idx(replacement)), "effective_on": day})
        return True

    def _can_repoint(self, parent, replacement):
        if replacement == parent:
            return False
        if replacement in self.children.get(parent, {}):
            return True
        if self.parents.get(parent) and self._reaches(replacement, parent):
            return False
        return self._height(replacement) + 1 + self._up(parent) <= self.cfg.bom_depth - 1

    # ---- BOM -------------------------------------------------------------------------------
    def _reaches(self, source, target):
        seen, stack = set(), [source]
        while stack:
            node = stack.pop()
            if node == target:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(sorted(self.children.get(node, {})))
        return False

    def _up(self, pn, memo=None):
        memo = {} if memo is None else memo
        if pn in memo:
            return memo[pn]
        parents = self.parents.get(pn)
        memo[pn] = 0 if not parents else 1 + max(self._up(p, memo) for p in sorted(parents))
        return memo[pn]

    def ok_edge(self, parent, child):
        if child == parent or child in self.children.get(parent, {}):
            return False
        if self.state(child) not in ALLOWED_CHILD[self.state(parent)]:
            return False
        if self.parents.get(parent) and self._reaches(child, parent):
            return False
        return self._height(child) + 1 + self._up(parent) <= self.cfg.bom_depth - 1

    def pick_child(self, g, parent, exclude):
        """A child that fits the parent's lifecycle and the depth limit, favouring older and shared parts."""
        for _ in range(40):
            pool = self.asm_order if (self.asm_order and g.random() < 0.45) else self.parts_order
            if not pool:
                return None
            child = pool[int(len(pool) * g.random() ** 2)]
            if child in exclude or child == parent:
                continue
            if self.ok_edge(parent, child):
                return child
        return None

    def add_line(self, parent, child, day, g, quantity=None):
        db = self.db
        n = self.nid("ebom")
        line_id = f"EBOM-{n:07d}"
        quantity = quantity if quantity is not None else g.choice(QTY_CHOICES)
        db.insert("bom_mgmt_ebom_line", {
            "ebom_line_id": line_id, "parent_item_key": bom_key(pn_idx(parent)),
            "child_item_key": bom_key(pn_idx(child)), "quantity": quantity, "find_number": g.randint(10, 990),
            "effective_from": day, "effective_to": None})
        roll = g.random()
        if roll < 0.15:
            start = g.randint(1, 4000)
            db.insert("bom_mgmt_ebom_line_effectivity", {
                "ebom_line_id": line_id, "seq": 1, "effectivity_type": "SERIAL",
                "from_value": str(start), "to_value": str(start + g.randint(100, 2000))})
        elif roll < 0.25:
            window = day + timedelta(days=g.randint(0, 365))
            db.insert("bom_mgmt_ebom_line_effectivity", {
                "ebom_line_id": line_id, "seq": 1, "effectivity_type": "DATE", "from_value": window.isoformat(),
                "to_value": (window + timedelta(days=g.randint(180, 720))).isoformat()})
        if g.random() < 0.85:
            m = self.nid("mbom")
            db.insert("bom_mgmt_mbom_line", {
                "mbom_line_id": f"MBOM-{m:07d}", "parent_item_key": bom_key(pn_idx(parent)),
                "child_item_key": bom_key(pn_idx(child)), "quantity": quantity,
                "plant_code": g.choice(PLANT_CODES), "source_ebom_line_id": line_id})
        self.children[parent][child] = line_id
        self.parents[child][parent] = line_id
        self._refresh_height(parent)
        return line_id

    def _refresh_height(self, start):
        """Recompute a part's height from its active children, and its ancestors' if it changed.
        Exact in both directions, so a live run and a reloaded one always agree."""
        stack = [start]
        while stack:
            p = stack.pop()
            h = max((self._height(c) + 1 for c in sorted(self.children.get(p, {}))), default=0)
            if h != self.height.get(p):
                self.height[p] = h
                stack.extend(sorted(self.parents.get(p, {})))

    def end_line(self, parent, child, day):
        line_id = self.children[parent].pop(child)
        self.parents[child].pop(parent)
        self.db.update("bom_mgmt_ebom_line", line_id, effective_to=day)
        for m in self.db.find("bom_mgmt_mbom_line", "by_source", line_id):
            self.db.delete("bom_mgmt_mbom_line", m["mbom_line_id"])
        self._refresh_height(parent)

    def churn_bom(self, day, g):
        """Design-state assemblies are edited freely, without change control."""
        if not self.asm_order:
            return
        for _ in range(10):
            parent = self.asm_order[g.randrange(len(self.asm_order))]
            if self.state(parent) == "Design":
                break
        else:
            return
        kids = sorted(self.children.get(parent, {}))
        if kids and g.random() < 0.4:
            self.end_line(parent, kids[g.randrange(len(kids))], day)
        else:
            child = self.pick_child(g, parent, set())
            if child:
                self.add_line(parent, child, day, g)

    def add_mbom_only(self, day, g):
        """A manufacturing-only component (tooling, consumables) with no EBOM line."""
        if not self.asm_order or not self.parts_order:
            return
        parent = self.asm_order[g.randrange(len(self.asm_order))]
        child = self.parts_order[g.randrange(len(self.parts_order))]
        if child == parent or self.state(child) == "Obsolete":
            return
        m = self.nid("mbom")
        self.db.insert("bom_mgmt_mbom_line", {
            "mbom_line_id": f"MBOM-{m:07d}", "parent_item_key": bom_key(pn_idx(parent)),
            "child_item_key": bom_key(pn_idx(child)), "quantity": 1, "plant_code": g.choice(PLANT_CODES),
            "source_ebom_line_id": None})

    # ---- changes ---------------------------------------------------------------------------
    def change_plan(self, cn):
        g = self.erng(cn, "plan")
        steps = g.randint(2, 4)
        roles = g.sample(ROLES, steps)
        reject_step = g.randint(1, steps) if g.random() < 0.15 else None
        return steps, roles, reject_step

    def change_due(self, cn):
        row = self.db.get("windchill_change_notice", cn)
        state, since = row["state"], row["state_since"]
        if state == "Open":
            return since + timedelta(days=self.erng(cn, "open").randint(1, 3))
        if state == "In Approval":
            pending = [t for t in self.db.find("part_release_task", "by_change", cn) if t["decision"] == "Pending"]
            task = pending[0]
            return task["assigned_on"] + timedelta(days=self.erng(cn, "step", task["step"]).randint(1, 4))
        if state == "Approved":
            return since + timedelta(days=self.erng(cn, "approved").randint(2, 10))
        return None

    def _schedule_change(self, cn):
        due = self.change_due(cn)
        if due is not None:
            heapq.heappush(self.heap, (due, "change", cn))

    def create_change(self, day, g):
        db = self.db
        want = g.randint(1, 3)
        picked = []
        for _ in range(60):
            if len(picked) == want or not self.parts_order:
                break
            pn = self.parts_order[g.randrange(len(self.parts_order))]
            if pn in picked or pn in self.in_flight or self.state(pn) != "Released":
                continue
            picked.append(pn)
        if not picked:
            return
        n = self.nid("change")
        cn = f"ECO-{n:06d}"
        db.insert("windchill_change_notice", {
            "change_number": cn, "title": f"Change request {n}", "state": "Open", "state_since": day,
            "created_on": day})
        for pn in picked:
            revisions = db.find("windchill_part_revision", "by_part", wt_id_of(pn_idx(pn)))
            current = [r for r in revisions if r["is_current"]][0]
            db.insert("windchill_change_affected_item", {
                "change_number": cn, "wt_id": wt_id_of(pn_idx(pn)), "before_version": current["version"],
                "after_version": rev_label(len(revisions))})
            self.in_flight.add(pn)

        if g.random() < 0.5 and self.asm_order:
            seen = set()
            for _ in range(g.randint(1, 3)):
                kind = g.choice(["ADDED", "REMOVED", "QTY_CHANGED"])
                for _attempt in range(20):
                    parent = self.asm_order[g.randrange(len(self.asm_order))]
                    if kind == "ADDED":
                        child = self.pick_child(g, parent, set())
                        if child is None:
                            continue
                        old_qty, new_qty = None, g.choice([1, 2, 4])
                    else:
                        kids = sorted(self.children.get(parent, {}))
                        if not kids:
                            continue
                        child = kids[g.randrange(len(kids))]
                        old = db.get("bom_mgmt_ebom_line", self.children[parent][child])["quantity"]
                        if kind == "REMOVED":
                            old_qty, new_qty = old, None
                        else:
                            old_qty, new_qty = old, g.choice([q for q in QTY_CHOICES if q != old])
                    if (parent, child) in seen:
                        continue
                    seen.add((parent, child))
                    db.insert("windchill_change_bom_delta", {
                        "change_number": cn, "parent_part_number": parent, "child_part_number": child,
                        "delta_type": kind, "old_quantity": old_qty, "new_quantity": new_qty,
                        "status": "Proposed"})
                    break
        self._schedule_change(cn)

    def new_task(self, cn, step, day):
        _, roles, _ = self.change_plan(cn)
        n = self.nid("task")
        self.db.insert("part_release_task", {
            "task_id": f"TSK-{n:07d}", "change_number": cn, "step": step, "approver_role": roles[step - 1],
            "decision": "Pending", "assigned_on": day, "decided_on": None})

    def _release_parts(self, cn):
        for item in self.db.find("windchill_change_affected_item", "by_change", cn):
            self.in_flight.discard(pn_of(item["wt_id"] - 1_000_000))

    def advance_change(self, cn, day):
        due = self.change_due(cn)
        if due is None or due > day:
            return
        db = self.db
        state = db.get("windchill_change_notice", cn)["state"]
        steps, _, reject_step = self.change_plan(cn)
        if state == "Open":
            db.update("windchill_change_notice", cn, state="In Approval", state_since=day)
            self.new_task(cn, 1, day)
        elif state == "In Approval":
            task = [t for t in db.find("part_release_task", "by_change", cn) if t["decision"] == "Pending"][0]
            if reject_step == task["step"]:
                db.update("part_release_task", task["task_id"], decision="Rejected", decided_on=day)
                db.update("windchill_change_notice", cn, state="Rejected", state_since=day)
                self._release_parts(cn)
                return
            db.update("part_release_task", task["task_id"], decision="Approved", decided_on=day)
            if task["step"] < steps:
                self.new_task(cn, task["step"] + 1, day)
            else:
                db.update("windchill_change_notice", cn, state="Approved", state_since=day)
        elif state == "Approved":
            self.implement_change(cn, day)
            return
        self._schedule_change(cn)

    def implement_change(self, cn, day):
        db = self.db
        for item in db.find("windchill_change_affected_item", "by_change", cn):
            wt = item["wt_id"]
            n = self.nid("release")
            db.insert("part_release_release", {
                "release_id": f"REL-{n:07d}", "change_number": cn, "wt_id": wt,
                "released_version": item["after_version"], "released_on": day})
            current = [r for r in db.find("windchill_part_revision", "by_part", wt) if r["is_current"]][0]
            db.update("windchill_part_revision", (wt, current["version"]), is_current=False, effective_to=day)
            db.insert("windchill_part_revision", {"wt_id": wt, "version": item["after_version"],
                                                  "is_current": True, "effective_from": day, "effective_to": None})
            db.update("windchill_part", wt, version=item["after_version"])
        for delta in db.find("windchill_change_bom_delta", "by_change", cn):
            self._apply_delta(cn, delta, day)
        db.update("windchill_change_notice", cn, state="Implemented", state_since=day)
        self._release_parts(cn)

    def _apply_delta(self, cn, delta, day):
        parent, child = delta["parent_part_number"], delta["child_part_number"]
        g = self.erng(cn, "apply", parent, child)
        status = "Skipped"
        if delta["delta_type"] == "ADDED":
            if parent in self.asm_set and self.ok_edge(parent, child):
                self.add_line(parent, child, day, g, quantity=delta["new_quantity"])
                status = "Applied"
        elif child in self.children.get(parent, {}):
            line = self.db.get("bom_mgmt_ebom_line", self.children[parent][child])
            if delta["delta_type"] == "REMOVED":
                self.end_line(parent, child, day)
                status = "Applied"
            elif line["quantity"] == delta["old_quantity"]:
                self.end_line(parent, child, day)
                self.add_line(parent, child, day, g, quantity=delta["new_quantity"])
                status = "Applied"
        self.db.update("windchill_change_bom_delta", (cn, parent, child), status=status)

    # ---- master data drift -----------------------------------------------------------------
    def update_mass(self, day, g):
        if not self.parts_order:
            return
        pn = self.parts_order[g.randrange(len(self.parts_order))]
        idx = pn_idx(pn)
        mass = round(self.db.get("master_part", pn)["mass_kg"] * g.uniform(0.9, 1.1), 3)
        self.db.update("master_part", pn, mass_kg=mass)
        self.db.update("sap_pdm_material", sap_matnr(idx), mass_value=mass, aedat=day)

    def change_primary_supplier(self, day, g):
        if not self.parts_order:
            return
        pn = self.parts_order[g.randrange(len(self.parts_order))]
        rows = self.db.find("master_supplier_part", "by_part", pn)
        primary = [r for r in rows if r["is_primary"]][0]
        alternates = [r for r in rows if not r["is_primary"] and r["approval_status"] == "Approved"]
        if not alternates:
            return
        new = alternates[g.randrange(len(alternates))]
        self.db.update("master_supplier_part", (primary["supplier_id"], pn), is_primary=False)
        self.db.update("master_supplier_part", (new["supplier_id"], pn), is_primary=True)
        self.db.update("master_part", pn, primary_supplier_id=new["supplier_id"])
        self.db.update("sap_pdm_material", sap_matnr(pn_idx(pn)),
                       lifnr=sap_lifnr(supplier_idx(new["supplier_id"])), aedat=day)

    def approve_alternate(self, g):
        if not self.parts_order:
            return
        pn = self.parts_order[g.randrange(len(self.parts_order))]
        pending = [r for r in self.db.find("master_supplier_part", "by_part", pn)
                   if r["approval_status"] == "Pending"]
        if pending:
            row = pending[g.randrange(len(pending))]
            self.db.update("master_supplier_part", (row["supplier_id"], pn), approval_status="Approved")

    def reload_material(self, g):
        """A material is deleted and recreated in SAP: the CDC stream carries a delete then an insert."""
        if not self.parts_order:
            return
        pn = self.parts_order[g.randrange(len(self.parts_order))]
        key = sap_matnr(pn_idx(pn))
        row = dict(self.db.get("sap_pdm_material", key))
        self.db.delete("sap_pdm_material", key)
        self.db.insert("sap_pdm_material", row)

    # ---- documents -------------------------------------------------------------------------
    def create_document(self, day, g):
        db = self.db
        if not self.parts_order:
            return
        k = g.randint(1, 3)
        linked = []
        for _ in range(k * 3):
            pn = self.parts_order[g.randrange(len(self.parts_order))]
            if pn not in linked:
                linked.append(pn)
            if len(linked) == k:
                break
        n = self.nid("doc")
        doc_id = f"DOC-{n:07d}"
        doc_type = g.choice(DOC_TYPES)
        scan = self.cfg.scan_share
        fmt = g.choices(["pdf", "docx", "png"], weights=[(1 - scan) * 0.78, (1 - scan) * 0.22, scan])[0]
        export = max((db.get("master_part", pn)["export_control_class"] for pn in linked), key=EXPORT_RANK.get)
        project = f"PRJ-{1 + g.randrange(db.count('project_docs_project')):03d}"
        doc = db.insert("project_docs_document", {
            "document_id": doc_id, "project_id": project, "document_type": doc_type, "title": doc_id,
            "file_format": fmt, "file_path": f"{self.cfg.volume_path}/{doc_id}.{fmt}", "uploaded_on": day,
            "uploaded_by": f"user{g.randint(1, 200):03d}", "export_control_class": export})
        snapshots = []
        for pn in linked:
            part = db.get("master_part", pn)
            revision = [r for r in db.find("windchill_part_revision", "by_part", wt_id_of(pn_idx(pn)))
                        if r["is_current"]][0]["version"]
            db.insert("project_docs_document_part_link", {"document_id": doc_id, "part_number": pn,
                                                          "part_revision": revision})
            supplier = db.get("master_supplier", part["primary_supplier_id"])
            snapshots.append({
                "part_number": pn, "part_name": part["part_name"], "revision": revision,
                "lifecycle": part["canonical_lifecycle"], "mass_kg": part["mass_kg"],
                "unit": part["unit_of_measure"], "supplier": supplier["supplier_name"],
                "plant": db.get("sap_pdm_material", sap_matnr(pn_idx(pn)))["werks"],
                "export_control_class": part["export_control_class"]})
        title = f"{doc_type} {doc_id} for {linked[0]} rev {snapshots[0]['revision']}"
        db.update("project_docs_document", doc_id, title=title)
        self.doc_jobs.append({"document_id": doc_id, "document_type": doc_type, "format": fmt,
                              "uploaded_on": day.isoformat(), "uploaded_by": doc["uploaded_by"],
                              "project_id": project, "title": title, "export_control_class": export,
                              "parts": snapshots})
