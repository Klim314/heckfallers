"""SE side AI.

The diver agent abstracts the playerbase as a constant pool of force
that gets distributed across the front each allocation tick. Cells the
user has pinned (via the controller pressure slider) consume from the
pool first; the rest is shared via softmax(utility / temperature) over
the remaining SE-attacker contested cells.

Utility currently combines four pieces:

- completion: cells closer to flipping get more divers (finish the job)
- weakness:   cells with low enemy supply are easier wins
- frontline:  cells with more SE-defended neighbors are reachable / safer
- siege:      cells under fortress siege multiplier are deprioritized

Future iterations will layer cohort-typed allocation (heavy / objective /
support) per the design doc, but v1 collapses everything into one pool.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

from .cell import Cell, Ownership
from .grid import cells_within, neighbors

if TYPE_CHECKING:
    from .world import World


def allocate_divers(world: "World") -> None:
    targets = [c for c in world.grid.values()
               if c.attacker == Ownership.SUPER_EARTH]
    if not targets:
        return

    params = world.params

    # Cut-off filter: a contested cell can only be reinforced if some held
    # SE-defended cell sits within `diver_supply_max_hops`. Beyond that the
    # cell is isolated — divers can't reach it. Force pressure to 0 and
    # release any stale pin so a future re-link doesn't carry phantom intent.
    se_held = {c.coord for c in world.grid.values()
               if c.defender == Ownership.SUPER_EARTH and c.attacker is None}
    max_hops = max(0, params.diver_supply_max_hops)

    reachable: list[Cell] = []
    for cell in targets:
        if _within_hops(cell.coord, se_held, max_hops):
            reachable.append(cell)
        else:
            cell.diver_pressure = 0.0
            cell.diver_pin = False

    if not reachable:
        return

    pinned = [c for c in reachable if c.diver_pin and c.diver_pressure > 0.0]
    free_cells = [c for c in reachable if c not in pinned]

    pinned_sum = sum(c.diver_pressure for c in pinned)
    free = max(0.0, params.diver_pool - pinned_sum)

    if not free_cells:
        return

    if free <= 0.0:
        # All hands pinned; allocator has nothing to spend.
        for c in free_cells:
            c.diver_pressure = 0.0
        return

    utilities = [_utility(world, c) for c in free_cells]
    probs = _softmax(utilities, params.allocation_temperature)
    for cell, p in zip(free_cells, probs):
        cell.diver_pressure = free * p


def _within_hops(coord, se_held: set, max_hops: int) -> bool:
    for c in cells_within(coord, max_hops):
        if c in se_held:
            return True
    return False


def _utility(world: "World", cell: Cell) -> float:
    params = world.params
    threshold = world._effective_threshold(cell)
    completion = (cell.progress / threshold) if threshold > 0 else 0.0

    weakness = 1.0 - cell.enemy_supply

    se_neighbors = 0
    for n in neighbors(cell.coord):
        nc = world.grid.get(n)
        if nc is not None and nc.defender == Ownership.SUPER_EARTH:
            se_neighbors += 1
    frontline = se_neighbors / 6.0

    # siege_mult is 1.0 for a normal cell, >1 when a fortress imposes its
    # multiplier — penalize directly with the excess.
    siege_excess = (threshold / params.flip_threshold) - 1.0

    return completion + weakness + frontline - siege_excess


def _softmax(values: list[float], temperature: float) -> list[float]:
    if not values:
        return []
    temp = max(1e-6, temperature)
    scaled = [v / temp for v in values]
    m = max(scaled)
    exps = [math.exp(s - m) for s in scaled]
    total = sum(exps)
    if total <= 0:
        n = len(values)
        return [1.0 / n] * n
    return [e / total for e in exps]
