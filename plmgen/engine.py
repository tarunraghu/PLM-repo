"""Run the simulation and manage a workspace on disk.

Workspace layout (the root is chosen with --workspace):
  state/           the current truth: one typed CSV per table, plus manifest.json
  out/base/        the initial extract that source systems would deliver
  out/batches/     one folder per advanced day (events, delta extracts, document files)
  truth/           ground truth for testing: questions, document facts
"""

import json
from datetime import timedelta
from pathlib import Path

from .config import Config
from .db import Database
from .world import World

MANIFEST = "manifest.json"
FORMAT_VERSION = 1


def daterange(first, last):
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)


class Workspace:
    def __init__(self, root):
        self.root = Path(root)
        self.state = self.root / "state"
        self.out = self.root / "out"
        self.truth = self.root / "truth"

    def exists(self):
        return (self.state / MANIFEST).exists()

    # ---- manifest -----------------------------------------------------------------------
    def read_manifest(self):
        return json.loads((self.state / MANIFEST).read_text(encoding="utf-8"))

    def write_manifest(self, manifest):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # ---- state --------------------------------------------------------------------------
    def save(self, db, cfg, manifest):
        db.save(self.state)
        manifest = {**manifest, "version": FORMAT_VERSION, "config": cfg.to_json()}
        self.write_manifest(manifest)

    def load(self):
        manifest = self.read_manifest()
        cfg = Config.from_json(manifest["config"])
        db = Database.load(self.state)
        return db, World(db, cfg), cfg, manifest


def simulate_history(cfg, on_day=None):
    """Run the whole history from the start date to the as-of date."""
    db = Database()
    world = World(db, cfg)
    world.bootstrap(cfg.start_date)
    for day in daterange(cfg.start_date, cfg.as_of_date):
        world.step_day(day)
        if on_day:
            on_day(day, db.drain())
        else:
            db.drain()
    return db, world


def simulate_days(world, first, last, on_day=None):
    """Advance an existing world over [first, last]."""
    for day in daterange(first, last):
        world.step_day(day)
        changes = world.db.drain()
        if on_day:
            on_day(day, changes)
