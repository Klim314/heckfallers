"""Factory primitive — placement, target selection, pressure application."""
from __future__ import annotations

import random

from server.sim import factory as factory_mod
from server.sim.cell import Cell, Ownership
from server.sim.world import World

from .conftest import make_world, pin_deterministic_allocation


def _wider_strip(width: int = 8, enemy_from: int = 5) -> World:
    """Strip world with the rightmost ``width - enemy_from`` cells enemy.

    Default conftest only has the capital enemy, which makes it impossible
    to place a factory that isn't directly on the front. This lets us
    place factories with stable neighborhoods.
    """
    w = World()
    pin_deterministic_allocation(w)
    for q in range(width):
        defender = Ownership.ENEMY if q >= enemy_from else Ownership.SUPER_EARTH
        is_capital = q == width - 1
        w.grid[(q, 0)] = Cell(coord=(q, 0), defender=defender, is_capital=is_capital)
    return w


# --------------------------------------------------------------------- #
# Placement
# --------------------------------------------------------------------- #


def test_spawn_factory_succeeds_on_enemy_cell():
    w = _wider_strip()
    poi = factory_mod.spawn_factory(w, (6, 0))
    assert poi is not None
    assert poi.kind == "factory"
    assert poi.owner == Ownership.ENEMY
    assert poi.state == {"active_targets": []}


def test_spawn_factory_refuses_on_se_cell():
    w = _wider_strip()
    poi = factory_mod.spawn_factory(w, (2, 0))
    assert poi is None


def test_spawn_factory_refuses_on_contested_cell():
    w = _wider_strip()
    w.grid[(6, 0)].attacker = Ownership.SUPER_EARTH
    poi = factory_mod.spawn_factory(w, (6, 0))
    assert poi is None


# --------------------------------------------------------------------- #
# tick_factories — target selection and pruning
# --------------------------------------------------------------------- #


def test_tick_factories_fills_open_slots_with_front_adjacent_se_cells():
    w = _wider_strip()
    poi = factory_mod.spawn_factory(w, (5, 0))
    assert poi is not None
    # Factory at q=5 (enemy front cell). Strip neighbors are q=4 (SE,
    # front-adjacent because q=5 is its enemy neighbor) and q=6 (enemy).
    # Only q=4 qualifies as a target.
    factory_mod.tick_factories(w)
    targets = poi.state["active_targets"]
    assert targets == [[4, 0]]
    # The cell should have been opened to ENEMY contestation.
    assert w.grid[(4, 0)].attacker == Ownership.ENEMY
    assert w.grid[(4, 0)].progress == 0.0


def test_tick_factories_respects_active_cap():
    w = _wider_strip(width=10, enemy_from=6)
    w.params.factory_active_cap = 1
    poi = factory_mod.spawn_factory(w, (6, 0))
    assert poi is not None
    factory_mod.tick_factories(w)
    assert len(poi.state["active_targets"]) == 1


def test_tick_factories_prunes_target_that_lost_front_connection():
    """A target whose only enemy neighbor gets captured by SE is no
    longer front-adjacent. Factory must drop it — feeding a stranded
    incursion behind the new front is the salient's job, not the
    factory's."""
    w = _wider_strip(width=10, enemy_from=8)
    # Enemy at q=8,9 (q=9 capital). SE q=0..7. Place factory at q=8.
    poi = factory_mod.spawn_factory(w, (8, 0))
    assert poi is not None
    factory_mod.tick_factories(w)
    # Front-adjacent SE cell within radius=3: only q=7 (q=6 has no enemy
    # neighbor since q=5 is SE).
    assert poi.state["active_targets"] == [[7, 0]]

    # Now flip q=8 to SE (factory's own cell, but pretend SE captured the
    # enemy line behind it). q=7 still has q=8 as a neighbor — but q=8 is
    # SE now, so q=7 has no enemy neighbor at all (q=6 SE, q=8 SE).
    w.grid[(8, 0)].defender = Ownership.SUPER_EARTH
    w.grid[(8, 0)].attacker = None
    w.tick = w.params.factory_target_period_ticks
    factory_mod.tick_factories(w)
    # q=7 should be pruned — no enemy neighbor remains. The factory may
    # pick something else or leave the slot empty.
    assert [7, 0] not in poi.state["active_targets"]


def test_tick_factories_prunes_targets_that_flipped_to_enemy():
    w = _wider_strip()
    poi = factory_mod.spawn_factory(w, (5, 0))
    assert poi is not None
    factory_mod.tick_factories(w)
    assert poi.state["active_targets"] == [[4, 0]]
    # Simulate the cell flipping to ENEMY (capture). The factory should
    # drop the now-enemy cell from its active_targets list. A new
    # front-adjacent candidate ((3, 0), since (4, 0) just became enemy)
    # is now available — the factory may rotate to it.
    w.grid[(4, 0)].defender = Ownership.ENEMY
    w.grid[(4, 0)].attacker = None
    w.tick = w.params.factory_target_period_ticks
    factory_mod.tick_factories(w)
    targets = poi.state["active_targets"]
    assert [4, 0] not in targets
    # Either empty (fully pruned) or rotated to the new front cell.
    assert targets in ([], [[3, 0]])


def test_tick_factories_prefers_in_progress_target():
    """Force two candidates and verify the half-flipped one is picked first."""
    # Build a fatter map: 3-row strip so a factory at (5,0) sees both (4,0)
    # and (4,1) as candidates within radius.
    w = World()
    pin_deterministic_allocation(w)
    for q in range(8):
        for r in range(2):
            defender = Ownership.ENEMY if q >= 5 else Ownership.SUPER_EARTH
            is_capital = (q, r) == (7, 0)
            w.grid[(q, r)] = Cell(coord=(q, r), defender=defender, is_capital=is_capital)
    w.params.factory_active_cap = 1
    poi = factory_mod.spawn_factory(w, (5, 0))
    assert poi is not None

    # Pre-existing in-progress target — already contested with negative progress.
    w.grid[(4, 1)].attacker = Ownership.ENEMY
    w.grid[(4, 1)].progress = -30.0

    factory_mod.tick_factories(w)
    targets = poi.state["active_targets"]
    assert targets == [[4, 1]], f"expected in-progress (4,1) preferred, got {targets}"


def test_tick_factories_cadence_gate():
    """tick_factories must do nothing on off-cadence ticks."""
    w = _wider_strip()
    poi = factory_mod.spawn_factory(w, (5, 0))
    assert poi is not None
    w.tick = 1   # not divisible by factory_target_period_ticks (default 25)
    factory_mod.tick_factories(w)
    assert poi.state["active_targets"] == []


# --------------------------------------------------------------------- #
# apply_factory_pressure
# --------------------------------------------------------------------- #


def test_apply_factory_pressure_stamps_active_targets_only():
    w = _wider_strip()
    poi = factory_mod.spawn_factory(w, (5, 0))
    assert poi is not None
    factory_mod.tick_factories(w)
    factory_mod.apply_factory_pressure(w)

    magnitude = w.params.factory_pressure_magnitude
    assert w.grid[(4, 0)].factory_pressure == magnitude
    for coord, cell in w.grid.items():
        if coord != (4, 0):
            assert cell.factory_pressure == 0.0


def test_apply_factory_pressure_clears_when_factory_removed():
    w = _wider_strip()
    poi = factory_mod.spawn_factory(w, (5, 0))
    assert poi is not None
    factory_mod.tick_factories(w)
    factory_mod.apply_factory_pressure(w)
    assert w.grid[(4, 0)].factory_pressure > 0

    w.remove_poi(poi.id)
    factory_mod.apply_factory_pressure(w)
    for cell in w.grid.values():
        assert cell.factory_pressure == 0.0


# --------------------------------------------------------------------- #
# Integration — pressure drives progress
# --------------------------------------------------------------------- #


def test_factory_pressure_drives_progress_on_target():
    """A factory with an active target should push that cell's progress
    toward enemy capture over a few simulated seconds. Diver pool is
    forced to 0 so the SE allocator doesn't open counter-fronts and
    flip the factory's host cell out from under it during the test."""
    w = _wider_strip()
    w.params.diver_pool = 0.0   # quiet the SE side; isolate factory dynamics
    poi = factory_mod.spawn_factory(w, (5, 0))
    assert poi is not None
    w.match_state = "running"
    for _ in range(int(w.params.tick_hz * 5)):  # ~5 simulated seconds
        w.step()

    target = w.grid[(4, 0)]
    if target.attacker == Ownership.ENEMY:
        assert target.progress < 0.0, (
            f"expected enemy progress on factory target, got {target.progress}"
        )
    else:
        # Already flipped — that's also a valid outcome.
        assert target.defender == Ownership.ENEMY


# --------------------------------------------------------------------- #
# Controller — probabilistic spawn
# --------------------------------------------------------------------- #


def test_controller_spawns_factory_below_soft_cap():
    """Direct invocation of the spawn method — chance forced to 1.0 so
    the test is deterministic. (Stepping through the full world to hit
    factory_period_ticks would race the diver allocator, which can sweep
    a defender-less strip in well under 200 ticks.)"""
    w = _wider_strip(width=10, enemy_from=6)
    w.params.factory_spawn_chance_below_cap = 1.0
    random.seed(0)
    w.controller._maybe_spawn_factory(w)
    factories = [p for p in w.pois.values() if p.kind == "factory"]
    assert len(factories) == 1
    assert factories[0].owner == Ownership.ENEMY


def test_controller_respects_hard_cap():
    """Pre-place factories at the hard cap; the controller must not add more."""
    w = _wider_strip(width=12, enemy_from=4)
    # Place factories up to the hard cap on enemy cells.
    placed = 0
    for q in range(4, 12):
        if placed >= w.params.factory_hard_cap:
            break
        if (q, 0) not in {p.coord for p in w.pois.values()}:
            poi = factory_mod.spawn_factory(w, (q, 0))
            if poi is not None:
                placed += 1
    assert placed == w.params.factory_hard_cap

    # Force the controller's hand 100x — no new factories should appear.
    random.seed(0)
    for _ in range(100):
        w.controller._maybe_spawn_factory(w)
    factories = [p for p in w.pois.values() if p.kind == "factory"]
    assert len(factories) == w.params.factory_hard_cap
