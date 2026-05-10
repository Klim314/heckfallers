"""SE side AI.

The diver agent abstracts the playerbase as a constant pool of force
that gets distributed across the front each allocation tick. Cells the
user has pinned (via the controller pressure slider) consume from the
pool first; the rest is shared via softmax(utility / temperature) over
both offensive (SE-attacker) and defensive (enemy-attacker) contests.

When the contested set is empty (e.g., after a wave of repulses),
``allocate_divers`` opens new fronts on enemy-defended cells bordering
held SE territory before allocating, so the sim can't deadlock between
"no contested cells" and "no SE POIs to attract destroy salients".

Offensive utility combines four pieces:

- completion: cells closer to flipping get more divers (finish the job)
- weakness:   cells with low enemy supply are easier wins
- frontline:  cells with more SE-defended neighbors are reachable / safer
- siege:      cells under fortress siege multiplier are deprioritized

Defensive utility (SE cells under enemy push — salients, factories,
ambient incursion) layers a flat ``defense_priority_bias`` on top of:

- urgency: how close enemy is to capturing this cell
- threat:  salient_pressure / factory_pressure stamped this tick
- corridor: membership in an active salient corridor, weighted higher
            for cells closer to the targeted SE POI (last line of defense)

Future iterations will layer cohort-typed allocation (heavy / objective /
support) per the design doc, but v1 collapses everything into one pool.
"""
from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING

from .cell import Cell, Ownership
from .grid import cells_within, neighbors

if TYPE_CHECKING:
    from .world import World


def allocate_divers(world: "World") -> None:
    params = world.params

    # Defensive contests (enemy-attacker SE cells) compete for the same
    # diver pool as offensive ones. The defensive utility carries a flat
    # bias so divers naturally divert to push back active salients /
    # factories before chasing offensive completions.
    targets = [c for c in world.grid.values() if c.attacker is not None]
    if not targets:
        # Quiet front — open new contestations on every enemy-defended cell
        # that borders held SE territory. Without this, repulse + zero SE
        # POIs deadlocks the sim: HighCommand needs SE-attacker cells to
        # score placements, OpportunisticController needs SE POIs to spawn
        # destroy salients, and no other path creates contested cells.
        # Gated on a positive pool: zero divers ⇒ no presence to push with.
        if params.diver_pool <= 0.0:
            return
        targets = _open_new_fronts(world)
        if not targets:
            return

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
    # Pin is a hard guarantee — apply pool jitter only to the discretionary
    # remainder so manual-controller commitments stay stable.
    free = max(0.0, params.diver_pool - pinned_sum)
    pool_sigma = params.allocation_pool_jitter_sigma
    if pool_sigma > 0.0:
        free *= math.exp(random.gauss(0.0, pool_sigma))

    if not free_cells:
        return

    if free <= 0.0:
        # All hands pinned; allocator has nothing to spend.
        for c in free_cells:
            c.diver_pressure = 0.0
        return

    temp = params.allocation_temperature
    temp_jitter = params.allocation_temperature_jitter
    if temp_jitter > 0.0:
        temp = max(0.05, temp + random.uniform(-temp_jitter / 2.0, temp_jitter / 2.0))

    utilities = [_utility(world, c) for c in free_cells]
    probs = _softmax(utilities, temp)

    chunk_count = params.allocation_chunk_count
    if chunk_count > 1:
        # Multinomial: each chunk is one "deployment wave" sampled from the
        # softmax distribution. Variance ≈ K * p * (1-p) per cell, which is
        # the main source of bursts that the retaliation gauge needs.
        chunk_size = free / chunk_count
        counts = [0] * len(free_cells)
        cumulative = []
        run = 0.0
        for p in probs:
            run += p
            cumulative.append(run)
        for _ in range(chunk_count):
            r = random.random()
            for i, c_prob in enumerate(cumulative):
                if r < c_prob:
                    counts[i] += 1
                    break
            else:
                counts[-1] += 1
        for cell, c in zip(free_cells, counts):
            cell.diver_pressure = c * chunk_size
    else:
        for cell, p in zip(free_cells, probs):
            cell.diver_pressure = free * p


def _open_new_fronts(world: "World") -> list[Cell]:
    """Stamp ``attacker = SE`` on every enemy-defended cell adjacent to held
    SE territory. Used when the contested set is empty to bootstrap a new
    push; the regular reachability filter and softmax in ``allocate_divers``
    handle the opened cells from there.
    """
    opened: list[Cell] = []
    for cell in world.grid.values():
        if cell.defender != Ownership.ENEMY or cell.attacker is not None:
            continue
        for n in neighbors(cell.coord):
            nc = world.grid.get(n)
            if nc is not None and nc.defender == Ownership.SUPER_EARTH and nc.attacker is None:
                cell.attacker = Ownership.SUPER_EARTH
                cell.progress = 0.0
                opened.append(cell)
                break
    return opened


def _within_hops(coord, se_held: set, max_hops: int) -> bool:
    for c in cells_within(coord, max_hops):
        if c in se_held:
            return True
    return False


def _utility(world: "World", cell: Cell) -> float:
    threshold = world._effective_threshold(cell)
    if cell.attacker == Ownership.SUPER_EARTH:
        return _offensive_utility(world, cell, threshold)
    return _defensive_utility(world, cell, threshold)


def _offensive_utility(world: "World", cell: Cell, threshold: float) -> float:
    params = world.params
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


def _defensive_utility(world: "World", cell: Cell, threshold: float) -> float:
    params = world.params

    # Urgency: enemy captures at progress = -threshold, so a more-negative
    # progress means the cell is closer to falling. Clamp non-negative so a
    # cell already swinging toward repulse doesn't drag utility down.
    urgency = max(0.0, -cell.progress / threshold) if threshold > 0 else 0.0

    # Active enemy push terms — present iff the salient/factory mechanic
    # stamped this cell this tick. Indicator-style (not magnitude-scaled)
    # since the two pressures use different magnitudes by design.
    threat = 0.0
    if cell.salient_pressure > 0.0:
        threat += 1.0
    if cell.factory_pressure > 0.0:
        threat += 0.5

    # Salient corridor: cells closer to the targeted SE POI carry more
    # weight (last line of defense before the POI is destroyed). corridor
    # is enemy_origin -> ... -> target, so larger index = closer to target.
    corridor_bonus = 0.0
    for s in world.salients.values():
        if cell.coord not in s.corridor:
            continue
        idx = s.corridor.index(cell.coord)
        denom = max(1, len(s.corridor) - 1)
        corridor_bonus = max(corridor_bonus, 1.0 + (idx / denom))

    return params.defense_priority_bias + urgency + threat + corridor_bonus


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
