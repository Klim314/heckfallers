"""Supply computation for the asymmetric model.

Two functions, one per side, called from the world tick loop. Both write
into ``Cell.enemy_supply`` / ``Cell.se_supply`` (range 0..1). Only the
contested-cell apply step in ``world._apply_pressure`` actually consumes
these values; non-contested cells carry computed values purely for
visualization.

- Defender (enemy): multi-source BFS from enemy capitals + fortress POIs
  over enemy-owned and contested cells. Hop distance maps linearly to
  supply via the ``supply_max_depth`` param. Cells unreachable through
  same-side territory get supply 0 — they're cut off.

- Attacker (SE): local same-faction-neighbor density within
  ``attacker_density_radius`` plus a FOB-proximity bonus. No BFS:
  orbital projection isn't path-based, so density is the right shape.
"""
from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from .cell import Ownership
from .grid import Coord, cells_within, distance, neighbors

if TYPE_CHECKING:
    from .world import World


def recompute_enemy_supply(world: "World") -> None:
    sources: list[Coord] = []
    for cell in world.grid.values():
        if cell.is_capital and cell.defender == Ownership.ENEMY:
            sources.append(cell.coord)
    for poi in world.pois.values():
        if poi.kind == "fortress" and poi.owner == Ownership.ENEMY:
            sources.append(poi.coord)

    max_depth = max(1, world.params.supply_max_depth)

    dist: dict[Coord, int] = {}
    queue: deque[Coord] = deque()
    for coord in sources:
        # A source must be on an enemy-defended cell.
        cell = world.grid.get(coord)
        if cell is None or cell.defender != Ownership.ENEMY:
            continue
        if coord not in dist:
            dist[coord] = 0
            queue.append(coord)

    while queue:
        coord = queue.popleft()
        d = dist[coord]
        if d >= max_depth:
            continue
        for n in neighbors(coord):
            if n in dist:
                continue
            ncell = world.grid.get(n)
            if ncell is None:
                continue
            # Supply flows through enemy-defended cells (contested or not).
            if ncell.defender != Ownership.ENEMY:
                continue
            dist[n] = d + 1
            queue.append(n)

    for coord, cell in world.grid.items():
        d = dist.get(coord)
        if d is None:
            cell.enemy_supply = 0.0
        else:
            cell.enemy_supply = max(0.0, 1.0 - d / max_depth)


def recompute_se_supply(world: "World") -> None:
    params = world.params
    radius = max(1, params.attacker_density_radius)
    # Hex disc of given radius has 3r(r+1)+1 cells; subtract self.
    max_neighbors = 3 * radius * (radius + 1)
    fob_bonus = params.fob_supply_bonus
    fob_radius = params.fob_radius

    fobs: list[Coord] = [
        p.coord for p in world.pois.values()
        if p.kind == "fob" and p.owner == Ownership.SUPER_EARTH
    ]

    for coord, cell in world.grid.items():
        count = 0
        for c in cells_within(coord, radius):
            if c == coord:
                continue
            nc = world.grid.get(c)
            if nc is not None and nc.defender == Ownership.SUPER_EARTH:
                count += 1
        density = count / max_neighbors if max_neighbors > 0 else 0.0

        bonus = 0.0
        for fob_coord in fobs:
            if distance(coord, fob_coord) <= fob_radius:
                bonus = fob_bonus
                break

        cell.se_supply = min(1.0, density + bonus)


def recompute_all(world: "World") -> None:
    recompute_enemy_supply(world)
    recompute_se_supply(world)


def effective_enemy_supply(cell, tick: int) -> float:
    """Apply supply shock at read time. Used by tick loop."""
    if tick < cell.supply_shock_until:
        return 0.0
    return cell.enemy_supply
