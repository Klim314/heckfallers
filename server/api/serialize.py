"""Wire-format serialization for World → JSON snapshots."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..sim.world import World


def world_to_wire(world: "World") -> dict:
    return {
        "tick": world.tick,
        "elapsed_s": round(world.elapsed_s, 2),
        "match_state": world.match_state,
        "speed": world.speed,
        "scenario_name": world.scenario_name,
        "params": world.params.to_dict(),
        "stats": world.stats(),
        "cells": [c.to_wire() for c in world.grid.values()],
        "pois": [p.to_wire() for p in world.pois.values()],
        "salients": [s.to_wire() for s in world.salients.values()],
        "controller": getattr(world.controller, "name", "unknown"),
        "se_controller": getattr(world.se_controller, "name", "unknown"),
        "requisition": round(getattr(world.se_controller, "requisition", 0.0), 2),
        "match_events": list(world.match_events),
    }
