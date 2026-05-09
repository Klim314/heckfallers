"""HighCommandController — scaffolding + FOB / artillery siting (Phase 3).

Covers the Phase 1 contract (cadence, accrual, flag, reset, wire format),
Phase 2 FOB placement, and Phase 3 dual-action selection: each strategic
tick the planner picks the highest gain/cost action across FOB and
artillery candidates. Decommission and move actions land in later phases.
"""
from __future__ import annotations

from server.sim.cell import Ownership
from server.sim.controllers.high_command import HighCommandController

from .conftest import make_world


def _se_pois(world):
    return {pid: poi for pid, poi in world.pois.items() if poi.owner == Ownership.SUPER_EARTH}


def _se_fobs(world):
    """SE FOBs — both completed and pending build sites with target_kind=fob.
    The planner reasons over both equally, so for placement-decision tests
    they count interchangeably."""
    return [p for p in world.pois.values()
            if p.owner == Ownership.SUPER_EARTH and (
                p.kind == "fob"
                or (p.kind == "build_site" and p.state.get("target_kind") == "fob")
            )]


def _se_arty(world):
    """SE artillery — both completed and pending build sites."""
    return [p for p in world.pois.values()
            if p.owner == Ownership.SUPER_EARTH and (
                p.kind == "artillery"
                or (p.kind == "build_site" and p.state.get("target_kind") == "artillery")
            )]


def _se_build_sites(world):
    return [p for p in world.pois.values()
            if p.owner == Ownership.SUPER_EARTH and p.kind == "build_site"]


def _run_ticks(world, n: int) -> None:
    world.match_state = "running"
    for _ in range(n):
        world.step()


def _contest_cell(world, coord):
    """Mark an enemy-defender cell as SE-attacker contested. If the cell was
    SE-defender in the fixture, flip it to enemy first."""
    cell = world.grid[coord]
    cell.defender = Ownership.ENEMY
    cell.attacker = Ownership.SUPER_EARTH


class _NoOpEnemyController:
    name = "noop"

    def tick(self, world):
        pass


def _freeze_progress(world):
    """Stabilize the contested fixture across a multi-tick test run.

    Phase 2 placement tests care about *what the planner picks*, not
    pressure dynamics. Two side effects to suppress:

    - The diver allocator would hammer one or two contested cells with
      the full pool and capture them in a few seconds.
    - The opportunistic enemy controller spawns destroy salients that
      open contested corridors, which then flip via supply mechanics.

    Zero the diver pool and swap in a no-op enemy controller.

    Caveat: ``World.reset_match()`` re-instantiates both controllers
    from their default factories, undoing the swap. If a test composes
    freeze → reset → run, re-apply the freeze after the reset.
    """
    world.params.diver_pool = 0.0
    world.controller = _NoOpEnemyController()


def test_high_command_default_is_disabled():
    """Until later phases land, the flag is off and the pool stays at zero."""
    w = make_world()
    _run_ticks(w, 50)
    assert w.se_controller.requisition == 0.0


def test_high_command_accrues_requisition_when_enabled():
    """Smooth per-tick accrual — pool grows independently of strategic cadence."""
    w = make_world()
    w.params.high_command_enabled = True
    _run_ticks(w, 10)
    expected = 10 * w.params.requisition_per_tick
    assert abs(w.se_controller.requisition - expected) < 1e-6


def test_high_command_takes_no_action_when_disabled():
    """Flag off ⇒ no POI mutations even past several strategic periods."""
    w = make_world()
    initial_se = _se_pois(w)
    _run_ticks(w, w.params.high_command_period_ticks * 3 + 7)
    assert _se_pois(w) == initial_se


def test_high_command_takes_no_action_without_contested_cells():
    """No contested SE-attacker cells ⇒ coverage gain is zero everywhere ⇒ no FOB placed."""
    w = make_world()
    w.params.high_command_enabled = True
    initial_se = _se_pois(w)
    _run_ticks(w, w.params.high_command_period_ticks * 3 + 7)
    assert _se_pois(w) == initial_se


def test_high_command_controller_name_exposed():
    """Serializer reads .name; ensure the default instance has the expected
    label so the wire format is stable across phases."""
    ctrl = HighCommandController()
    assert ctrl.name == "high_command"


def test_reset_match_clears_requisition():
    """Accrued requisition must not leak between matches."""
    w = make_world()
    w.params.high_command_enabled = True
    _run_ticks(w, 20)
    assert w.se_controller.requisition > 0.0

    w.reset_match()
    assert w.se_controller.requisition == 0.0


def test_phase2_places_fob_when_affordable_and_contested_in_reach():
    """First FOB lands at the strategic tick after enough requisition has accrued."""
    w = make_world(width=8)
    w.params.high_command_enabled = True
    _freeze_progress(w)
    _contest_cell(w, (6, 0))   # contested SE-attacker cell

    # Run past the first strategic tick. With defaults (period=100,
    # accrual=0.5/tick, base=50, exp=2), the first opportunity is the
    # strategic gate after tick 100, by which point the pool can afford 50.
    _run_ticks(w, w.params.high_command_period_ticks + 5)

    fobs = _se_fobs(w)
    assert len(fobs) == 1
    # Placed within fob_radius of the contested cell so it actually covers something.
    from server.sim.grid import distance
    assert distance(fobs[0].coord, (6, 0)) <= w.params.fob_radius


def test_phase2_no_placement_when_unaffordable():
    """High base cost ⇒ requisition can't afford a placement during the run."""
    w = make_world(width=8)
    w.params.high_command_enabled = True
    _freeze_progress(w)
    w.params.fob_base_cost = 10_000.0
    _contest_cell(w, (6, 0))

    _run_ticks(w, w.params.high_command_period_ticks + 5)
    assert _se_fobs(w) == []
    # Requisition keeps accruing while waiting.
    assert w.se_controller.requisition > 0.0


def test_phase2_picks_highest_coverage_candidate():
    """Among multiple candidates, the one covering the most uncovered contested cells wins."""
    # Wider strip so two distinct candidate clusters exist.
    w = make_world(width=14)
    w.params.high_command_enabled = True
    _freeze_progress(w)
    # Cluster A: three contested cells around (3, 0). A FOB at (3, 0) (radius 2)
    # covers all three.
    _contest_cell(w, (2, 0))
    _contest_cell(w, (3, 0))
    _contest_cell(w, (4, 0))
    # Cluster B: one contested cell at (10, 0). A FOB nearby covers only one.
    _contest_cell(w, (10, 0))

    _run_ticks(w, w.params.high_command_period_ticks + 5)

    fobs = _se_fobs(w)
    assert len(fobs) == 1
    from server.sim.grid import distance
    # Placed FOB should sit closer to cluster A than cluster B.
    d_to_a = distance(fobs[0].coord, (3, 0))
    d_to_b = distance(fobs[0].coord, (10, 0))
    assert d_to_a < d_to_b


def test_phase2_cost_curve_slows_subsequent_placements():
    """Second FOB requires accruing past the (n+1)^2 cost. Artillery and
    move-action are suppressed (prohibitive costs) so this isolates
    sequential FOB placement — Phase 3 added artillery competition and
    Phase 4b added move-vs-place competition."""
    w = make_world(width=14)
    w.params.high_command_enabled = True
    _freeze_progress(w)
    w.params.arty_base_cost = 1e9      # keep artillery out of the running
    w.params.fob_move_cost = 1e9       # keep relocation out of the running
    w.params.arty_move_cost = 1e9
    w.params.decommission_stale_ticks = 10**9   # don't recycle the first FOB
    # Two non-overlapping contested clusters so two FOB placements both have
    # positive coverage gain.
    _contest_cell(w, (3, 0))
    _contest_cell(w, (10, 0))

    # cost(1)=50 lands ~tick 100; cost(2)=200 lands ~tick 500. cost(3)=450
    # would need ~tick 1400, well past the 600-tick window.
    _run_ticks(w, 600)
    assert len(_se_fobs(w)) == 2


def test_phase2_skips_cells_already_hosting_a_fob():
    """A cell with an existing SE FOB must not be picked again, even if its
    coverage gain would otherwise be highest."""
    w = make_world(width=8)
    w.params.high_command_enabled = True
    _freeze_progress(w)
    # Place a FOB manually first.
    placed = w.place_poi("fob", Ownership.SUPER_EARTH, (4, 0))
    assert placed is not None
    _contest_cell(w, (5, 0))   # within radius 2 of (4, 0); coverage already exists

    _run_ticks(w, w.params.high_command_period_ticks * 2 + 5)
    fobs = _se_fobs(w)
    coords = [f.coord for f in fobs]
    # First FOB still there; at most one new one (and not on the same cell).
    assert (4, 0) in coords
    assert coords.count((4, 0)) == 1


def test_phase3_fire_artillery_blocked_beyond_range():
    """Game-mechanic gate: firing at a target farther than arty_range fails."""
    w = make_world(width=12)
    poi = w.place_poi("artillery", Ownership.SUPER_EARTH, (3, 0))
    assert poi is not None
    w.params.arty_range = 3

    # Within range — should succeed.
    assert w.fire_artillery(poi.id, (5, 0)) is True
    # Reset state to remove the in-flight target so the next fire is clean.
    poi.state["target"] = None
    poi.state["expires_at"] = -1
    poi.state["shells"] = w.params.arty_default_shells

    # Beyond range — should fail and not consume a shell.
    shells_before = poi.state["shells"]
    assert w.fire_artillery(poi.id, (10, 0)) is False
    assert poi.state["shells"] == shells_before
    assert poi.state["target"] is None


def test_phase3_places_artillery_when_fob_disqualified():
    """When no FOB candidate qualifies (here via min-coverage threshold),
    the planner falls back to artillery if it can cover the front."""
    w = make_world(width=12)
    w.params.high_command_enabled = True
    w.params.fob_min_coverage_threshold = 99   # FOB can never qualify
    _freeze_progress(w)
    _contest_cell(w, (8, 0))

    # cost(0) artillery = 100 → affordable by ~tick 200; strategic gate
    # fires at tick 100, 200, 300.
    _run_ticks(w, w.params.high_command_period_ticks * 3 + 5)

    assert len(_se_arty(w)) >= 1
    assert _se_fobs(w) == []


def test_phase3_planner_picks_fob_first_when_both_viable():
    """At equal coverage, FOB's lower base cost wins by gain/cost ratio."""
    w = make_world(width=10)
    w.params.high_command_enabled = True
    _freeze_progress(w)
    _contest_cell(w, (5, 0))

    _run_ticks(w, w.params.high_command_period_ticks + 5)
    # FOB (gain=1, cost=50, ratio=0.02) beats artillery (gain=1, cost=100,
    # ratio=0.01) → FOB placed first.
    assert len(_se_fobs(w)) == 1
    assert _se_arty(w) == []


def test_phase3_artillery_skips_existing_arty_cells():
    """A cell already hosting an SE artillery is excluded from candidates,
    even when a second artillery placement is otherwise viable elsewhere."""
    # Wider strip so the second contested cell is out of range of the
    # pre-placed artillery — otherwise its existing coverage zeros the
    # gain everywhere and the test would pass for the wrong reason.
    w = make_world(width=18)
    w.params.high_command_enabled = True
    _freeze_progress(w)
    w.params.fob_min_coverage_threshold = 99   # keep FOB out of the running
    placed = w.place_poi("artillery", Ownership.SUPER_EARTH, (3, 0))
    assert placed is not None
    _contest_cell(w, (4, 0))    # near the existing artillery (already covered)
    _contest_cell(w, (12, 0))   # far cluster — drives a second placement

    # cost(2) = 100 * 4 = 400 → 800 ticks of accrual at 0.5/tick.
    _run_ticks(w, 900)

    arty = _se_arty(w)
    coords = [a.coord for a in arty]
    # A second artillery should have been placed (at the far cluster) —
    # otherwise we're not actually testing the skip logic.
    assert len(arty) == 2
    # Original at (3, 0) untouched, no duplicate stack.
    assert coords.count((3, 0)) == 1
    # The new artillery sits in range of the far cluster, not on (3, 0).
    new_coord = next(c for c in coords if c != (3, 0))
    from server.sim.grid import distance
    assert distance(new_coord, (12, 0)) <= w.params.arty_range


def test_phase4a_planner_creates_build_site_not_instant_fob():
    """Phase 4a: planner placement creates a build_site, not a FOB directly."""
    w = make_world(width=8)
    w.params.high_command_enabled = True
    _freeze_progress(w)
    _contest_cell(w, (6, 0))

    _run_ticks(w, w.params.high_command_period_ticks + 5)

    sites = _se_build_sites(w)
    assert len(sites) == 1
    assert sites[0].state["target_kind"] == "fob"
    # No completed FOB yet — site hasn't resolved.
    real_fobs = [p for p in w.pois.values()
                 if p.kind == "fob" and p.owner == Ownership.SUPER_EARTH]
    assert real_fobs == []


def test_phase4a_build_site_resolves_to_target_after_duration():
    """After fresh_build_ticks elapse, the build_site mutates into the target POI."""
    w = make_world(width=8)
    w.params.high_command_enabled = True
    w.params.fresh_build_ticks = 30   # speed test
    _freeze_progress(w)
    _contest_cell(w, (6, 0))

    # Build site placed at strategic tick 100. Resolves at tick 130.
    _run_ticks(w, w.params.high_command_period_ticks + w.params.fresh_build_ticks + 5)

    sites = _se_build_sites(w)
    assert sites == []
    real_fobs = [p for p in w.pois.values()
                 if p.kind == "fob" and p.owner == Ownership.SUPER_EARTH]
    assert len(real_fobs) == 1


def test_phase4a_build_site_provides_no_buff_during_construction():
    """A build_site should not contribute buffs to nearby contested cells —
    only the resolved POI does."""
    w = make_world(width=8)
    site = w.place_build_site("fob", Ownership.SUPER_EARTH, (4, 0))
    assert site is not None
    contested = w.grid[(5, 0)]
    contested.defender = Ownership.ENEMY
    contested.attacker = Ownership.SUPER_EARTH

    # effect_on must return 0 for the build site even though a real FOB at
    # (4, 0) would buff (5, 0).
    assert site.effect_on(contested, w) == 0.0


def test_phase4a_artillery_build_site_resolves_with_shells_and_state():
    """Artillery build site resolves into a fully-stateful artillery POI."""
    w = make_world(width=8)
    w.params.fresh_build_ticks = 5
    site = w.place_build_site("artillery", Ownership.SUPER_EARTH, (4, 0))
    assert site is not None

    # Tick the world enough to trigger resolve.
    w.match_state = "running"
    for _ in range(10):
        w.step()

    # The same POI id was mutated in place.
    poi = w.pois[site.id]
    assert poi.kind == "artillery"
    assert poi.state["shells"] == w.params.arty_default_shells
    assert poi.state.get("target") is None
    assert poi.state.get("expires_at", -1) == -1


def test_phase4a_build_site_destroyed_on_cell_flip():
    """If the build site's cell flips to enemy mid-construction, the site is
    destroyed (existing _flip_cell teardown handles owner-opposite POIs)."""
    w = make_world(width=8)
    site = w.place_build_site("fob", Ownership.SUPER_EARTH, (4, 0))
    assert site is not None
    # Force-flip (4, 0) to ENEMY by direct API call.
    w._flip_cell(w.grid[(4, 0)], Ownership.ENEMY)

    assert site.id not in w.pois
    assert _se_build_sites(w) == []


def test_phase4a_build_sites_count_toward_cost():
    """A pending FOB build site counts toward n_fobs so cost(2nd) uses (n+1)=2,
    preventing the planner from spamming concurrent builds."""
    from server.sim.controllers.high_command import _best_placement_action

    w = make_world(width=12)
    w.place_build_site("fob", Ownership.SUPER_EARTH, (3, 0))
    _contest_cell(w, (8, 0))   # leaves a candidate (8 is far from the build site)

    # With one pending FOB: n=1, cost = 50 * (1+1)^2 = 200. Below requisition
    # of 199 should not trigger a placement; >= 200 should.
    p = w.params
    too_low = _best_placement_action(
        w, 199.0, "fob", p.fob_radius, p.fob_base_cost,
        p.fob_cost_exponent, p.fob_min_coverage_threshold,
    )
    assert too_low is None

    enough = _best_placement_action(
        w, 200.0, "fob", p.fob_radius, p.fob_base_cost,
        p.fob_cost_exponent, p.fob_min_coverage_threshold,
    )
    assert enough is not None
    _, _, cost = enough
    assert cost == 200.0


def test_phase4a_enemy_targets_build_site_at_target_kind_value():
    """OpportunisticController treats a pending artillery build site as an
    artillery-valued target for destroy-salient spawning."""
    from server.sim.controllers.opportunistic import OpportunisticController

    w = make_world(width=8)
    site = w.place_build_site("artillery", Ownership.SUPER_EARTH, (3, 0))
    assert site is not None
    ctrl = OpportunisticController()

    w.tick = w.params.salient_period_ticks
    ctrl.tick(w)

    # Enemy spawns a destroy salient targeting the build site.
    assert len(w.salients) == 1
    sal = next(iter(w.salients.values()))
    assert sal.target == (3, 0)


def test_phase4b_decommissions_stale_fob():
    """A FOB whose individual coverage is zero for ``decommission_stale_ticks``
    consecutive strategic passes should be removed, freeing the slot."""
    w = make_world(width=8)
    w.params.high_command_enabled = True
    _freeze_progress(w)
    # Place a built FOB directly so the stale-counter sees it on the first
    # strategic tick. Use place_poi (instant) to skip the build window.
    w.place_poi("fob", Ownership.SUPER_EARTH, (3, 0))
    # No contested cells anywhere — coverage is zero from the start.

    # decommission_stale_ticks defaults to 3, so the FOB is removed after
    # 3 strategic passes (ticks 100, 200, 300).
    period = w.params.high_command_period_ticks
    needed = period * w.params.decommission_stale_ticks + 5

    _run_ticks(w, needed)

    fobs = [p for p in w.pois.values()
            if p.kind == "fob" and p.owner == Ownership.SUPER_EARTH]
    assert fobs == []


def test_phase4b_stale_counter_resets_on_renewed_coverage():
    """If coverage returns before the threshold, the counter resets and the
    POI is not decommissioned."""
    w = make_world(width=10)
    w.params.high_command_enabled = True
    _freeze_progress(w)
    # Zero the FOB buff so the contested cell we add later doesn't flip
    # under base_rate + buff before the next strategic pass observes it.
    w.params.fob_buff = 0.0
    w.place_poi("fob", Ownership.SUPER_EARTH, (3, 0))
    # Suppress placement competition so the planner doesn't fold this FOB
    # into a move/place decision and confuse the assertion.
    w.params.fob_base_cost = 1e9
    w.params.arty_base_cost = 1e9
    w.params.fob_move_cost = 1e9
    w.params.arty_move_cost = 1e9

    period = w.params.high_command_period_ticks

    # Tick 100, 200: zero coverage — counter goes to 2 (below threshold 3).
    _run_ticks(w, period * 2 + 5)
    assert (3, 0) in {p.coord for p in w.pois.values() if p.kind == "fob"}

    # Add a contested cell in radius before the next strategic pass.
    _contest_cell(w, (4, 0))

    # Tick 300: coverage > 0, counter resets. Tick 400, 500: still covered.
    # Without renewal, we'd have hit threshold at tick 300.
    _run_ticks(w, period * 3 + 5)
    fobs = [p for p in w.pois.values() if p.kind == "fob"]
    assert any(p.coord == (3, 0) for p in fobs)


def test_phase4b_move_relocates_stale_fob_to_better_cluster():
    """A FOB with zero unique coverage relocates to a better destination
    instead of getting decommissioned, when move improvement > 0.
    Asserts the action used move semantics (cost = move_cost, build site
    duration = move_build_ticks), not a fresh place."""
    w = make_world(width=14)
    w.params.high_command_enabled = True
    _freeze_progress(w)
    # Pre-place FOB at (1, 0) — far from any contested cell.
    w.place_poi("fob", Ownership.SUPER_EARTH, (1, 0))
    # Contest a cluster within reach of, say, (10, 0) but not (1, 0).
    _contest_cell(w, (10, 0))
    # Suppress fresh placements so move is the only positive-improvement
    # action available.
    w.params.fob_base_cost = 1e9
    w.params.arty_base_cost = 1e9

    period = w.params.high_command_period_ticks
    total_ticks = period + 5

    # Run one strategic period — move should fire on the first opportunity.
    _run_ticks(w, total_ticks)

    # The original FOB at (1, 0) is gone; a build site exists in range of (10, 0).
    coords = {p.coord for p in w.pois.values()
              if p.owner == Ownership.SUPER_EARTH}
    assert (1, 0) not in coords
    pending = _se_build_sites(w)
    assert len(pending) == 1
    site = pending[0]
    assert site.state["target_kind"] == "fob"

    from server.sim.grid import distance
    assert distance(site.coord, (10, 0)) <= w.params.fob_radius

    # Cost: move_cost was spent (not base_cost). Total pool = (every tick
    # accrued requisition_per_tick) − the one move debit.
    expected_pool = total_ticks * w.params.requisition_per_tick - w.params.fob_move_cost
    assert abs(w.se_controller.requisition - expected_pool) < 1e-6

    # Duration: build site uses move_build_ticks, not fresh_build_ticks.
    completes_at = site.state["completes_at"]
    # Site was placed during the strategic tick at world.tick == period (100).
    # completes_at should be that tick + move_build_ticks.
    assert completes_at == period + w.params.move_build_ticks


def test_phase4b_move_uses_shorter_build_window():
    """The build site created by a move uses ``move_build_ticks``, not
    ``fresh_build_ticks``."""
    w = make_world(width=14)
    w.params.high_command_enabled = True
    w.params.move_build_ticks = 7
    w.params.fresh_build_ticks = 999   # would not resolve in test window
    _freeze_progress(w)
    w.place_poi("fob", Ownership.SUPER_EARTH, (1, 0))
    _contest_cell(w, (10, 0))
    w.params.fob_base_cost = 1e9
    w.params.arty_base_cost = 1e9

    period = w.params.high_command_period_ticks
    _run_ticks(w, period + w.params.move_build_ticks + 5)

    real_fobs = [p for p in w.pois.values()
                 if p.kind == "fob" and p.owner == Ownership.SUPER_EARTH]
    # The relocated FOB has resolved despite fresh_build_ticks being huge.
    assert len(real_fobs) == 1


def test_phase4b_move_skips_when_no_improvement():
    """If every alternative gives the same gain as the source, no move
    should fire (improvement <= 0)."""
    w = make_world(width=8)
    w.params.high_command_enabled = True
    _freeze_progress(w)
    # FOB covering (5, 0). Best alt also gives gain=1; improvement=0.
    w.place_poi("fob", Ownership.SUPER_EARTH, (4, 0))
    _contest_cell(w, (5, 0))
    w.params.fob_base_cost = 1e9
    w.params.arty_base_cost = 1e9
    w.params.decommission_stale_ticks = 10**9

    _run_ticks(w, w.params.high_command_period_ticks * 3 + 5)

    fobs = [p for p in w.pois.values()
            if p.kind == "fob" and p.owner == Ownership.SUPER_EARTH]
    coords = {p.coord for p in fobs}
    assert (4, 0) in coords   # source untouched


def test_phase4b_decommissioned_poi_clears_stale_counter():
    """After decommission, the controller's stale-counter dict no longer
    holds the dead pid (no leak across many cycles)."""
    w = make_world(width=8)
    w.params.high_command_enabled = True
    _freeze_progress(w)
    poi = w.place_poi("fob", Ownership.SUPER_EARTH, (3, 0))
    assert poi is not None

    period = w.params.high_command_period_ticks
    _run_ticks(w, period * w.params.decommission_stale_ticks + 5)

    # FOB removed, counter cleaned up.
    assert poi.id not in w.pois
    assert poi.id not in w.se_controller._stale_counters


def test_wire_format_exposes_se_controller_fields():
    """Lock the wire contract before the client reads it."""
    from server.api.serialize import world_to_wire

    w = make_world()
    w.params.high_command_enabled = True
    _run_ticks(w, 4)

    wire = world_to_wire(w)
    assert wire["se_controller"] == "high_command"
    assert isinstance(wire["requisition"], float)
    assert wire["requisition"] == round(w.se_controller.requisition, 2)
