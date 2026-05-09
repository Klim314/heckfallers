"""Salient primitive — directed enemy operations.

A salient is a *what* (cells, target, lifetime, effects); the *who/when/
why* of its creation lives in a controller. This module is pure mechanics:
a Salient dataclass plus module-level functions called by the world tick
/ supply pipeline / controllers.

Two kinds exist:
- ``destroy``: a single corridor from the enemy front to a high-value SE
  POI, narrow and high-pressure. Ends on lifetime or target POI loss.
- ``conquer``: a union of small patches dropped on top of recent SE
  capture activity. Wide and low-pressure — rubber-banding retaliation
  for a player who's been clearing too quickly. Ends only on lifetime.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Literal, TYPE_CHECKING

from .cell import Ownership
from .events import emit
from .grid import Coord, cells_within, distance, neighbors

if TYPE_CHECKING:
    from .world import World


SalientKind = Literal["destroy", "conquer"]


@dataclass
class Salient:
    id: str
    kind: SalientKind
    corridor: list[Coord]
    target: Coord | None
    target_poi_id: str | None
    spawned_tick: int
    expires_tick: int
    region: list[Coord] = field(default_factory=list)
    state: dict = field(default_factory=dict)

    def to_wire(self) -> dict:
        out: dict = {
            "id": self.id,
            "kind": self.kind,
            "corridor": [list(c) for c in self.corridor],
            "target": list(self.target) if self.target is not None else None,
            "target_poi_id": self.target_poi_id or "",
            "spawned_tick": self.spawned_tick,
            "expires_tick": self.expires_tick,
        }
        if self.region:
            out["region"] = [list(c) for c in self.region]
        return out


def update_salients(world: "World") -> None:
    """Expire salients past lifetime or whose target POI is gone.

    Emits a ``salient_ended`` event with reason=success|expired so the
    client log can render the outcome. Conquer salients have no POI
    target — they only end on lifetime (reason=expired).
    """
    doomed: list[tuple[str, str]] = []  # (salient_id, reason)
    for sid, s in world.salients.items():
        if (s.kind == "destroy"
                and s.target_poi_id is not None
                and s.target_poi_id not in world.pois):
            doomed.append((sid, "success"))
        elif world.tick >= s.expires_tick:
            doomed.append((sid, "expired"))

    for sid, reason in doomed:
        s = world.salients.pop(sid)
        emit(
            world, "salient_ended",
            salient_id=sid,
            kind=s.kind,
            reason=reason,
            target=list(s.target) if s.target is not None else None,
        )

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


def apply_salient_pressure(world: "World") -> None:
    """Stamp offensive pressure on each salient's affected cells.

    Mirror of ``diver_pressure`` for the enemy attacker side: a constant
    per-kind magnitude, consumed in ``_apply_pressure`` via
    ``salient_pressure * pressure_coefficient * en_factor``.

    Per-kind magnitude (destroy ~ high, conquer ~ low) makes overlap
    saturating-by-max: when corridors and conquer regions cross, the
    higher destroy magnitude wins and conquer-on-conquer caps at one
    conquer's worth — never additive, so stacking can't blow up.

    Re-stamped each tick so an expiring salient releases its cells
    immediately. Non-contested cells carry the value for visualization
    only — ``_apply_pressure`` skips them.
    """
    for cell in world.grid.values():
        cell.salient_pressure = 0.0
    destroy_mag = world.params.salient_pressure_magnitude
    conquer_mag = world.params.conquer_pressure_magnitude
    for s in world.salients.values():
        if s.kind == "destroy":
            magnitude = destroy_mag
            cells = s.corridor
        elif s.kind == "conquer":
            magnitude = conquer_mag
            cells = s.region
        else:
            continue
        for coord in cells:
            cell = world.grid.get(coord)
            if cell is None:
                continue
            if cell.salient_pressure < magnitude:
                cell.salient_pressure = magnitude


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
    emit(
        world, "salient_spawned",
        salient_id=sid,
        kind="destroy",
        target=list(poi.coord),
        target_poi_id=target_poi_id,
        target_kind=poi.kind,
    )

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


def find_recent_flip_clusters(
    buffer: list[tuple[Coord, int]],
    k: int,
    radius: int,
    window_ticks: int,
    current_tick: int,
) -> list[Coord]:
    """Pick up to ``k`` cluster centers from a recent-flip buffer.

    Each candidate (a unique flipped coord within the time window) is
    scored by how many flips lie within ``radius`` of it. Greedy selection
    of the top-scoring candidates with a min-separation of ``2*radius``
    spreads the picks instead of stacking them on one hot spot — that's
    the "multiple areas" half of a wide retaliation push.
    """
    if not buffer or k <= 0:
        return []
    cutoff = current_tick - window_ticks
    flips = [c for c, t in buffer if t >= cutoff]
    if not flips:
        return []

    candidates = list({c for c in flips})
    scores: list[tuple[int, Coord]] = []
    for cand in candidates:
        cnt = sum(1 for f in flips if distance(cand, f) <= radius)
        scores.append((cnt, cand))
    # Sort by count desc, with coord as deterministic tiebreaker.
    scores.sort(key=lambda x: (-x[0], x[1]))

    picks: list[Coord] = []
    min_sep = 2 * radius
    for _, cand in scores:
        if len(picks) >= k:
            break
        if all(distance(cand, p) > min_sep for p in picks):
            picks.append(cand)
    return picks


def spawn_conquer_salient(world: "World", centers: list[Coord]) -> Salient | None:
    """Construct + register a conquer salient covering the given cluster centers.

    Each center contributes the cells within ``conquer_cluster_radius``
    (deduped across centers) to the salient's region. SE-defended region
    cells with no current attacker get opened to enemy contestation —
    that's the "widespread push" — so the lower-magnitude pressure stamp
    has cells to bite on. Returns None if no valid grid cells were found.
    """
    if not centers:
        return None
    radius = world.params.conquer_cluster_radius
    region: list[Coord] = []
    seen: set[Coord] = set()
    for ctr in centers:
        for c in cells_within(ctr, radius):
            if c in seen:
                continue
            if world.grid.get(c) is None:
                continue
            seen.add(c)
            region.append(c)
    if not region:
        return None

    sid = f"sal_{world._next_salient_id}"
    world._next_salient_id += 1
    lifetime_ticks = int(world.params.conquer_salient_lifetime_s * world.params.tick_hz)

    salient = Salient(
        id=sid,
        kind="conquer",
        corridor=[],
        target=None,
        target_poi_id=None,
        spawned_tick=world.tick,
        expires_tick=world.tick + lifetime_ticks,
        region=region,
    )
    world.salients[sid] = salient
    emit(
        world, "salient_spawned",
        salient_id=sid,
        kind="conquer",
        target=None,
        target_poi_id=None,
        region_size=len(region),
        center=list(centers[0]) if centers else None,
    )

    # Open the push: every SE-defended uncontested region cell becomes
    # enemy-attacker. Without this the pressure stamp lands on cells whose
    # attacker is None, which _apply_pressure skips. Wide opening matches
    # the "push into multiple areas" intent — many simultaneous shallow
    # contests rather than one focused breach.
    for coord in region:
        cell = world.grid.get(coord)
        if cell is None:
            continue
        if cell.defender == Ownership.SUPER_EARTH and cell.attacker is None:
            cell.attacker = Ownership.ENEMY
            cell.progress = 0.0

    world._supply_dirty = True
    return salient
