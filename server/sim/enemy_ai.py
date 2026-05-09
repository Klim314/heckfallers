"""Reactive enemy AI.

Two responsibilities, both runnable in isolation:

- ``update_enemy_pressure(world)``: distribute an enemy "budget" across
  contested cells weighted by perceived threat (diver pressure + nearby
  friendly POIs). The result is written into ``cell.enemy_resistance``,
  which the tick loop subtracts from progress.
- ``maybe_spawn_resistance_node(world)``: occasionally drop a Resistance
  Node POI on the most-pressured enemy or contested cell.

v2 will replace the body of these functions with a smarter AI behind the
same interface.
"""
from __future__ import annotations

import random
from typing import TYPE_CHECKING

from .cell import Ownership
from .grid import Coord, neighbors

if TYPE_CHECKING:
    from .world import World


def update_enemy_pressure(world: "World") -> None:
    contested = world.contested_cells()
    if not contested:
        return

    params = world.params

    # "Threat" is computed in SE-force units (same scale as the per-tick
    # rate the sim applies). That way the enemy budget — a fraction of
    # total SE force — counter-pressures cleanly: ratio < 1 lets SE win
    # contested cells; ratio > 1 means the enemy outproduces SE.
    threats: dict[Coord, float] = {}
    for cell in contested:
        force = cell.diver_pressure * params.pressure_coefficient
        for poi in world.pois.values():
            if poi.owner == Ownership.SUPER_EARTH:
                force += poi.effect_on(cell, world)
        threats[cell.coord] = max(0.0, force) + 1e-3

    total_force = sum(threats.values())
    budget = total_force * params.enemy_budget_ratio

    for cell in contested:
        share = threats[cell.coord] / total_force
        cell.enemy_resistance = budget * share


def maybe_spawn_resistance_node(world: "World") -> None:
    candidates = []
    for cell in world.grid.values():
        # Resistance nodes spawn on enemy-defended cells (contested or not).
        if cell.defender != Ownership.ENEMY:
            continue
        if any(p.coord == cell.coord and p.kind == "resistance_node" for p in world.pois.values()):
            continue
        threat = _local_friendly_pressure(world, cell.coord)
        if threat > 0:
            candidates.append((cell.coord, threat))

    if not candidates:
        return

    # Weighted choice toward the most-threatened cell
    candidates.sort(key=lambda x: x[1], reverse=True)
    pick = candidates[: max(1, len(candidates) // 3)]
    coord = random.choice(pick)[0]
    world.place_poi("resistance_node", Ownership.ENEMY, coord)


def _local_friendly_pressure(world: "World", coord: Coord) -> float:
    out = 0.0
    cell = world.grid.get(coord)
    if cell is not None:
        out += cell.diver_pressure
    for n in neighbors(coord):
        nc = world.grid.get(n)
        if nc is None:
            continue
        if nc.defender == Ownership.SUPER_EARTH:
            out += 1.0
        out += nc.diver_pressure * 0.25
    return out
