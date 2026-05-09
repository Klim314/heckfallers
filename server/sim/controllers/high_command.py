"""HighCommandController — SE strategic infrastructure planner.

Counterpart to OpportunisticController. Where the enemy controller spawns
incursions targeting exposed SE POIs, the high command places, moves, and
decommissions FOBs and artillery to project SE force into contested
territory. Player diver allocation (server.sim.se_ai) is the executor;
this controller is the planner above it.

Cost model: a single shared ``requisition`` pool accrues every world
tick. Each candidate action has a cost (placement scales quadratically
with current count of that type; moves are flat). Per strategic tick the
controller picks the best ``utility / cost`` action above threshold that
the pool can afford and commits it.

Phase 4b: lifecycle. Each strategic tick the controller (1) advances a
per-POI stale counter and decommissions any structure whose individual
coverage has been zero for ``decommission_stale_ticks`` consecutive
strategic passes (free, automatic), then (2) scores place and move
candidates and commits the highest gain/cost ratio that the pool can
afford. Move = teardown source + build site at dest with the shorter
``move_build_ticks`` window — exposing the destination cell to enemy
interruption while construction completes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..cell import Ownership
from ..grid import Coord, cells_within

if TYPE_CHECKING:
    from ..world import World


@dataclass
class HighCommandController:
    name: str = "high_command"
    requisition: float = 0.0
    # Per-POI consecutive zero-coverage strategic-tick count. Cleaned up
    # whenever a POI disappears or returns to nonzero coverage.
    _stale_counters: dict[str, int] = field(default_factory=dict)

    def tick(self, world: "World") -> None:
        params = world.params
        if not params.high_command_enabled:
            return

        # Smooth accrual every world tick — strategic decisions are lumpy
        # but the pool fills continuously, so cost legibility doesn't
        # depend on the strategic cadence.
        self.requisition += params.requisition_per_tick

        if world.tick == 0:
            return
        if world.tick % params.high_command_period_ticks != 0:
            return

        self._strategic_pass(world)

    # --------------------------------------------------------------- #
    # Strategic pass
    # --------------------------------------------------------------- #

    def _strategic_pass(self, world: "World") -> None:
        # Score and commit move/place actions BEFORE decommission so a
        # stale POI with a positive-improvement move can be relocated
        # rather than removed — the materiel discount of move is the
        # whole point of the move primitive. Decommission then sweeps
        # any *still*-stale POIs that weren't selected as move sources.
        candidates = self._collect_candidates(world)
        if candidates:
            # Highest gain-per-cost wins. On ties prefer the cheaper action;
            # then op (move alphabetically before place — relocating preserves
            # the count and is "less committal" under the soft-cap intent);
            # then kind / coord for full determinism.
            candidates.sort(
                key=lambda a: (-a.ratio, a.cost, a.op, a.target_kind, a.target_coord)
            )
            self._commit(world, candidates[0])

        self._update_stale_and_decommission(world)

    def _collect_candidates(self, world: "World") -> list["_Action"]:
        params = world.params
        out: list[_Action] = []

        place_specs = [
            ("fob", params.fob_radius, params.fob_base_cost,
             params.fob_cost_exponent, params.fob_min_coverage_threshold),
            ("artillery", params.arty_range, params.arty_base_cost,
             params.arty_cost_exponent, params.arty_min_coverage_threshold),
        ]
        for kind, radius, base, exp, threshold in place_specs:
            result = _best_placement_action(
                world, self.requisition, kind, radius, base, exp, threshold,
            )
            if result is None:
                continue
            gain, coord, cost = result
            out.append(_Action(
                ratio=gain / cost, cost=cost, op="place",
                target_kind=kind, target_coord=coord,
            ))

        move_specs = [
            ("fob", params.fob_radius, params.fob_move_cost),
            ("artillery", params.arty_range, params.arty_move_cost),
        ]
        for kind, radius, move_cost in move_specs:
            result = _best_move_action(world, self.requisition, kind, radius, move_cost)
            if result is None:
                continue
            improvement, source_pid, dest_coord, cost = result
            out.append(_Action(
                ratio=improvement / cost, cost=cost, op="move",
                target_kind=kind, target_coord=dest_coord, source_pid=source_pid,
            ))

        return out

    def _commit(self, world: "World", action: "_Action") -> None:
        if action.op == "place":
            poi = world.place_build_site(
                action.target_kind, Ownership.SUPER_EARTH, action.target_coord,
            )
            if poi is None:
                return
        elif action.op == "move":
            # Source could have been destroyed between scoring and commit
            # (concurrent flip or other side effect). Bail out cleanly.
            if action.source_pid is None or action.source_pid not in world.pois:
                return
            world.remove_poi(action.source_pid)
            self._stale_counters.pop(action.source_pid, None)
            poi = world.place_build_site(
                action.target_kind, Ownership.SUPER_EARTH, action.target_coord,
                duration_ticks=world.params.move_build_ticks,
            )
            if poi is None:
                # Placement failed after teardown — source is already gone.
                # Rare (placement rules already validated during scoring).
                return
        else:
            return

        self.requisition -= action.cost

    # --------------------------------------------------------------- #
    # Decommission
    # --------------------------------------------------------------- #

    def _update_stale_and_decommission(self, world: "World") -> None:
        params = world.params

        eligible_pids: set[str] = set()
        to_remove: list[str] = []

        for pid, poi in world.pois.items():
            if poi.owner != Ownership.SUPER_EARTH:
                continue
            if poi.kind not in ("fob", "artillery"):
                continue
            eligible_pids.add(pid)
            radius = params.fob_radius if poi.kind == "fob" else params.arty_range
            # Individual coverage: contested cells in this POI's radius,
            # ignoring overlap with others. The check is "is this POI
            # contributing anything at all", not "is it contributing
            # *uniquely*". A redundant FOB still buffs its cells.
            coverage = _coverage_gain(world, poi.coord, radius, set())
            if coverage == 0:
                self._stale_counters[pid] = self._stale_counters.get(pid, 0) + 1
                if self._stale_counters[pid] >= params.decommission_stale_ticks:
                    to_remove.append(pid)
            else:
                self._stale_counters.pop(pid, None)

        # Drop counters for POIs that disappeared since the last pass
        # (flipped to enemy, removed by some other path, etc.).
        for pid in list(self._stale_counters.keys()):
            if pid not in eligible_pids:
                del self._stale_counters[pid]

        for pid in to_remove:
            world.remove_poi(pid)
            self._stale_counters.pop(pid, None)


@dataclass
class _Action:
    """Tagged candidate for the strategic pass. ``op`` is "place" or "move";
    ``source_pid`` is set only on move actions.
    """
    ratio: float
    cost: float
    op: str
    target_kind: str
    target_coord: Coord
    source_pid: str | None = None


def _best_placement_action(
    world: "World",
    requisition: float,
    kind: str,
    radius: int,
    base_cost: float,
    cost_exponent: float,
    min_threshold: int,
) -> tuple[int, Coord, float] | None:
    """Score candidate placements of ``kind`` (one of "fob", "artillery") over
    SE-defender non-contested cells. Returns ``(gain, coord, cost)`` for the
    best above-threshold affordable choice, or None.

    Gain counts contested SE-attacker cells within ``radius`` of the candidate
    that aren't already covered by an existing SE POI of the same kind. Ties
    are broken by lexicographic coord order so placement doesn't depend on
    grid insertion order.
    """
    # Pending build sites of this kind count toward n (so concurrent builds
    # aren't free) and toward existing-coord exclusion (so we don't stack a
    # second build on the same cell).
    se_coords = _se_kind_coords(world, kind)
    cost = base_cost * ((len(se_coords) + 1) ** cost_exponent)
    if requisition < cost:
        return None

    covered = _existing_coverage(world, kind, radius)

    best: tuple[int, Coord] | None = None
    for cell in world.grid.values():
        if cell.defender != Ownership.SUPER_EARTH or cell.attacker is not None:
            continue
        if cell.coord in se_coords:
            continue
        gain = _coverage_gain(world, cell.coord, radius, covered)
        if gain < min_threshold:
            continue
        if best is None or gain > best[0] or (gain == best[0] and cell.coord < best[1]):
            best = (gain, cell.coord)

    if best is None:
        return None
    return best[0], best[1], cost


def _best_move_action(
    world: "World",
    requisition: float,
    kind: str,
    radius: int,
    move_cost: float,
) -> tuple[int, str, Coord, float] | None:
    """For each existing SE POI of ``kind`` (build sites can't be moved), find
    the best alternative cell. Returns ``(improvement, source_pid, dest_coord,
    cost)`` for the best move that's affordable and has positive improvement,
    or None.

    "Improvement" = ``alt_gain - source_unique_coverage`` where source_unique
    is contested cells in source's radius that *only* this POI covers (i.e.,
    coverage that would be lost on teardown). This means a redundant FOB
    relocates more readily than one that's pulling unique weight.

    Note: ``improvement`` does not account for the ``move_build_ticks``
    window during which the destination contributes zero coverage. The
    planner can over-eagerly relocate marginal wins; if that becomes a
    problem, dampen with an expected-loss penalty proportional to
    ``source_unique * move_build_ticks``.
    """
    if requisition < move_cost:
        return None

    sources = [(pid, poi) for pid, poi in world.pois.items()
               if poi.kind == kind and poi.owner == Ownership.SUPER_EARTH]
    if not sources:
        return None

    occupied = _se_kind_coords(world, kind)

    best: tuple[int, str, Coord] | None = None

    for source_pid, source_poi in sources:
        other_coverage = _existing_coverage_excluding(world, kind, radius, source_pid)
        source_unique = _coverage_gain(world, source_poi.coord, radius, other_coverage)

        alt: tuple[int, Coord] | None = None
        for cell in world.grid.values():
            if cell.defender != Ownership.SUPER_EARTH or cell.attacker is not None:
                continue
            if cell.coord in occupied:
                continue
            if cell.coord == source_poi.coord:
                continue
            gain = _coverage_gain(world, cell.coord, radius, other_coverage)
            if gain <= 0:
                continue
            if alt is None or gain > alt[0] or (gain == alt[0] and cell.coord < alt[1]):
                alt = (gain, cell.coord)

        if alt is None:
            continue
        alt_gain, alt_coord = alt
        improvement = alt_gain - source_unique
        if improvement <= 0:
            continue

        if best is None or improvement > best[0] or (
            improvement == best[0] and source_pid < best[1]
        ):
            best = (improvement, source_pid, alt_coord)

    if best is None:
        return None
    return best[0], best[1], best[2], move_cost


def _se_kind_coords(world: "World", kind: str) -> set[Coord]:
    """Coords occupied by an SE POI of ``kind`` or a pending build site
    targeting ``kind``."""
    return {p.coord for p in world.pois.values()
            if p.owner == Ownership.SUPER_EARTH and (
                p.kind == kind
                or (p.kind == "build_site" and p.state.get("target_kind") == kind)
            )}


def _existing_coverage(world: "World", kind: str, radius: int) -> set[Coord]:
    """Coords (inclusive of POI cell itself) already within reach of an SE
    POI of ``kind`` — including pending build sites whose target_kind
    matches, so the planner doesn't double-build the same cluster.
    Recomputed each strategic pass — O(n_pois * radius^2) and only runs
    once per strategic period."""
    return _existing_coverage_excluding(world, kind, radius, exclude_pid=None)


def _existing_coverage_excluding(
    world: "World", kind: str, radius: int, exclude_pid: str | None,
) -> set[Coord]:
    """Same as ``_existing_coverage`` but skips the POI with id ``exclude_pid``.
    Used for move scoring where the source's own coverage doesn't count
    against alternative candidates (the source is being torn down).
    """
    covered: set[Coord] = set()
    for pid, poi in world.pois.items():
        if exclude_pid is not None and pid == exclude_pid:
            continue
        if poi.owner != Ownership.SUPER_EARTH:
            continue
        is_kind = poi.kind == kind
        is_pending_kind = (poi.kind == "build_site"
                           and poi.state.get("target_kind") == kind)
        if not (is_kind or is_pending_kind):
            continue
        for cc in cells_within(poi.coord, radius):
            covered.add(cc)
    return covered


def _coverage_gain(world: "World", coord: Coord, radius: int, covered: set[Coord]) -> int:
    """Contested SE-attacker cells within ``radius`` of ``coord`` (inclusive
    of center) that aren't already in ``covered``. Pass ``covered=set()`` to
    get the unfiltered "individual coverage" of a cell.
    """
    gain = 0
    for cc in cells_within(coord, radius):
        if cc in covered:
            continue
        cell = world.grid.get(cc)
        if cell is None:
            continue
        if cell.attacker == Ownership.SUPER_EARTH:
            gain += 1
    return gain
