"""Salient primitive — directed enemy operations.

A salient is a *what* (cells, target, lifetime, effects); the *who/when/
why* of its creation lives in a controller. This module is pure mechanics:
a Salient dataclass plus module-level functions called by the world tick
/ supply pipeline / controllers.

Two kinds exist:
- ``destroy``: a single corridor from the enemy front to a high-value SE
  POI, narrow and high-pressure. Ends on lifetime or target POI loss.
- ``conquer``: a leapfrogging directional wedge. Spawns a visible staging
  POI on enemy territory; after a charge timer it activates with a 2- or
  3-cell initial fan; subsequent SE→ENEMY flips inside the salient roll
  forward-hemisphere-only spread with additive probability across adjacent
  tracked cells and per-generation decay. Ends on lifetime expiry, on
  staging-POI loss before activation (intercepted), or when all tracked
  cells are repulsed (extinguished). See docs/conquer-salient-redesign.md.
"""
from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field
from typing import Literal, TYPE_CHECKING

from .cell import Ownership
from .events import emit
from .grid import Coord, _axial_to_cube, distance, forward_hemisphere, neighbors

if TYPE_CHECKING:
    from .world import World


SalientKind = Literal["destroy", "conquer"]


@dataclass
class Salient:
    id: str
    kind: SalientKind
    spawned_tick: int
    expires_tick: int

    # destroy-only
    corridor: list[Coord] = field(default_factory=list)
    target: Coord | None = None
    target_poi_id: str | None = None

    # conquer-only (new shape)
    activated: bool = False
    staging_poi_id: str | None = None
    axis: Coord | None = None
    fan_size: int = 0
    tracked_cells: dict[Coord, int] = field(default_factory=dict)

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
        if self.kind == "conquer":
            out["activated"] = self.activated
            out["staging_poi_id"] = self.staging_poi_id or ""
            out["axis"] = list(self.axis) if self.axis is not None else None
            out["fan_size"] = self.fan_size
            # tuple-keyed dicts don't JSON-serialize; emit as list of [q, r, gen].
            out["tracked_cells"] = [
                [c[0], c[1], gen] for c, gen in self.tracked_cells.items()
            ]
        return out


def update_salients(world: "World") -> None:
    """Drive salient state machines: lifetime expiry, target-POI destruction,
    conquer staging-charge timer, conquer tracked-cells pruning, conquer
    natural extinction.

    Emits a ``salient_ended`` event with reason=success|expired|extinguished|
    intercepted so the client log can render the outcome. Conquer salients
    in their staging phase end with reason=intercepted if their staging POI
    is gone; activated conquer salients end with reason=extinguished if
    their tracked-cells set empties out.
    """
    # First pass: drive conquer state machines (may activate or end them).
    for sid, s in list(world.salients.items()):
        if s.kind != "conquer":
            continue
        if not s.activated:
            # Pre-activation: staging-POI lifecycle.
            if s.staging_poi_id is None or s.staging_poi_id not in world.pois:
                _end_salient(world, sid, "intercepted")
                continue
            staging = world.pois[s.staging_poi_id]
            charge_at = staging.state.get("charge_completes_at", float("inf"))
            if world.tick >= charge_at:
                activate_conquer_salient(world, s)
        else:
            # Activated: prune any tracked cell that's now SE-defended uncontested
            # (covers both repulse and SE recapture of a flipped salient cell).
            doomed_coords = [
                c for c in s.tracked_cells
                if (cell := world.grid.get(c)) is not None
                and cell.defender == Ownership.SUPER_EARTH
                and cell.attacker is None
            ]
            for c in doomed_coords:
                del s.tracked_cells[c]
            if doomed_coords:
                world._supply_dirty = True
            if not s.tracked_cells:
                _end_salient(world, sid, "extinguished")

    # Second pass: destroy success + universal lifetime expiry.
    doomed: list[tuple[str, str]] = []  # (salient_id, reason)
    for sid, s in world.salients.items():
        if (s.kind == "destroy"
                and s.target_poi_id is not None
                and s.target_poi_id not in world.pois):
            doomed.append((sid, "success"))
        elif world.tick >= s.expires_tick:
            doomed.append((sid, "expired"))

    for sid, reason in doomed:
        _end_salient(world, sid, reason)


def _end_salient(world: "World", sid: str, reason: str) -> None:
    """Pop a salient and emit ``salient_ended``. Also tears down a staging
    POI if the salient was still in its staging phase."""
    s = world.salients.pop(sid, None)
    if s is None:
        return
    # Tear down a still-attached staging POI so a cancelled conquer salient
    # doesn't leave its telegraph orphaned on the grid.
    if s.kind == "conquer" and s.staging_poi_id and s.staging_poi_id in world.pois:
        world.remove_poi(s.staging_poi_id)
        s.staging_poi_id = None
    emit(
        world, "salient_ended",
        salient_id=sid,
        kind=s.kind,
        reason=reason,
        target=list(s.target) if s.target is not None else None,
    )
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
    saturating-by-max: when destroy corridors and conquer tracked cells
    cross, the higher destroy magnitude wins and conquer-on-conquer caps
    at one conquer's worth — never additive, so stacking can't blow up.

    Conquer salients only stamp once activated; pre-activation salients
    are pure telegraph (the staging POI is the player's signal).

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
            cells: list[Coord] | tuple[Coord, ...] = s.corridor
        elif s.kind == "conquer":
            # Pre-activation conquer salients are pure telegraph — no pressure.
            if not s.activated:
                continue
            magnitude = conquer_mag
            cells = list(s.tracked_cells.keys())
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


# --------------------------------------------------------------------- #
# Conquer salient — staging / activation / spread
# --------------------------------------------------------------------- #


def spawn_conquer_staging(world: "World", target_se_cell: Coord) -> "Salient | None":
    """Place a staging POI on the closest enemy-defended uncontested cell to
    ``target_se_cell`` and register a pre-activation conquer salient.

    The salient's lifetime starts now (staging spawn), not at activation —
    keeps the duration knob simple. The staging POI carries the charge timer
    and a back-pointer to the salient. Returns None if no suitable host cell
    exists within range.
    """
    if world.grid.get(target_se_cell) is None:
        return None

    max_range = world.params.destroy_max_range
    best: tuple[int, Coord] | None = None
    for coord, cell in world.grid.items():
        if cell.defender != Ownership.ENEMY or cell.attacker is not None:
            continue
        d = distance(coord, target_se_cell)
        if d > max_range:
            continue
        if best is None or d < best[0] or (d == best[0] and coord < best[1]):
            best = (d, coord)
    if best is None:
        return None
    staging_coord = best[1]

    poi = world.place_poi("salient_staging", Ownership.ENEMY, staging_coord)
    if poi is None:
        return None

    sid = f"sal_{world._next_salient_id}"
    world._next_salient_id += 1
    lifetime_ticks = int(world.params.conquer_salient_lifetime_s * world.params.tick_hz)
    fan_size = random.randint(world.params.conquer_fan_min, world.params.conquer_fan_max)
    axis_hint = (target_se_cell[0] - staging_coord[0], target_se_cell[1] - staging_coord[1])

    salient = Salient(
        id=sid,
        kind="conquer",
        spawned_tick=world.tick,
        expires_tick=world.tick + lifetime_ticks,
        activated=False,
        staging_poi_id=poi.id,
        axis=axis_hint,
        fan_size=fan_size,
    )

    charge_ticks = int(world.params.conquer_staging_charge_s * world.params.tick_hz)
    poi.state["charge_completes_at"] = world.tick + charge_ticks
    poi.state["parent_salient_id"] = sid

    world.salients[sid] = salient
    emit(
        world, "salient_staging_spawned",
        salient_id=sid,
        staging_coord=list(staging_coord),
        target_coord=list(target_se_cell),
        charge_completes_at=poi.state["charge_completes_at"],
    )
    return salient


def activate_conquer_salient(world: "World", salient: Salient) -> None:
    """Promote a staging-phase conquer salient to active: pick fan cells in
    the forward hemisphere of the staging POI's axis hint, contest them,
    freeze the centroid-based axis, and remove the staging POI.

    If no fan cells can be placed (terrain blocks all forward hexes), the
    salient ends with reason ``intercepted``.
    """
    if salient.activated:
        return
    staging_id = salient.staging_poi_id
    staging_poi = world.pois.get(staging_id) if staging_id else None
    if staging_poi is None:
        # Defensive: caller should have routed this through update_salients,
        # which would already have ended the salient on missing staging POI.
        _end_salient(world, salient.id, "intercepted")
        return
    staging_coord = staging_poi.coord

    axis_hint = salient.axis if salient.axis is not None else (1, 0)
    if axis_hint == (0, 0):
        axis_hint = (1, 0)
    forward_dirs = forward_hemisphere(axis_hint)
    if not forward_dirs:
        # No forward direction — extremely defensive (axis was zero). Bail.
        _end_salient(world, salient.id, "intercepted")
        return

    # Order forward_dirs by descending dot product with the axis hint so the
    # pure-forward direction comes first, then the two forward-laterals.
    axis_cube = _axial_to_cube(axis_hint)

    def _dot(d: Coord) -> int:
        b = _axial_to_cube(d)
        return sum(x * y for x, y in zip(axis_cube, b))

    ordered = sorted(forward_dirs, key=lambda d: -_dot(d))

    # Pick fan cells: pure-forward first, then forward-laterals in NEIGHBOR_DIRS order.
    # Only SE-defended uncontested cells are eligible — pre-contested cells (by
    # another mechanism: destroy salient, factory, etc.) would otherwise be
    # silently absorbed into tracked_cells without being re-contested.
    picked: list[Coord] = []
    for d in ordered:
        coord = (staging_coord[0] + d[0], staging_coord[1] + d[1])
        cell = world.grid.get(coord)
        if cell is None:
            continue
        if cell.defender != Ownership.SUPER_EARTH or cell.attacker is not None:
            continue
        # Skip cells already tracked by another conquer salient (multi-salient overlap guard).
        if any(
            other.kind == "conquer"
            and other is not salient
            and coord in other.tracked_cells
            for other in world.salients.values()
        ):
            continue
        picked.append(coord)

    picked = picked[: salient.fan_size] if salient.fan_size > 0 else picked
    if not picked:
        _end_salient(world, salient.id, "intercepted")
        return

    for coord in picked:
        cell = world.grid[coord]
        cell.attacker = Ownership.ENEMY
        cell.progress = 0.0
        salient.tracked_cells[coord] = 0

    # Freeze axis = centroid(picked) - staging.
    cq = sum(c[0] for c in picked) / len(picked)
    cr = sum(c[1] for c in picked) / len(picked)
    new_axis: Coord = (round(cq) - staging_coord[0], round(cr) - staging_coord[1])
    if new_axis == (0, 0):
        # Degenerate centroid; fall back to the original axis hint.
        new_axis = axis_hint
    salient.axis = new_axis

    world.remove_poi(staging_id)
    salient.staging_poi_id = None
    salient.activated = True
    world._supply_dirty = True

    emit(
        world, "salient_activated",
        salient_id=salient.id,
        axis=list(new_axis),
        fan=[list(c) for c in picked],
    )


def on_cell_flip(world: "World", coord: Coord, new_defender: Ownership) -> None:
    """Hook fired from ``World._flip_cell`` after default bookkeeping.

    For an ENEMY flip on a coord tracked by an activated conquer salient,
    roll spread into the forward-hemisphere neighbors with additive
    probability across adjacent already-tracked cells, decayed per
    generation. New contestations are added to ``tracked_cells`` with
    ``gen = parent_gen + 1``. The new contestations cannot themselves
    flip in the same tick, so this is safe to run synchronously without
    cascade risk.
    """
    if new_defender != Ownership.ENEMY:
        return
    parent: Salient | None = None
    for s in world.salients.values():
        if s.kind != "conquer" or not s.activated:
            continue
        if coord in s.tracked_cells:
            parent = s
            break
    if parent is None:
        return
    if parent.axis is None:
        return

    parent_gen = parent.tracked_cells[coord]
    p_base = world.params.conquer_spread_p_base
    decay = world.params.conquer_spread_decay_base
    max_gen = world.params.conquer_max_gen

    # Snapshot tracked_cells before the loop so all neighbors see the same
    # k for this single flip event — newly-contested cells in this loop
    # don't cascade into raising k for the cells we visit next.
    tracked_snapshot = list(parent.tracked_cells.keys())

    for d in forward_hemisphere(parent.axis):
        n_coord = (coord[0] + d[0], coord[1] + d[1])
        ncell = world.grid.get(n_coord)
        if ncell is None:
            continue
        if n_coord in parent.tracked_cells:
            continue
        # Multi-salient guard: don't poach a cell tracked by another conquer.
        if any(
            other.kind == "conquer"
            and other is not parent
            and n_coord in other.tracked_cells
            for other in world.salients.values()
        ):
            continue
        if ncell.defender != Ownership.SUPER_EARTH or ncell.attacker is not None:
            continue
        gen_new = parent_gen + 1
        if gen_new > max_gen:
            continue
        # k = number of currently-tracked salient cells adjacent to n_coord,
        # measured against the pre-loop snapshot.
        k = sum(1 for t in tracked_snapshot if distance(t, n_coord) == 1)
        if k <= 0:
            continue
        p_per_source = p_base * (decay ** gen_new)
        # Clamp per-source probability so 1-(1-p)^k stays well-defined.
        p_per_source = max(0.0, min(1.0, p_per_source))
        p_contest = 1.0 - (1.0 - p_per_source) ** k
        if random.random() < p_contest:
            ncell.attacker = Ownership.ENEMY
            ncell.progress = 0.0
            parent.tracked_cells[n_coord] = gen_new
            world._supply_dirty = True
