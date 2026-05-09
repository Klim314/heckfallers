"""SE diver allocator — defensive reinforcement against active salients.

Offensive allocation (SE-attacker contests) is exercised indirectly via
the broader controller / world tests. These tests focus on the defensive
path: divers must divert to enemy-attacker SE cells, especially those on
an active salient corridor.
"""
from __future__ import annotations

from server.sim import salient as salient_mod
from server.sim.cell import Ownership
from server.sim.se_ai import allocate_divers

from .conftest import make_world


def test_defensive_contest_receives_divers():
    """An enemy-attacker SE cell (salient lead) must get a share of the
    pool — the old allocator filtered to SE-attacker cells only."""
    w = make_world(width=8)
    target = w.place_poi("artillery", Ownership.SUPER_EARTH, (3, 0))
    sal = salient_mod.spawn_destroy_salient(w, target.id)
    assert sal is not None
    salient_mod.apply_salient_pressure(w)

    allocate_divers(w)

    lead = w.grid[sal.corridor[1]]
    assert lead.attacker == Ownership.ENEMY
    assert lead.diver_pressure > 0.0


def test_defensive_corridor_outranks_offensive_baseline():
    """With both an offensive contest and a defensive corridor lead alive,
    the defensive cell wins the bulk of the pool — divers divert."""
    w = make_world(width=8)

    # Offensive: SE attacking the rightmost enemy cell.
    enemy_cell = w.grid[(7, 0)]
    enemy_cell.attacker = Ownership.SUPER_EARTH
    enemy_cell.progress = 0.0

    # Defensive: salient opens (6, 0) as enemy-attacker.
    target = w.place_poi("artillery", Ownership.SUPER_EARTH, (3, 0))
    sal = salient_mod.spawn_destroy_salient(w, target.id)
    assert sal is not None
    salient_mod.apply_salient_pressure(w)

    allocate_divers(w)

    offensive = w.grid[(7, 0)]
    defensive = w.grid[sal.corridor[1]]
    assert offensive.diver_pressure > 0.0
    assert defensive.diver_pressure > offensive.diver_pressure


def test_corridor_position_weights_toward_target():
    """Two defensive contests on the same corridor: the one closer to the
    targeted SE POI (last line of defense) outranks the one closer to the
    enemy origin."""
    w = make_world(width=8)
    target = w.place_poi("artillery", Ownership.SUPER_EARTH, (3, 0))
    sal = salient_mod.spawn_destroy_salient(w, target.id)
    assert sal is not None

    # Open a second defensive contest deeper in the corridor (closer to target).
    near_target_cell = w.grid[(4, 0)]
    near_target_cell.attacker = Ownership.ENEMY
    near_target_cell.progress = 0.0

    salient_mod.apply_salient_pressure(w)

    allocate_divers(w)

    far_from_target = w.grid[sal.corridor[1]]   # (6, 0), idx=1
    near_target = w.grid[(4, 0)]                # idx=3
    assert near_target.diver_pressure > far_from_target.diver_pressure


def test_pinned_offensive_preserved_when_defensive_active():
    """User-pinned offensive pressure stays on the cell even when a salient
    opens — the pin contract is independent of the defensive bias."""
    w = make_world(width=8)

    # Offensive contest that the user has pinned to a fixed value.
    enemy_cell = w.grid[(7, 0)]
    enemy_cell.attacker = Ownership.SUPER_EARTH
    enemy_cell.progress = 0.0
    enemy_cell.diver_pressure = 300.0
    enemy_cell.diver_pin = True

    target = w.place_poi("artillery", Ownership.SUPER_EARTH, (3, 0))
    sal = salient_mod.spawn_destroy_salient(w, target.id)
    assert sal is not None
    salient_mod.apply_salient_pressure(w)

    allocate_divers(w)

    assert enemy_cell.diver_pressure == 300.0
    assert enemy_cell.diver_pin is True
    # Free pool minus the pin still feeds the defensive lead.
    defensive = w.grid[sal.corridor[1]]
    assert defensive.diver_pressure > 0.0
    assert defensive.diver_pressure <= w.params.diver_pool - 300.0
