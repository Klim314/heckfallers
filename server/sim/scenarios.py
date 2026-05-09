"""Scenario loading from JSON files in ``server/scenarios/``.

A scenario fully replaces the world's grid + POIs. Cells not listed in
the scenario are absent; only listed coordinates are part of the front.

Scenario JSON schema:

    {
      "name": "demo_planet",
      "cells": [                           # explicit form
        {"q": 0, "r": 0, "ownership": "se" | "enemy" | "contested", "is_capital": false}
      ],
      "ascii_grid": [                      # OR compact form: rows of chars
        "SSSEEE",                          # 'S'=SE, 'E'=Enemy, '.'=absent
        "SSSEEX"                           # 'C'=SE capital, 'X'=Enemy capital
      ],
      "pois": [
        {"kind": "fob" | "artillery" | "fortress" | "resistance_node",
         "owner": "se" | "enemy",
         "q": 0, "r": 0}
      ],
      "params": { ... optional overrides ... }
    }

Contested cells are derived automatically: any SE/Enemy cell that
borders the opposing faction is converted to Contested at load time, so
scenario authors don't have to maintain the front line by hand.
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

    if "ascii_grid" in data:
        _load_ascii_grid(world, data["ascii_grid"])
    else:
        for c in data.get("cells", []):
            coord: Coord = (int(c["q"]), int(c["r"]))
            ownership = Ownership(c.get("ownership", "se"))
            world.grid[coord] = Cell(
                coord=coord,
                ownership=ownership,
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


_ASCII_TO_OWNERSHIP = {
    "S": (Ownership.SUPER_EARTH, False),
    "E": (Ownership.ENEMY, False),
    "C": (Ownership.SUPER_EARTH, True),
    "X": (Ownership.ENEMY, True),
}


def _load_ascii_grid(world: "World", rows: list[str]) -> None:
    for r, row in enumerate(rows):
        for q, ch in enumerate(row):
            if ch == "." or ch == " ":
                continue
            entry = _ASCII_TO_OWNERSHIP.get(ch.upper())
            if entry is None:
                raise ValueError(f"Unknown grid char {ch!r} at row={r} col={q}")
            ownership, is_capital = entry
            coord: Coord = (q, r)
            world.grid[coord] = Cell(
                coord=coord,
                ownership=ownership,
                is_capital=is_capital,
            )


def _derive_front(world: "World") -> None:
    """Convert SE/Enemy cells bordering the opposing faction to Contested."""
    to_contest: list[Coord] = []
    for coord, cell in world.grid.items():
        if cell.ownership == Ownership.CONTESTED:
            continue
        opposing = Ownership.ENEMY if cell.ownership == Ownership.SUPER_EARTH else Ownership.SUPER_EARTH
        for n in neighbors(coord):
            ncell = world.grid.get(n)
            if ncell is not None and ncell.ownership == opposing:
                to_contest.append(coord)
                break
    for coord in to_contest:
        world.grid[coord].ownership = Ownership.CONTESTED
