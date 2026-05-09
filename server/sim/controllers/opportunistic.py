"""OpportunisticController — strikes exposed high-value SE POIs.

Tactical (every enemy_ai_period_ticks): existing flat resistance stamp.
Resistance node spawn (every enemy_spawn_period_ticks): existing logic.
Factory spawn (every factory_period_ticks): probabilistic with soft and
hard caps — usually sits at the soft cap, occasionally surges above it.
Strategic (every salient_period_ticks): score every SE fob/artillery by
``value / (front_distance + 1)``; if best score > threshold and active
destroy-salient cap not hit, spawn a destroy salient targeting it.

Personality: "wait for the player to expose something, then strike." A
player who keeps high-value POIs deep behind their lines is rarely
struck; a player pushing FOBs to the front pays for it.
"""
from __future__ import annotations

import random
from typing import TYPE_CHECKING

from .. import factory as factory_mod
from .. import salient as salient_mod
from ..cell import Ownership
from ..enemy_ai import maybe_spawn_resistance_node, update_enemy_pressure
from ..grid import Coord, cells_within, distance, neighbors

if TYPE_CHECKING:
    from ..world import World


# Per-POI value weight for strike prioritization. Artillery is more
# disruptive to lose than a FOB (one shell can decide a flip), so it
# scores higher. Build sites are valued at their target kind — an
# in-progress artillery build is just as enticing as a finished one.
_POI_VALUE: dict[str, float] = {
    "artillery": 1.0,
    "fob": 0.7,
}


def _value_of(poi) -> float:
    if poi.kind == "build_site":
        target = poi.state.get("target_kind")
        return _POI_VALUE.get(target, 0.0)
    return _POI_VALUE.get(poi.kind, 0.0)


class OpportunisticController:
    name = "opportunistic"

    def tick(self, world: "World") -> None:
        if world.tick % world.params.enemy_ai_period_ticks == 0:
            update_enemy_pressure(world)
        if world.tick % world.params.enemy_spawn_period_ticks == 0 and world.tick > 0:
            maybe_spawn_resistance_node(world)
        if world.tick % world.params.factory_period_ticks == 0 and world.tick > 0:
            self._maybe_spawn_factory(world)
        if world.tick % world.params.salient_period_ticks == 0 and world.tick > 0:
            self._maybe_spawn_destroy_salient(world)

    # --------------------------------------------------------------- #
    # Factories
    # --------------------------------------------------------------- #

    def _maybe_spawn_factory(self, world: "World") -> None:
        """Probabilistic factory spawn with soft / hard caps.

        Below the soft cap the chance is high (steady stream); between
        the soft and hard cap it drops sharply so an occasional surge
        is possible but the average count stays near the soft cap.
        Placement: any enemy-defended uncontested cell with at least
        one SE cell within ``factory_radius`` (i.e., the factory has
        work in reach). Random pick — siting smarts can layer in later.
        """
        params = world.params
        count = sum(1 for p in world.pois.values() if p.kind == "factory")
        if count >= params.factory_hard_cap:
            return
        chance = (params.factory_spawn_chance_below_cap
                  if count < params.factory_soft_cap
                  else params.factory_spawn_chance_over_cap)
        if random.random() > chance:
            return

        candidates: list[Coord] = []
        existing = {p.coord for p in world.pois.values() if p.kind == "factory"}
        for coord, cell in world.grid.items():
            if cell.defender != Ownership.ENEMY or cell.attacker is not None:
                continue
            if coord in existing:
                continue
            in_reach = False
            for c in cells_within(coord, params.factory_radius):
                nc = world.grid.get(c)
                if nc is not None and nc.defender == Ownership.SUPER_EARTH:
                    in_reach = True
                    break
            if in_reach:
                candidates.append(coord)
        if not candidates:
            return
        coord = random.choice(candidates)
        factory_mod.spawn_factory(world, coord)

    # --------------------------------------------------------------- #
    # Strategic
    # --------------------------------------------------------------- #

    def _maybe_spawn_destroy_salient(self, world: "World") -> None:
        active_destroy = sum(1 for s in world.salients.values() if s.kind == "destroy")
        if active_destroy >= world.params.max_active_destroy_salients:
            return

        targeted = {s.target_poi_id for s in world.salients.values()}
        front: list[Coord] = [
            c.coord for c in world.grid.values() if c.defender == Ownership.ENEMY
        ]
        if not front:
            return

        best: tuple[float, str] | None = None
        for pid, poi in world.pois.items():
            if poi.owner != Ownership.SUPER_EARTH:
                continue
            value = _value_of(poi)
            if value == 0.0:
                continue
            if pid in targeted:
                continue

            # Distance from nearest enemy-defended cell to this POI.
            d = min(distance(poi.coord, fc) for fc in front)
            if d > world.params.destroy_max_range:
                continue

            score = value / (d + 1)
            if best is None or score > best[0]:
                best = (score, pid)

        if best is None or best[0] < world.params.destroy_min_score_threshold:
            return

        salient_mod.spawn_destroy_salient(world, best[1])
