"""Run configuration and size profiles."""

import json
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

PROFILES = {
    # Fast, for tests.
    "tiny": dict(parts=300, suppliers=20, changes=60, documents=150, history_days=730, assemblies_ratio=0.15),
    # Default for hands-on work.
    "small": dict(parts=5000, suppliers=200, changes=800, documents=3000, history_days=1460, assemblies_ratio=0.12),
    "medium": dict(parts=50000, suppliers=1000, changes=8000, documents=30000, history_days=1460,
                   assemblies_ratio=0.12),
    "large": dict(parts=250000, suppliers=3000, changes=40000, documents=150000, history_days=1460,
                  assemblies_ratio=0.12),
}

PLANTS = [
    ("PLT01", "Detroit Assembly", "US"),
    ("PLT02", "Stuttgart Works", "DE"),
    ("PLT03", "Monterrey Plant", "MX"),
    ("PLT04", "Pune Facility", "IN"),
]


@dataclass
class Config:
    seed: int = 42
    profile: str = "small"
    start: str = ""                 # first simulated day (ISO). Default: as_of - history_days.
    as_of: str = "2026-11-03"       # last simulated day so far
    history_days: int = 1460
    parts: int = 5000               # target number of parts by the end of the history
    suppliers: int = 200
    changes: int = 800              # target number of change notices over the history
    documents: int = 3000           # target number of documents over the history
    assemblies_ratio: float = 0.12  # share of new parts that are assemblies
    bom_depth: int = 10             # maximum BOM levels, leaf included
    min_children: int = 2
    max_children: int = 12
    activity: float = 1.0           # multiplier on arrival rates (for bursts)
    scan_share: float = 0.10        # share of documents delivered as scanned images (slow to render, ~10/s)
    volume_path: str = "/Volumes/plm_poc/raw/project_docs"

    @classmethod
    def from_profile(cls, profile, **overrides):
        values = dict(PROFILES[profile])
        values.update({k: v for k, v in overrides.items() if v is not None})
        cfg = cls(profile=profile, **values)
        if not cfg.start:
            cfg.start = (date.fromisoformat(cfg.as_of) - timedelta(days=cfg.history_days)).isoformat()
        return cfg

    @property
    def start_date(self):
        return date.fromisoformat(self.start)

    @property
    def as_of_date(self):
        return date.fromisoformat(self.as_of)

    # Steady-state arrival rates per day (before the activity multiplier).
    @property
    def parts_per_day(self):
        return self.parts / self.history_days

    @property
    def changes_per_day(self):
        return self.changes / self.history_days

    @property
    def documents_per_day(self):
        return self.documents / self.history_days

    @property
    def projects(self):
        return max(5, self.parts // 125)

    def to_json(self):
        return asdict(self)

    @classmethod
    def from_json(cls, data):
        return cls(**data)

    def validate(self):
        problems = []
        for name, minimum in (("parts", 50), ("suppliers", 3), ("changes", 0), ("documents", 0),
                              ("bom_depth", 2), ("min_children", 1), ("history_days", 365)):
            if getattr(self, name) < minimum:
                problems.append(f"{name} must be at least {minimum}")
        if self.max_children < self.min_children:
            problems.append("max_children must be at least min_children")
        if not 0 <= self.assemblies_ratio <= 1:
            problems.append("assemblies_ratio must be between 0 and 1")
        if self.activity <= 0:
            problems.append("activity must be positive")
        if not 0 <= self.scan_share <= 1:
            problems.append("scan_share must be between 0 and 1")
        try:
            self.start_date, self.as_of_date
        except ValueError:
            problems.append("start and as_of must be ISO dates")
        else:
            if self.as_of_date <= self.start_date:
                problems.append("as_of must be after start")
        return problems


def dump(cfg):
    return json.dumps(cfg.to_json(), indent=2)
