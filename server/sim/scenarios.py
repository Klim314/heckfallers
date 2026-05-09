"""Scenario loading from JSON files in ``server/scenarios/``.

A scenario fully replaces the world's grid + POIs. Cells not listed in
the scenario are absent; only listed coordinates are part of the front.

Scenario JSON schema:

    {
      "name": "demo_planet",
      "cells": [                           # explicit form
        {"q": 0, "r": 0, "defender": "se" | "enemy", "is_capital": false}
      ],
      "ascii_grid": [                      # OR compact form: rows of chars
        "SSSEEE",                          # 'S'=SE, 'E'=Enemy, '.'=absent
        "SSSEEX"                           # 'C'=SE capital, 'X'=Enemy capital
      ],
      "hex_disc": {                        # OR procedural giant-hexagon form
        "radius": 5,                       # hex disc radius (cells)
        "se_capital": [-5, 0],             # optional capital coords
        "enemy_capital": [5, 0]
      },
      "pois": [
        {"kind": "fob" | "artillery" | "fortress" | "resistance_node",
         "owner": "se" | "enemy",
         "q": 0, "r": 0}
      ],
      "params": { ... optional overrides ... }
    }

Active incursions are derived automatically: any cell that borders an
opposing-defended cell gets its ``attacker`` set to the opposing
faction at load time, so scenario authors don't have to maintain the
front line by hand.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .cell import Cell, Ownership
from .grid import Coord, neighbors
from .params import SimParams
from .poi import POI

if TYPE_CHECKING:
    from .world import World


def scenarios_dir() -> Path:
    override = os.environ.get("HEXA_SCENARIO_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "scenarios"


def list_scenarios() -> list[str]:
    d = scenarios_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


def load_scenario(world: "World", name: str) -> None:
    path = scenarios_dir() / f"{name}.json"
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    world.grid.clear()
    world.pois.clear()
    world._next_poi_id = 1
    world.scenario_name = data.get("name", name)

    if "params" in data:
        world.params = SimParams()
        world.params.update_from(data["params"])

    if "hex_disc" in data:
        _load_hex_disc(world, data["hex_disc"])
    elif "ascii_grid" in data:
        _load_ascii_grid(world, data["ascii_grid"])
    else:
        for c in data.get("cells", []):
            coord: Coord = (int(c["q"]), int(c["r"]))
            defender = Ownership(c.get("defender", c.get("ownership", "se")))
            world.grid[coord] = Cell(
                coord=coord,
                defender=defender,
                is_capital=bool(c.get("is_capital", False)),
            )

    _derive_front(world)

    for p in data.get("pois", []):
        coord = (int(p["q"]), int(p["r"]))
        world.place_poi(
            kind=p["kind"],
            owner=Ownership(p["owner"]),
            coord=coord,
        )

    # Initial seed POIs emit poi_placed events; drop them so the player
    # event log starts clean rather than full of "match begins" noise.
    world.match_events.clear()


_ASCII_TO_DEFENDER = {
    "S": (Ownership.SUPER_EARTH, False),
    "E": (Ownership.ENEMY, False),
    "C": (Ownership.SUPER_EARTH, True),
    "X": (Ownership.ENEMY, True),
}


def _load_hex_disc(world: "World", config: dict) -> None:
    """Procedurally generate a giant-hexagon grid.

    Cells are split between SE (left) and Enemy (right) by the sign of
    the pointy-top pixel-x discriminator ``2*q + r`` (cells with x<0 go
    SE, x>0 go Enemy, x=0 splits by r so the dividing line alternates
    cleanly down the middle).
    """
    radius = int(config["radius"])
    se_capital = tuple(config["se_capital"]) if config.get("se_capital") else None
    enemy_capital = tuple(config["enemy_capital"]) if config.get("enemy_capital") else None

    for dq in range(-radius, radius + 1):
        for dr in range(max(-radius, -dq - radius), min(radius, -dq + radius) + 1):
            coord: Coord = (dq, dr)
            disc = 2 * dq + dr
            if disc < 0 or (disc == 0 and dr <= 0):
                defender = Ownership.SUPER_EARTH
            else:
                defender = Ownership.ENEMY
            is_capital = coord == se_capital or coord == enemy_capital
            world.grid[coord] = Cell(
                coord=coord,
                defender=defender,
                is_capital=is_capital,
            )


def _load_ascii_grid(world: "World", rows: list[str]) -> None:
    for r, row in enumerate(rows):
        for q, ch in enumerate(row):
            if ch == "." or ch == " ":
                continue
            entry = _ASCII_TO_DEFENDER.get(ch.upper())
            if entry is None:
                raise ValueError(f"Unknown grid char {ch!r} at row={r} col={q}")
            defender, is_capital = entry
            coord: Coord = (q, r)
            world.grid[coord] = Cell(
                coord=coord,
                defender=defender,
                is_capital=is_capital,
            )


def _derive_front(world: "World") -> None:
    """Mark enemy-defended cells that border SE territory as under SE attack.

    v1 is asymmetric: SE is the global attacker on this planet, enemy is the
    defender. SE-defended border cells are NOT auto-contested — enemy doesn't
    initiate incursions in this scenario. Mid-game flips can still create
    enemy-attacker contestations via ``_flip_cell``."""
    for coord, cell in world.grid.items():
        if cell.defender != Ownership.ENEMY:
            continue
        for n in neighbors(coord):
            ncell = world.grid.get(n)
            if ncell is not None and ncell.defender == Ownership.SUPER_EARTH:
                cell.attacker = Ownership.SUPER_EARTH
                break
