"""Salient primitive — directed enemy operations.

A salient is a *what* (corridor of cells, target, lifetime, effects); the
*who/when/why* of its creation lives in a controller. This module is pure
mechanics: a Salient dataclass plus three module-level functions called by
the world tick / supply pipeline / controllers.

v1 ships only the destroy kind, but the union and helpers are structured
so capture/build slot in without churning callers.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Literal, TYPE_CHECKING

from .cell import Ownership
from .grid import Coord, neighbors

if TYPE_CHECKING:
    from .world import World


SalientKind = Literal["destroy"]


@dataclass
class Salient:
    id: str
    kind: SalientKind
    corridor: list[Coord]
    target: Coord
    target_poi_id: str
    spawned_tick: int
    expires_tick: int
    state: dict = field(default_factory=dict)

    def to_wire(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "corridor": [list(c) for c in self.corridor],
            "target": list(self.target),
            "target_poi_id": self.target_poi_id,
            "spawned_tick": self.spawned_tick,
            "expires_tick": self.expires_tick,
        }


def update_salients(world: "World") -> None:
    """Expire salients past lifetime or whose target POI is gone.

    Emits a match event on success (target POI destroyed) so the client
    can flash an alert. Lifetime expiry is silent.
    """
    doomed: list[tuple[str, str]] = []  # (salient_id, reason)
    for sid, s in world.salients.items():
        if s.target_poi_id not in world.pois:
            doomed.append((sid, "success"))
        elif world.tick >= s.expires_tick:
            doomed.append((sid, "expired"))

    for sid, reason in doomed:
        s = world.salients.pop(sid)
        if reason == "success":
            world.match_events.append({
                "type": "destroy_salient_success",
                "tick": world.tick,
                "salient_id": sid,
                "target": list(s.target),
            })

    if doomed:
        # Corridor cells need to revert from tunneled supply back to BFS.
        world._supply_dirty = True


def apply_salient_supply(world: "World") -> None:
    """Post-pass after recompute_all: corridor cells get enemy_supply >= floor.

    Models the salient projecting supply along its axis regardless of
    normal BFS connectivity. Only meaningful for contested corridor cells
    (the apply step in world._apply_pressure consumes supply only there);
    non-contested cells just carry the boosted value for visualization.
    """
    floor = world.params.destroy_corridor_supply_floor
    for s in world.salients.values():
        for coord in s.corridor:
            cell = world.grid.get(coord)
            if cell is None:
                continue
            if cell.enemy_supply < floor:
                cell.enemy_supply = floor


def build_destroy_corridor(world: "World", target: Coord) -> list[Coord] | None:
    """BFS from any enemy-defended cell to ``target``, all cells traversable.

    Returns the path origin -> target inclusive, or None if the target is
    unreachable within ``destroy_max_range`` hops. The corridor may cross
    contested or SE-defended cells (that's the "tunnel" — supply will be
    projected along it).

    Multi-source BFS from the enemy front guarantees the shortest path
    from the closest enemy cell to the target.
    """
    if world.grid.get(target) is None:
        return None

    max_range = world.params.destroy_max_range

    # Sources: every enemy-defended cell. (Multi-source BFS finds the
    # shortest path from *any* of them to the target.)
    parents: dict[Coord, Coord | None] = {}
    queue: deque[Coord] = deque()
    for coord, cell in world.grid.items():
        if cell.defender == Ownership.ENEMY:
            parents[coord] = None
            queue.append(coord)

    if not queue:
        return None

    dist: dict[Coord, int] = {c: 0 for c in queue}
    found = False
    while queue:
        cur = queue.popleft()
        if cur == target:
            found = True
            break
        if dist[cur] >= max_range:
            continue
        for n in neighbors(cur):
            if n in parents:
                continue
            if world.grid.get(n) is None:
                continue
            parents[n] = cur
            dist[n] = dist[cur] + 1
            queue.append(n)

    if not found:
        return None

    # Walk parents back from target to its enemy-front origin.
    path: list[Coord] = []
    cur: Coord | None = target
    while cur is not None:
        path.append(cur)
        cur = parents.get(cur)
    path.reverse()
    return path


def spawn_destroy_salient(world: "World", target_poi_id: str) -> Salient | None:
    """Construct + register a destroy salient targeting the given POI.

    Returns None if the POI is missing, no corridor exists within range,
    or another active salient already targets this POI. Opens the lead by
    setting the first SE-defended corridor cell to contested by enemy.
    """
    poi = world.pois.get(target_poi_id)
    if poi is None or poi.owner != Ownership.SUPER_EARTH:
        return None

    for s in world.salients.values():
        if s.target_poi_id == target_poi_id:
            return None

    corridor = build_destroy_corridor(world, poi.coord)
    if corridor is None or len(corridor) < 2:
        return None

    sid = f"sal_{world._next_salient_id}"
    world._next_salient_id += 1
    lifetime_ticks = int(world.params.destroy_salient_lifetime_s * world.params.tick_hz)

    salient = Salient(
        id=sid,
        kind="destroy",
        corridor=corridor,
        target=poi.coord,
        target_poi_id=target_poi_id,
        spawned_tick=world.tick,
        expires_tick=world.tick + lifetime_ticks,
    )
    world.salients[sid] = salient

    # Open the lead: the first SE-defended cell along the corridor (from
    # the enemy origin toward the target) becomes contested by enemy if
    # not already. Without this the salient would have to wait for the
    # natural front to advance into the corridor.
    for coord in corridor:
        cell = world.grid.get(coord)
        if cell is None:
            continue
        if cell.defender == Ownership.SUPER_EARTH and cell.attacker is None:
            cell.attacker = Ownership.ENEMY
            cell.progress = 0.0
            break

    world._supply_dirty = True
    return salient
