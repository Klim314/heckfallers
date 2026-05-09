"""Factory primitive — enemy POI that pumps a continuous trickle of incursions.

A factory holds a small list of active target cells (length capped by
``factory_active_cap``) and stamps ``factory_pressure`` on each of them
every tick. Pressure is cleared and re-stamped each tick (mirror of the
salient pattern), so destroying the factory releases its cells
immediately and target rotation is free.

Salients are discrete operations — focused, large, time-bounded, one
corridor with high pressure. Factories are ambient pressure — small per
push, persistent, distributed across the front. Together they produce
"continuous war with flash points" rather than uniform pressure.

Target selection (``tick_factories``) runs on its own cadence so factories
don't churn picks every tick. Heuristic prefers in-progress incursions
(don't abandon a half-flipped cell) and falls back to weighted random by
SE supply weakness for fresh slots.
"""
from __future__ import annotations

import random
from typing import TYPE_CHECKING

from .cell import Ownership
from .events import emit
from .grid import Coord, cells_within, neighbors
from .poi import POI

if TYPE_CHECKING:
    from .world import World


def apply_factory_pressure(world: "World") -> None:
    """Clear factory_pressure on every cell, then re-stamp on each
    factory's active targets at ``factory_pressure_magnitude``.

    Re-stamped each tick so a destroyed factory or rotated target stops
    contributing immediately. Non-contested targets carry the value for
    visualization only — ``_apply_pressure`` skips them.
    """
    for cell in world.grid.values():
        cell.factory_pressure = 0.0
    magnitude = world.params.factory_pressure_magnitude
    for poi in world.pois.values():
        if poi.kind != "factory":
            continue
        for t in poi.state.get("active_targets", []):
            cell = world.grid.get(tuple(t))
            if cell is None:
                continue
            if cell.factory_pressure < magnitude:
                cell.factory_pressure = magnitude


def tick_factories(world: "World") -> None:
    """Per-factory: prune stale targets, fill open slots from candidates.

    Cadence-gated by ``factory_target_period_ticks``. A target stays
    live only while it remains a valid push: SE-defended, ENEMY-attacker,
    AND still front-adjacent to enemy territory. Cells that lose their
    enemy neighbor (the front moved past them) get dropped — the factory
    isn't a way to feed a stranded incursion deep in SE territory; that
    role belongs to salients.
    """
    if world.tick % max(1, world.params.factory_target_period_ticks) != 0:
        return
    cap = max(0, world.params.factory_active_cap)
    radius = max(1, world.params.factory_radius)
    for poi in world.pois.values():
        if poi.kind != "factory":
            continue
        targets = poi.state.setdefault("active_targets", [])
        live: list[list[int]] = []
        for t in targets:
            coord = (t[0], t[1])
            cell = world.grid.get(coord)
            if cell is None:
                continue
            if cell.defender != Ownership.SUPER_EARTH:
                continue
            if cell.attacker != Ownership.ENEMY:
                continue
            if not _is_front_adjacent(world, coord):
                continue
            live.append([t[0], t[1]])
        while len(live) < cap:
            picked = _select_target(world, poi.coord, radius, exclude={(t[0], t[1]) for t in live})
            if picked is None:
                break
            cell = world.grid[picked]
            if cell.attacker is None:
                cell.attacker = Ownership.ENEMY
                cell.progress = 0.0
                emit(
                    world, "factory_strike",
                    coord=list(picked),
                    factory_id=poi.id,
                )
            live.append([picked[0], picked[1]])
        poi.state["active_targets"] = live


def _is_front_adjacent(world: "World", coord: Coord) -> bool:
    """True iff ``coord`` has at least one ENEMY-defended neighbor.

    Single source of truth for "this cell touches enemy territory" — used
    by both target selection (filter candidates) and pruning (drop targets
    that lost their connection as the front shifted).
    """
    for n in neighbors(coord):
        nc = world.grid.get(n)
        if nc is not None and nc.defender == Ownership.ENEMY:
            return True
    return False


def _select_target(
    world: "World",
    origin: Coord,
    radius: int,
    exclude: set[Coord],
) -> Coord | None:
    """Pick one SE-defended cell within radius for a factory to push.

    Candidate filter: SE-defended, front-adjacent (≥1 enemy-defended
    neighbor), within ``radius`` hex distance from origin, not in
    ``exclude``. Selection:

    1. If any candidate is already ENEMY-attacker with negative progress,
       take the one closest to capture (most-negative progress) — keeps
       a flash point burning rather than diluting effort.
    2. Else weighted random over uncontested candidates by
       ``(1 - se_supply)``: prefer thin SE supply.
    """
    in_progress: list[tuple[Coord, float]] = []
    fresh: list[tuple[Coord, float]] = []
    for coord in cells_within(origin, radius):
        if coord == origin or coord in exclude:
            continue
        cell = world.grid.get(coord)
        if cell is None or cell.defender != Ownership.SUPER_EARTH:
            continue
        if not _is_front_adjacent(world, coord):
            continue
        if cell.attacker == Ownership.ENEMY and cell.progress < 0:
            in_progress.append((coord, cell.progress))
        elif cell.attacker is None:
            fresh.append((coord, max(0.0, 1.0 - cell.se_supply)))

    if in_progress:
        in_progress.sort(key=lambda x: x[1])  # most negative first
        return in_progress[0][0]
    if not fresh:
        return None
    total = sum(w for _, w in fresh)
    if total <= 0:
        return random.choice(fresh)[0]
    pick = random.random() * total
    acc = 0.0
    for coord, w in fresh:
        acc += w
        if pick <= acc:
            return coord
    return fresh[-1][0]


def spawn_factory(world: "World", coord: Coord) -> POI | None:
    """Place a factory POI on ``coord`` if permitted. Convenience helper
    for tests and controllers; mirrors ``spawn_destroy_salient``."""
    return world.place_poi("factory", Ownership.ENEMY, coord)
