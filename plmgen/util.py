"""Small helpers shared across modules: keys, labels, random draws."""

import math
import random

SUPPLIER_STEMS = ["Apex", "Northwind", "Meridian", "Vector", "Ironclad", "Harbor", "Summit"]
SUPPLIER_SUFFIX = ["Industries", "Components", "Precision", "Systems"]
COUNTRIES = ["US", "DE", "MX", "CN", "IN", "JP"]
COMPONENTS = ["Bracket", "Housing", "Gear", "Shaft", "Bearing", "PCB", "Harness",
              "Sensor", "Fastener", "Gasket", "Cover", "Motor", "Valve", "Panel"]
PART_SUFFIXES = ["A", "B", "C", "X1", "X2"]
ROLES = ["Design Engineer", "Quality", "Manufacturing", "Supplier Quality", "Program Manager"]
DOC_TYPES = ["Spec", "Drawing", "Test Report", "Certificate", "Work Instruction"]
PROJECT_NAMES = ["Atlas", "Borealis", "Cascade", "Delta", "Equinox", "Frontier"]
CAD_FORMATS = {"CATIA": ".CATPart", "NX": ".prt", "STEP": ".stp", "PDF": ".pdf"}
UNITS = ["EA", "KG", "M", "L"]
QTY_CHOICES = [1, 1, 2, 4, 6, 10]
MATERIALS = ["Aluminium 6061", "Stainless Steel 304", "Carbon Steel 1045", "Polycarbonate", "Nylon 66",
             "Copper C110", "Titanium Grade 5", "ABS"]
FINISHES = ["Anodised", "Zinc plated", "Powder coated", "Passivated", "None"]


def pn_idx(part_number):
    return int(part_number[2:]) - 100000


def pn_of(idx):
    return f"P-{100000 + idx}"


def sap_matnr(idx):
    return f"{100000 + idx:018d}"


def sap_lifnr(idx):
    return f"{idx:010d}"


def supplier_id(idx):
    return f"SUP-{idx:05d}"


def supplier_idx(sid):
    return int(sid[4:])


def wt_id_of(idx):
    return 1_000_000 + idx


def bom_key(idx):
    return f"BM-{idx:07d}"


def idx_from_bom_key(key):
    return int(key[3:])


def rev_label(k):
    """Revision label for the k-th revision: A.1, B.1, ... Z.1, AA.1, ..."""
    label, k = "", k + 1
    while k:
        k, rem = divmod(k - 1, 26)
        label = chr(65 + rem) + label
    return f"{label}.1"


def rev_index(label):
    """Inverse of rev_label: A.1 -> 0, Z.1 -> 25, AA.1 -> 26."""
    k = 0
    for ch in label.split(".")[0]:
        k = k * 26 + (ord(ch) - 64)
    return k - 1


def seq_of(identifier):
    """Numeric part of ids like ECO-000012 or EBOM-0000123."""
    return int(identifier.rsplit("-", 1)[1])


def poisson(rng, lam):
    """Poisson draw. Knuth for small means, normal approximation for large ones."""
    if lam <= 0:
        return 0
    if lam > 40:
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def entity_rng(seed, *key):
    """A random stream that depends only on the seed and the entity, never on run history."""
    return random.Random(":".join(str(part) for part in (seed,) + key))
