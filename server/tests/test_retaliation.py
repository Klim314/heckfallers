"""Retaliation (conquer salients) — gauge dynamics + leapfrogging wedge.

Covers the rubber-banding feedback loop and the new staging→activation→
spread mechanics described in docs/conquer-salient-redesign.md:

- world.retaliation_gauge integrates SE flips up, enemy flips down,
  decays per tick, floors at 0.
- world._recent_se_flips records SE captures with timestamps; pruned to
  recent_se_flip_window_ticks each step.
- find_recent_flip_clusters returns up to K spread-out centers from the
  buffer (still used by the controller, called with k=1).
- spawn_conquer_staging places a staging POI on the closest enemy-defended
  cell to a target SE cell and registers a pre-activation conquer salient.
- update_salients drives the state machine: charge timer, activation,
  tracked-cells pruning, natural extinction, intercept-on-staging-loss.
- activate_conquer_salient picks fan cells in the forward hemisphere,
  freezes the centroid axis, removes the staging POI.
- on_cell_flip rolls forward-only spread with additive probability and
  per-generation decay.
- apply_salient_pressure stamps tracked_cells (post-activation only) and
  saturates by max when corridors and conquer footprints overlap.
- The controller fires when gauge >= threshold + buffer non-empty +
  under cap, drains on fire, holds when capped.
"""
from __future__ import annotations

import random
from unittest.mock import patch

from server.sim import salient as salient_mod
from server.sim.cell import Cell, Ownership
from server.sim.grid import NEIGHBOR_DIRS, distance
from server.sim.world import World

from .conftest import make_world, pin_deterministic_allocation, pin_fast_conquer_charge


def _hex_world(qs: int = 9, rs: int = 5) -> World:
    """A 2D rectangular hex patch, all SE except the rightmost column.

    Wide enough that conquer wedges have room to spread without bumping
    the grid edge in tests, and the rightmost column gives an enemy front.
    """
    w = World()
    pin_deterministic_allocation(w)
    for q in range(qs):
        for r in range(rs):
            defender = Ownership.ENEMY if q == qs - 1 else Ownership.SUPER_EARTH
            is_capital = (q, r) == (qs - 1, 0)
            w.grid[(q, r)] = Cell(coord=(q, r), defender=defender, is_capital=is_capital)
    return w


# --------------------------------------------------------------------- #
# Gauge dynamics — _flip_cell hook + step decay
# --------------------------------------------------------------------- #


def test_gauge_starts_at_zero():
    w = _hex_world()
    assert w.retaliation_gauge == 0.0
    assert w._recent_se_flips == []


def test_se_flip_pushes_gauge_up_and_records_buffer():
    w = _hex_world()
    cell = w.grid[(2, 2)]
    cell.attacker = Ownership.SUPER_EARTH

    w._flip_cell(cell, Ownership.SUPER_EARTH)

    assert w.retaliation_gauge == w.params.retaliation_w_se_flip
    assert w._recent_se_flips == [((2, 2), 0)]


def test_enemy_flip_drains_gauge_clamped_at_zero():
    w = _hex_world()
    w.retaliation_gauge = 0.5  # less than the drain weight
    cell = w.grid[(2, 2)]
    cell.defender = Ownership.SUPER_EARTH
    cell.attacker = Ownership.ENEMY

    w._flip_cell(cell, Ownership.ENEMY)

    assert w.retaliation_gauge == 0.0
    # Enemy flips don't enter the SE-flip buffer.
    assert w._recent_se_flips == []


def test_step_applies_decay_to_gauge():
    w = _hex_world()
    w.retaliation_gauge = 10.0
    w.match_state = "running"
    w.step()
    assert w.retaliation_gauge == 10.0 - w.params.retaliation_gauge_decay_per_tick


def test_step_decay_floors_at_zero():
    w = _hex_world()
    w.retaliation_gauge = w.params.retaliation_gauge_decay_per_tick * 0.5
    w.match_state = "running"
    w.step()
    assert w.retaliation_gauge == 0.0


def test_step_prunes_old_se_flips_outside_window():
    w = _hex_world()
    window = w.params.recent_se_flip_window_ticks
    w._recent_se_flips = [((1, 1), -window - 5), ((2, 2), -1)]
    w.match_state = "running"
    w.tick = 0
    w.step()
    coords = [c for c, _ in w._recent_se_flips]
    assert (1, 1) not in coords
    assert (2, 2) in coords


# --------------------------------------------------------------------- #
# find_recent_flip_clusters — still used by the controller (k=1)
# --------------------------------------------------------------------- #


def test_clusters_empty_buffer_returns_empty():
    assert salient_mod.find_recent_flip_clusters([], k=3, radius=2,
                                                 window_ticks=100,
                                                 current_tick=10) == []


def test_clusters_single_dense_spot():
    buf = [((5, 5), 1), ((5, 6), 2), ((6, 5), 3), ((4, 5), 4)]
    centers = salient_mod.find_recent_flip_clusters(
        buf, k=3, radius=2, window_ticks=100, current_tick=10
    )
    assert len(centers) >= 1
    assert centers[0] in {c for c, _ in buf}


def test_clusters_respects_window():
    buf = [((2, 2), 0), ((10, 10), 50)]
    centers = salient_mod.find_recent_flip_clusters(
        buf, k=2, radius=2, window_ticks=10, current_tick=55
    )
    assert centers == [(10, 10)]


def test_clusters_caps_at_k():
    buf = [((0, 0), 1), ((10, 0), 2), ((0, 10), 3)]
    centers = salient_mod.find_recent_flip_clusters(
        buf, k=1, radius=1, window_ticks=100, current_tick=10
    )
    assert len(centers) == 1


# --------------------------------------------------------------------- #
# spawn_conquer_staging — telegraph + registration
# --------------------------------------------------------------------- #


def test_staging_spawn_places_poi_on_closest_enemy_cell():
    w = _hex_world()
    target = (2, 2)  # SE cell, deep in SE territory
    sal = salient_mod.spawn_conquer_staging(w, target)
    assert sal is not None
    assert sal.kind == "conquer"
    assert sal.activated is False
    assert sal.staging_poi_id is not None
    staging = w.pois[sal.staging_poi_id]
    assert staging.kind == "salient_staging"
    assert staging.owner == Ownership.ENEMY
    # Closest enemy-defended cell to (2,2) is (8,2) (rightmost column).
    assert staging.coord[0] == 8
    # Staging POI carries charge + back-pointer.
    assert "charge_completes_at" in staging.state
    assert staging.state["parent_salient_id"] == sal.id


def test_staging_spawn_emits_event():
    w = _hex_world()
    salient_mod.spawn_conquer_staging(w, (2, 2))
    assert any(e["type"] == "salient_staging_spawned" for e in w.match_events)


def test_staging_spawn_returns_none_when_no_enemy_cells():
    w = _hex_world()
    for cell in w.grid.values():
        cell.defender = Ownership.SUPER_EARTH
        cell.is_capital = False
    sal = salient_mod.spawn_conquer_staging(w, (2, 2))
    assert sal is None


def test_staging_cell_has_siege_multiplier():
    """The staging POI's own cell takes longer for divers to flip."""
    w = _hex_world()
    sal = salient_mod.spawn_conquer_staging(w, (2, 2))
    assert sal is not None
    staging = w.pois[sal.staging_poi_id]
    cell = w.grid[staging.coord]
    threshold = w._effective_threshold(cell)
    assert threshold == w.params.flip_threshold * w.params.conquer_staging_siege_mult


# --------------------------------------------------------------------- #
# Counterplay A — kill staging POI before charge completes
# --------------------------------------------------------------------- #


def test_intercept_when_staging_poi_destroyed_before_charge():
    w = _hex_world()
    sal = salient_mod.spawn_conquer_staging(w, (2, 2))
    assert sal is not None
    staging_id = sal.staging_poi_id

    # Simulate divers clearing the staging POI by removing it directly.
    w.remove_poi(staging_id)
    salient_mod.update_salients(w)

    assert sal.id not in w.salients
    assert any(
        e["type"] == "salient_ended" and e.get("reason") == "intercepted"
        for e in w.match_events
    )
    # No fan cells were ever contested.
    assert all(c.attacker is None or c.defender == Ownership.ENEMY
               for c in w.grid.values()
               if c.defender == Ownership.SUPER_EARTH)


def test_intercept_via_natural_flip_of_staging_host_cell():
    """End-to-end counterplay A: SE captures the staging POI's host cell.

    Exercises the real teardown path — _flip_cell destroys opposite-owner
    POIs on the captured cell ([world.py:274-278](server/sim/world.py#L274)),
    then update_salients sees the staging POI is gone and ends the salient.
    """
    w = _hex_world()
    sal = salient_mod.spawn_conquer_staging(w, (2, 2))
    assert sal is not None
    staging_id = sal.staging_poi_id
    staging_coord = w.pois[staging_id].coord

    # SE captures the staging POI's host cell via the normal flip pathway.
    host_cell = w.grid[staging_coord]
    host_cell.attacker = Ownership.SUPER_EARTH
    w._flip_cell(host_cell, Ownership.SUPER_EARTH)

    # The staging POI was destroyed by _flip_cell's opposite-owner teardown.
    assert staging_id not in w.pois
    # The salient is still registered until update_salients reaps it.
    assert sal.id in w.salients

    salient_mod.update_salients(w)

    assert sal.id not in w.salients
    assert any(
        e["type"] == "salient_ended" and e.get("reason") == "intercepted"
        for e in w.match_events
    )


# --------------------------------------------------------------------- #
# Activation — timer fires update_salients into activate_conquer_salient
# --------------------------------------------------------------------- #


def test_activation_fires_at_charge_completes_at():
    # Wider/taller grid so the staging POI has all 3 forward neighbors in-bounds
    # regardless of the rolled fan_size.
    w = _hex_world(qs=11, rs=9)
    pin_fast_conquer_charge(w)
    # Force a known fan_size so the assertion is deterministic across rolls.
    with patch.object(random, "randint", return_value=2):
        sal = salient_mod.spawn_conquer_staging(w, (2, 4))
    assert sal is not None
    staging_id = sal.staging_poi_id

    # Advance the clock past charge_completes_at.
    charge_at = w.pois[staging_id].state["charge_completes_at"]
    w.tick = charge_at
    salient_mod.update_salients(w)

    assert sal.activated is True
    # Staging POI is gone post-activation.
    assert staging_id not in w.pois
    # fan_size cells are now tracked at gen=0.
    assert len(sal.tracked_cells) == sal.fan_size
    for coord, gen in sal.tracked_cells.items():
        assert gen == 0
        cell = w.grid[coord]
        assert cell.attacker == Ownership.ENEMY


def test_activation_emits_event():
    w = _hex_world(qs=11, rs=9)
    pin_fast_conquer_charge(w)
    with patch.object(random, "randint", return_value=2):
        sal = salient_mod.spawn_conquer_staging(w, (2, 4))
    assert sal is not None
    w.tick = w.pois[sal.staging_poi_id].state["charge_completes_at"]
    salient_mod.update_salients(w)
    assert any(e["type"] == "salient_activated" for e in w.match_events)


def test_fan_cells_lie_on_forward_hemisphere():
    """With axis pointing toward (-1, 0) (i.e. from staging at (8, 2) toward
    target SE cells in the negative-q half), the fan cells must have non-
    negative dot product with that axis (cube-coord), never lateral or
    backward."""
    w = _hex_world()
    pin_fast_conquer_charge(w)
    sal = salient_mod.spawn_conquer_staging(w, (2, 2))
    assert sal is not None
    staging_coord = w.pois[sal.staging_poi_id].coord

    w.tick = w.pois[sal.staging_poi_id].state["charge_completes_at"]
    salient_mod.update_salients(w)
    assert sal.activated

    # Original axis hint: target_se - staging = (2-8, 2-2) = (-6, 0).
    axis_hint = (-6, 0)
    from server.sim.grid import _axial_to_cube
    a = _axial_to_cube(axis_hint)
    for coord in sal.tracked_cells:
        d = (coord[0] - staging_coord[0], coord[1] - staging_coord[1])
        b = _axial_to_cube(d)
        dot = sum(x * y for x, y in zip(a, b))
        assert dot >= 0, f"fan cell {coord} (dir {d}) lies behind axis"


# --------------------------------------------------------------------- #
# on_cell_flip — forward-only, additive probability, generation decay
# --------------------------------------------------------------------- #


def _activated_salient(w: World, axis: tuple[int, int] = (-1, 0)) -> salient_mod.Salient:
    """Force a fully-activated conquer salient at a known position with a
    chosen axis. Bypasses the staging timer for tests that focus on spread."""
    sid = f"sal_{w._next_salient_id}"
    w._next_salient_id += 1
    sal = salient_mod.Salient(
        id=sid,
        kind="conquer",
        spawned_tick=w.tick,
        expires_tick=w.tick + 10_000,
        activated=True,
        axis=axis,
        fan_size=1,
    )
    w.salients[sid] = sal
    return sal


def test_spread_only_into_forward_hemisphere():
    """At p_base = 1.0, neighbors in the forward hemisphere may flip to
    contested; backward-hemisphere neighbors must never be touched."""
    w = _hex_world(qs=11, rs=7)
    w.params.conquer_spread_p_base = 1.0
    w.params.conquer_spread_decay_base = 1.0  # no decay for this test

    sal = _activated_salient(w, axis=(-1, 0))  # forward = -q direction
    seed = (5, 3)
    w.grid[seed].defender = Ownership.ENEMY
    w.grid[seed].attacker = None
    sal.tracked_cells[seed] = 0

    # Trigger the flip hook directly.
    salient_mod.on_cell_flip(w, seed, Ownership.ENEMY)

    # All newly-tracked cells must be in the forward hemisphere of axis=(-1, 0).
    from server.sim.grid import _axial_to_cube, forward_hemisphere
    forward = set(forward_hemisphere((-1, 0)))
    for coord in sal.tracked_cells:
        if coord == seed:
            continue
        d = (coord[0] - seed[0], coord[1] - seed[1])
        # Must be a single-step neighbor and in the forward hemisphere.
        assert distance(coord, seed) == 1
        assert d in forward


def test_spread_additive_probability_with_k_neighbors():
    """Two adjacent tracked cells contribute additively: P = 1 - (1-p)^k.
    With p_base=0.5 and k=2, P = 0.75. Mock random to verify both branches."""
    w = _hex_world(qs=11, rs=7)
    w.params.conquer_spread_p_base = 0.5
    w.params.conquer_spread_decay_base = 1.0

    sal = _activated_salient(w, axis=(-1, 0))

    # Place two adjacent tracked cells whose forward-hemisphere neighbors
    # overlap on a single SE cell. With axis=(-1, 0), forward dirs include
    # (-1, 0). Pick seeds (5, 3) and (5, 4); their shared forward neighbor
    # might be (4, 3) or (4, 4) — easier: place seeds (5, 3) and (4, 4),
    # both have (4, 3) in distance 1.
    seed_a = (5, 3)
    seed_b = (4, 4)
    target_n = (4, 3)  # both seeds are neighbors to (4, 3)
    assert distance(seed_a, target_n) == 1
    assert distance(seed_b, target_n) == 1

    w.grid[seed_a].defender = Ownership.ENEMY
    w.grid[seed_a].attacker = None
    w.grid[seed_b].defender = Ownership.ENEMY
    w.grid[seed_b].attacker = None
    sal.tracked_cells[seed_a] = 0
    sal.tracked_cells[seed_b] = 0

    # P_contest = 1 - (1 - 0.5)^2 = 0.75. Mock random to 0.74 — should contest.
    with patch.object(random, "random", return_value=0.74):
        salient_mod.on_cell_flip(w, seed_a, Ownership.ENEMY)
    assert target_n in sal.tracked_cells

    # Reset and try with random=0.76 — should NOT contest.
    del sal.tracked_cells[target_n]
    w.grid[target_n].attacker = None
    w.grid[target_n].progress = 0.0
    with patch.object(random, "random", return_value=0.76):
        salient_mod.on_cell_flip(w, seed_a, Ownership.ENEMY)
    assert target_n not in sal.tracked_cells


def test_generation_decay_halves_probability_per_gen():
    """At decay_base = 0.5 and gen=2, effective p = p_base * 0.25.
    With p_base = 1.0 and gen=2, p_per_source = 0.25; with k=1, P = 0.25.
    Mock random to 0.24 (contests) vs 0.26 (does not)."""
    w = _hex_world(qs=11, rs=7)
    w.params.conquer_spread_p_base = 1.0
    w.params.conquer_spread_decay_base = 0.5

    sal = _activated_salient(w, axis=(-1, 0))
    # Seed at gen=1 — its neighbor will be considered at gen_new=2.
    seed = (5, 3)
    w.grid[seed].defender = Ownership.ENEMY
    w.grid[seed].attacker = None
    sal.tracked_cells[seed] = 1

    # Contest case at random=0.24.
    with patch.object(random, "random", return_value=0.24):
        salient_mod.on_cell_flip(w, seed, Ownership.ENEMY)
    # At least one new neighbor was contested at gen=2.
    assert any(g == 2 for g in sal.tracked_cells.values())


def test_max_gen_cap_prevents_deep_spread():
    """conquer_max_gen=2 stops spread at depth 2 even with p_base=1.0."""
    w = _hex_world(qs=11, rs=7)
    w.params.conquer_spread_p_base = 1.0
    w.params.conquer_spread_decay_base = 1.0
    w.params.conquer_max_gen = 2

    sal = _activated_salient(w, axis=(-1, 0))
    seed = (5, 3)
    w.grid[seed].defender = Ownership.ENEMY
    w.grid[seed].attacker = None
    sal.tracked_cells[seed] = 2  # already at max_gen

    salient_mod.on_cell_flip(w, seed, Ownership.ENEMY)
    # No new gen-3 cells added.
    assert all(g <= 2 for g in sal.tracked_cells.values())
    # And no new tracked cells beyond the seed.
    assert len(sal.tracked_cells) == 1


# --------------------------------------------------------------------- #
# Counterplay B — repulse drops a tracked cell, reducing k for neighbors
# --------------------------------------------------------------------- #


def test_repulse_drops_tracked_cell_and_lowers_k():
    """Repulsing a contested tracked cell back to SE-defended drops it from
    tracked_cells, so adjacent neighbors see a lower k on subsequent rolls."""
    w = _hex_world(qs=11, rs=7)
    sal = _activated_salient(w, axis=(-1, 0))
    a = (5, 3)
    b = (4, 4)
    w.grid[a].defender = Ownership.SUPER_EARTH
    w.grid[a].attacker = Ownership.ENEMY  # contested but not yet flipped
    w.grid[b].defender = Ownership.ENEMY
    w.grid[b].attacker = None  # already flipped
    sal.tracked_cells[a] = 0
    sal.tracked_cells[b] = 0

    # Now divers repulse cell `a` — defender stays SE, attacker clears.
    w.grid[a].attacker = None
    w.grid[a].progress = 0.0

    salient_mod.update_salients(w)

    # Cell a was dropped from tracked_cells.
    assert a not in sal.tracked_cells
    # Cell b (defender=ENEMY, attacker=None) stays — flipped salient cells
    # remain tracked so they continue contributing to k for their neighbors.
    assert b in sal.tracked_cells


def test_repulse_lowers_k_on_subsequent_spread_roll():
    """End-to-end counterplay B: repulsing a tracked cell measurably lowers
    the spread probability for shared-neighbor cells on the next flip.

    With p_base=0.5: k=2 → P=0.75; k=1 → P=0.5. A roll of 0.6 contests at
    k=2 but does not at k=1.
    """
    w = _hex_world(qs=11, rs=7)
    w.params.conquer_spread_p_base = 0.5
    w.params.conquer_spread_decay_base = 1.0

    sal = _activated_salient(w, axis=(-1, 0))
    seed_a = (5, 3)
    seed_b = (4, 4)
    target_n = (4, 3)
    assert distance(seed_a, target_n) == 1
    assert distance(seed_b, target_n) == 1

    # Both seeds flipped to enemy (defender=ENEMY, attacker=None) and tracked.
    w.grid[seed_a].defender = Ownership.ENEMY
    w.grid[seed_a].attacker = None
    w.grid[seed_b].defender = Ownership.SUPER_EARTH
    w.grid[seed_b].attacker = Ownership.ENEMY  # contested but not yet flipped
    sal.tracked_cells[seed_a] = 0
    sal.tracked_cells[seed_b] = 0

    # Divers repulse seed_b before it flips. Pruning happens in update_salients.
    w.grid[seed_b].attacker = None
    w.grid[seed_b].progress = 0.0
    salient_mod.update_salients(w)
    assert seed_b not in sal.tracked_cells
    assert seed_a in sal.tracked_cells  # still tracked (defender=ENEMY)

    # Now a flip event on seed_a rolls with k=1 instead of k=2 for target_n.
    # P_contest at k=1 is 0.5; random=0.6 > 0.5 → no contest.
    with patch.object(random, "random", return_value=0.6):
        salient_mod.on_cell_flip(w, seed_a, Ownership.ENEMY)
    assert target_n not in sal.tracked_cells, (
        "k=1 should give P=0.5; random=0.6 must not contest"
    )


# --------------------------------------------------------------------- #
# Natural extinction
# --------------------------------------------------------------------- #


def test_natural_extinction_when_all_tracked_cells_repulsed():
    w = _hex_world(qs=11, rs=7)
    sal = _activated_salient(w, axis=(-1, 0))
    coord = (5, 3)
    w.grid[coord].defender = Ownership.SUPER_EARTH
    w.grid[coord].attacker = Ownership.ENEMY
    sal.tracked_cells[coord] = 0

    # Divers repulse the only tracked cell.
    w.grid[coord].attacker = None
    w.grid[coord].progress = 0.0
    salient_mod.update_salients(w)

    assert sal.id not in w.salients
    assert any(
        e["type"] == "salient_ended" and e.get("reason") == "extinguished"
        for e in w.match_events
    )


# --------------------------------------------------------------------- #
# Lifetime expiry — unchanged
# --------------------------------------------------------------------- #


def test_conquer_expires_on_lifetime():
    w = _hex_world()
    pin_fast_conquer_charge(w)
    sal = salient_mod.spawn_conquer_staging(w, (2, 2))
    assert sal is not None
    w.tick = sal.expires_tick
    salient_mod.update_salients(w)
    assert sal.id not in w.salients
    assert any(
        e["type"] == "salient_ended" and e.get("reason") == "expired"
        for e in w.match_events
    )
    assert not any(
        e["type"] == "salient_ended" and e.get("reason") == "success"
        for e in w.match_events
    )


# --------------------------------------------------------------------- #
# apply_salient_pressure — pre-activation no-op + post-activation tracked
# --------------------------------------------------------------------- #


def test_pre_activation_stamps_no_pressure():
    w = _hex_world()
    sal = salient_mod.spawn_conquer_staging(w, (2, 2))
    assert sal is not None
    salient_mod.apply_salient_pressure(w)
    for cell in w.grid.values():
        assert cell.salient_pressure == 0.0


def test_activated_conquer_stamps_tracked_cells():
    w = _hex_world()
    sal = _activated_salient(w, axis=(-1, 0))
    coord = (3, 2)
    w.grid[coord].defender = Ownership.SUPER_EARTH
    w.grid[coord].attacker = Ownership.ENEMY
    sal.tracked_cells[coord] = 0

    salient_mod.apply_salient_pressure(w)
    assert w.grid[coord].salient_pressure == w.params.conquer_pressure_magnitude
    # Cells outside tracked_cells unaffected.
    assert w.grid[(0, 0)].salient_pressure == 0.0


def test_destroy_overrides_conquer_on_overlap():
    """Where a destroy corridor and an activated conquer footprint overlap,
    the higher destroy magnitude must win (max-saturate, not additive)."""
    w = make_world(width=8)
    poi = w.place_poi("artillery", Ownership.SUPER_EARTH, (3, 0))
    destroy = salient_mod.spawn_destroy_salient(w, poi.id)
    assert destroy is not None
    # Force-activate a conquer salient covering a corridor cell.
    sal = _activated_salient(w, axis=(-1, 0))
    overlap_coord = destroy.corridor[2]
    sal.tracked_cells[overlap_coord] = 0

    salient_mod.apply_salient_pressure(w)
    assert w.grid[overlap_coord].salient_pressure == w.params.salient_pressure_magnitude


# --------------------------------------------------------------------- #
# Post-flip cells stay in tracked_cells
# --------------------------------------------------------------------- #


def test_post_flip_cells_stay_tracked_for_k_contribution():
    """A flipped salient cell (defender=ENEMY, attacker=None) remains in
    tracked_cells so it keeps contributing to k for its neighbors."""
    w = _hex_world(qs=11, rs=7)
    sal = _activated_salient(w, axis=(-1, 0))
    flipped = (5, 3)
    w.grid[flipped].defender = Ownership.ENEMY
    w.grid[flipped].attacker = None
    sal.tracked_cells[flipped] = 0

    salient_mod.update_salients(w)
    assert flipped in sal.tracked_cells


# --------------------------------------------------------------------- #
# Controller — _maybe_spawn_retaliation_salient (now produces staging)
# --------------------------------------------------------------------- #


def test_controller_no_spawn_below_threshold():
    w = _hex_world()
    w.retaliation_gauge = w.params.retaliation_gauge_threshold - 1.0
    w._recent_se_flips = [((2, 2), 0)]
    w.controller._maybe_spawn_retaliation_salient(w)
    assert all(s.kind != "conquer" for s in w.salients.values())


def test_controller_spawns_staging_at_threshold():
    """Gauge crossing produces a pre-activation conquer salient with a
    staging POI — not an active spreading salient."""
    w = _hex_world()
    w.retaliation_gauge = w.params.retaliation_gauge_threshold
    w._recent_se_flips = [((2, 2), 0), ((3, 2), 0), ((2, 3), 0)]
    w.controller._maybe_spawn_retaliation_salient(w)
    conquer = [s for s in w.salients.values() if s.kind == "conquer"]
    assert len(conquer) == 1
    assert conquer[0].activated is False
    assert conquer[0].staging_poi_id is not None
    assert conquer[0].staging_poi_id in w.pois
    assert w.retaliation_gauge == 0.0


def test_controller_holds_gauge_when_at_cap_counting_staging():
    """The cap counts staging-phase salients too — they occupy a slot."""
    w = _hex_world()
    # Pre-spawn a staging-phase conquer salient to fill the cap.
    sal = salient_mod.spawn_conquer_staging(w, (2, 2))
    assert sal is not None
    assert sal.activated is False
    pre_count = sum(1 for s in w.salients.values() if s.kind == "conquer")

    w.retaliation_gauge = w.params.retaliation_gauge_threshold + 5.0
    w._recent_se_flips = [((6, 2), 0)]
    w.controller._maybe_spawn_retaliation_salient(w)

    post_count = sum(1 for s in w.salients.values() if s.kind == "conquer")
    assert post_count == pre_count
    assert w.retaliation_gauge >= w.params.retaliation_gauge_threshold


def test_controller_no_spawn_with_empty_buffer():
    w = _hex_world()
    w.retaliation_gauge = w.params.retaliation_gauge_threshold + 1.0
    w._recent_se_flips = []
    w.controller._maybe_spawn_retaliation_salient(w)
    assert all(s.kind != "conquer" for s in w.salients.values())
    assert w.retaliation_gauge >= w.params.retaliation_gauge_threshold


def test_reset_match_clears_gauge_state():
    w = _hex_world()
    w.scenario_name = "demo_planet"
    w.retaliation_gauge = 99.0
    w._recent_se_flips = [((1, 1), 0)]
    w.reset_match()
    assert w.retaliation_gauge == 0.0
    assert w._recent_se_flips == []
