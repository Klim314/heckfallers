"""Retaliation (conquer salients) — gauge dynamics, cluster targeting, spawn.

Covers the rubber-banding feedback loop:

- world.retaliation_gauge integrates SE flips up, enemy flips down,
  decays per tick, floors at 0.
- world._recent_se_flips records SE captures with timestamps; pruned to
  recent_se_flip_window_ticks each step.
- find_recent_flip_clusters returns up to K spread-out centers from the
  buffer.
- spawn_conquer_salient builds a region from the centers and opens
  SE-defended uncontested cells to enemy contestation.
- apply_salient_pressure stamps conquer_pressure_magnitude on region
  cells and saturates by max when corridors and regions overlap.
- The controller fires when gauge >= threshold + buffer non-empty +
  under cap, drains on fire, holds when capped.
"""
from __future__ import annotations

from server.sim import salient as salient_mod
from server.sim.cell import Cell, Ownership
from server.sim.world import World

from .conftest import make_world


def _hex_world(qs: int = 9, rs: int = 5) -> World:
    """A 2D rectangular hex patch, all SE except the rightmost column.

    Wide enough that conquer cluster regions (radius 2) fit without
    bumping the grid edge in tests, and the rightmost column gives an
    enemy front so make_world-style helpers behave.
    """
    w = World()
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
    # After step, world.tick became 1; cutoff = 1 - window. The -window-5
    # entry is below cutoff and pruned; the -1 entry survives.
    coords = [c for c, _ in w._recent_se_flips]
    assert (1, 1) not in coords
    assert (2, 2) in coords


# --------------------------------------------------------------------- #
# find_recent_flip_clusters
# --------------------------------------------------------------------- #


def test_clusters_empty_buffer_returns_empty():
    assert salient_mod.find_recent_flip_clusters([], k=3, radius=2,
                                                 window_ticks=100,
                                                 current_tick=10) == []


def test_clusters_single_dense_spot():
    # All flips bunched around (5, 5).
    buf = [((5, 5), 1), ((5, 6), 2), ((6, 5), 3), ((4, 5), 4)]
    centers = salient_mod.find_recent_flip_clusters(
        buf, k=3, radius=2, window_ticks=100, current_tick=10
    )
    assert len(centers) >= 1
    # The densest center should be one of the actual flip coords.
    assert centers[0] in {c for c, _ in buf}


def test_clusters_separated_by_min_distance():
    """Two distinct hot spots far apart must yield two centers, not one."""
    # Cluster A around (2, 2), cluster B around (10, 10). 2*radius=4, so
    # any pick from A and any pick from B differ by way more than 4.
    buf = [
        ((2, 2), 1), ((2, 3), 2), ((3, 2), 3),
        ((10, 10), 4), ((10, 11), 5), ((11, 10), 6),
    ]
    centers = salient_mod.find_recent_flip_clusters(
        buf, k=2, radius=2, window_ticks=100, current_tick=10
    )
    assert len(centers) == 2
    # Distinct hot spots — pairs must be from different clusters.
    from server.sim.grid import distance
    assert distance(centers[0], centers[1]) > 2 * 2


def test_clusters_respects_window():
    """Old flips outside the window are excluded from clustering."""
    buf = [((2, 2), 0), ((10, 10), 50)]
    centers = salient_mod.find_recent_flip_clusters(
        buf, k=2, radius=2, window_ticks=10, current_tick=55
    )
    # Only the (10, 10) flip is within window (55 - 10 = 45 cutoff).
    assert centers == [(10, 10)]


def test_clusters_caps_at_k():
    """K=1 returns at most one center even when more clusters exist."""
    buf = [((0, 0), 1), ((10, 0), 2), ((0, 10), 3)]
    centers = salient_mod.find_recent_flip_clusters(
        buf, k=1, radius=1, window_ticks=100, current_tick=10
    )
    assert len(centers) == 1


# --------------------------------------------------------------------- #
# spawn_conquer_salient
# --------------------------------------------------------------------- #


def test_spawn_conquer_creates_salient_with_region():
    w = _hex_world()
    centers = [(2, 2)]
    sal = salient_mod.spawn_conquer_salient(w, centers)
    assert sal is not None
    assert sal.kind == "conquer"
    assert sal.target is None
    assert sal.target_poi_id is None
    # Region covers radius=2 (default param) around (2, 2).
    assert (2, 2) in sal.region
    assert (3, 2) in sal.region   # 1 hop
    assert (4, 2) in sal.region   # 2 hops


def test_spawn_conquer_opens_se_cells_to_enemy_contestation():
    w = _hex_world()
    sal = salient_mod.spawn_conquer_salient(w, [(2, 2)])
    assert sal is not None
    # Every SE-defended uncontested region cell should now have an enemy attacker.
    opened = 0
    for coord in sal.region:
        cell = w.grid[coord]
        if cell.defender == Ownership.SUPER_EARTH:
            assert cell.attacker == Ownership.ENEMY
            opened += 1
    assert opened > 0


def test_spawn_conquer_skips_cells_already_contested():
    w = _hex_world()
    # Pre-contest one cell — by SUPER_EARTH attacker (the "wrong" direction
    # for a conquer to override).
    pre = w.grid[(2, 2)]
    pre.defender = Ownership.SUPER_EARTH
    pre.attacker = Ownership.SUPER_EARTH
    sal = salient_mod.spawn_conquer_salient(w, [(2, 2)])
    assert sal is not None
    # The pre-contested cell's attacker must not have been overwritten.
    assert pre.attacker == Ownership.SUPER_EARTH


def test_spawn_conquer_returns_none_for_empty_centers():
    w = _hex_world()
    assert salient_mod.spawn_conquer_salient(w, []) is None


def test_spawn_conquer_dedups_overlapping_patches():
    """When two centers overlap, region cells appear once each."""
    w = _hex_world()
    # (2, 2) and (3, 2) are 1 hop apart; their radius-2 patches overlap heavily.
    sal = salient_mod.spawn_conquer_salient(w, [(2, 2), (3, 2)])
    assert sal is not None
    assert len(sal.region) == len(set(sal.region))


# --------------------------------------------------------------------- #
# apply_salient_pressure — kind-specific magnitude + max-saturation
# --------------------------------------------------------------------- #


def test_apply_pressure_stamps_conquer_region_at_lower_magnitude():
    w = _hex_world()
    sal = salient_mod.spawn_conquer_salient(w, [(2, 2)])
    assert sal is not None
    salient_mod.apply_salient_pressure(w)

    cmag = w.params.conquer_pressure_magnitude
    for coord in sal.region:
        cell = w.grid.get(coord)
        if cell is None:
            continue
        assert cell.salient_pressure == cmag
    # Cells outside the region should be untouched.
    outside = w.grid[(0, 0)]
    assert outside.salient_pressure == 0.0


def test_apply_pressure_destroy_overrides_conquer_on_overlap():
    """Where a destroy corridor and conquer region overlap, the higher
    destroy magnitude must win (max-saturate, not additive)."""
    w = make_world(width=8)
    poi = w.place_poi("artillery", Ownership.SUPER_EARTH, (3, 0))
    destroy = salient_mod.spawn_destroy_salient(w, poi.id)
    assert destroy is not None
    # Spawn a conquer salient covering a corridor cell.
    overlap_coord = destroy.corridor[2]
    conquer = salient_mod.spawn_conquer_salient(w, [overlap_coord])
    assert conquer is not None
    assert overlap_coord in conquer.region

    salient_mod.apply_salient_pressure(w)
    # The overlap cell carries destroy's higher magnitude.
    overlap_cell = w.grid[overlap_coord]
    assert overlap_cell.salient_pressure == w.params.salient_pressure_magnitude


# --------------------------------------------------------------------- #
# update_salients — conquer expires only on lifetime
# --------------------------------------------------------------------- #


def test_conquer_expires_on_lifetime_only():
    w = _hex_world()
    sal = salient_mod.spawn_conquer_salient(w, [(2, 2)])
    assert sal is not None
    # Mid-life: still alive.
    w.tick = sal.expires_tick - 1
    salient_mod.update_salients(w)
    assert sal.id in w.salients
    # Past lifetime: expired silently.
    w.tick = sal.expires_tick
    salient_mod.update_salients(w)
    assert sal.id not in w.salients
    # No success event — that's destroy-only.
    assert not any(e["type"] == "destroy_salient_success" for e in w.match_events)


# --------------------------------------------------------------------- #
# Controller — _maybe_spawn_retaliation_salient
# --------------------------------------------------------------------- #


def test_controller_no_spawn_below_threshold():
    w = _hex_world()
    w.retaliation_gauge = w.params.retaliation_gauge_threshold - 1.0
    w._recent_se_flips = [((2, 2), 0)]
    w.controller._maybe_spawn_retaliation_salient(w)
    assert all(s.kind != "conquer" for s in w.salients.values())


def test_controller_spawns_and_drains_at_threshold():
    w = _hex_world()
    w.retaliation_gauge = w.params.retaliation_gauge_threshold
    w._recent_se_flips = [((2, 2), 0), ((3, 2), 0), ((2, 3), 0)]
    w.controller._maybe_spawn_retaliation_salient(w)
    conquer = [s for s in w.salients.values() if s.kind == "conquer"]
    assert len(conquer) == 1
    assert w.retaliation_gauge == 0.0


def test_controller_holds_gauge_when_at_cap():
    """At cap, no new spawn AND gauge is held — fires the moment a slot opens."""
    w = _hex_world()
    # Pre-spawn enough conquer salients to hit the cap.
    for _ in range(w.params.max_active_conquer_salients):
        sal = salient_mod.spawn_conquer_salient(w, [(2, 2)])
        assert sal is not None
    pre_count = sum(1 for s in w.salients.values() if s.kind == "conquer")

    w.retaliation_gauge = w.params.retaliation_gauge_threshold + 5.0
    w._recent_se_flips = [((6, 2), 0)]
    w.controller._maybe_spawn_retaliation_salient(w)

    post_count = sum(1 for s in w.salients.values() if s.kind == "conquer")
    assert post_count == pre_count
    assert w.retaliation_gauge >= w.params.retaliation_gauge_threshold


def test_controller_no_spawn_with_empty_buffer():
    """At threshold but no recent SE flips — can't pick clusters, hold gauge."""
    w = _hex_world()
    w.retaliation_gauge = w.params.retaliation_gauge_threshold + 1.0
    w._recent_se_flips = []
    w.controller._maybe_spawn_retaliation_salient(w)
    assert all(s.kind != "conquer" for s in w.salients.values())
    # Gauge isn't drained — there's no salient to drain *for*.
    assert w.retaliation_gauge >= w.params.retaliation_gauge_threshold


def test_reset_match_clears_gauge_state():
    w = _hex_world()
    w.scenario_name = "demo_planet"
    w.retaliation_gauge = 99.0
    w._recent_se_flips = [((1, 1), 0)]
    w.reset_match()
    assert w.retaliation_gauge == 0.0
    assert w._recent_se_flips == []
