"""Reactive enemy AI.

Two responsibilities, both runnable in isolation:

- ``update_enemy_pressure(world)``: stamp a constant defender-resistance
  magnitude on contested cells where ENEMY is the defender. Supply
  scaling (and breakthrough shocks) are applied by the world tick, so
  this stays decoupled from SE pressure — pressure is a real lever, not
  echoed back at the player. Defender POIs (fortress, resistance node)
  layer their own contribution via ``poi.effect_on``. SE-defended
  contested cells are zeroed: ``enemy_resistance`` is consumed in
  ``_apply_pressure`` as a force in the enemy direction, so applying it
  on a cell where SE defends would push the incursion toward capture.
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
    base = world.params.enemy_resistance_base
    for cell in world.contested_cells():
        if cell.defender == Ownership.ENEMY:
            cell.enemy_resistance = base
        else:
            cell.enemy_resistance = 0.0


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
