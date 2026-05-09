"""Salient primitive — corridor BFS, spawn validation, lifetime."""
from __future__ import annotations

from server.sim import salient as salient_mod
from server.sim.cell import Ownership
from server.sim.world import World

from .conftest import make_world


def test_corridor_bfs_finds_shortest_path():
    w = make_world(width=8)
    # Target at q=2 (deep in SE territory). Nearest enemy cell is q=7.
    path = salient_mod.build_destroy_corridor(w, (2, 0))
    assert path is not None
    assert path[0] == (7, 0)        # closest enemy front
    assert path[-1] == (2, 0)       # target
    assert len(path) == 6           # 7,6,5,4,3,2


def test_corridor_returns_none_when_out_of_range():
    w = make_world(width=20)
    w.params.destroy_max_range = 5
    # Target far from any enemy cell.
    path = salient_mod.build_destroy_corridor(w, (2, 0))
    assert path is None


def test_corridor_returns_none_when_no_enemy_cells():
    w = make_world(width=5)
    # Strip out enemy cells.
    for cell in w.grid.values():
        cell.defender = Ownership.SUPER_EARTH
        cell.is_capital = False
    path = salient_mod.build_destroy_corridor(w, (2, 0))
    assert path is None


def test_spawn_destroy_salient_succeeds_and_opens_lead():
    w = make_world(width=8)
    poi = w.place_poi("artillery", Ownership.SUPER_EARTH, (3, 0))
    assert poi is not None

    sal = salient_mod.spawn_destroy_salient(w, poi.id)
    assert sal is not None
    assert sal.kind == "destroy"
    assert sal.target == (3, 0)
    assert sal.target_poi_id == poi.id

    # First SE cell along the corridor (closest to enemy front) is opened.
    lead_coord = sal.corridor[1]   # corridor[0] is the enemy origin
    lead = w.grid[lead_coord]
    assert lead.attacker == Ownership.ENEMY
    assert lead.progress == 0.0


def test_spawn_refuses_when_poi_missing():
    w = make_world()
    assert salient_mod.spawn_destroy_salient(w, "missing_poi") is None


def test_spawn_refuses_when_target_already_targeted():
    w = make_world(width=8)
    poi = w.place_poi("artillery", Ownership.SUPER_EARTH, (3, 0))
    first = salient_mod.spawn_destroy_salient(w, poi.id)
    assert first is not None
    second = salient_mod.spawn_destroy_salient(w, poi.id)
    assert second is None


def test_spawn_refuses_when_target_out_of_range():
    w = make_world(width=20)
    w.params.destroy_max_range = 3
    poi = w.place_poi("artillery", Ownership.SUPER_EARTH, (2, 0))
    assert salient_mod.spawn_destroy_salient(w, poi.id) is None


def test_spawn_refuses_for_enemy_owned_poi():
    w = make_world(width=8)
    # Fortress is enemy-owned and only spawnable on enemy cells; place on capital.
    poi = w.place_poi("fortress", Ownership.ENEMY, (7, 0))
    assert poi is not None
    assert salient_mod.spawn_destroy_salient(w, poi.id) is None


def test_update_salients_expires_when_target_poi_destroyed():
    w = make_world(width=8)
    poi = w.place_poi("artillery", Ownership.SUPER_EARTH, (3, 0))
    sal = salient_mod.spawn_destroy_salient(w, poi.id)
    assert sal is not None

    # Simulate target POI removal (e.g., a flip would do this).
    w.remove_poi(poi.id)
    salient_mod.update_salients(w)

    assert sal.id not in w.salients
    # Success event surfaced.
    assert any(e["type"] == "destroy_salient_success" for e in w.match_events)


def test_update_salients_expires_on_lifetime():
    w = make_world(width=8)
    poi = w.place_poi("artillery", Ownership.SUPER_EARTH, (3, 0))
    sal = salient_mod.spawn_destroy_salient(w, poi.id)
    assert sal is not None

    w.tick = sal.expires_tick
    salient_mod.update_salients(w)
    assert sal.id not in w.salients
    # Expiry is silent — no success event.
    assert not any(e["type"] == "destroy_salient_success" for e in w.match_events)


def test_apply_salient_supply_boosts_corridor():
    w = make_world(width=8)
    poi = w.place_poi("artillery", Ownership.SUPER_EARTH, (3, 0))
    sal = salient_mod.spawn_destroy_salient(w, poi.id)
    assert sal is not None

    # Pretend BFS supply put corridor cells at 0.
    for coord in sal.corridor:
        w.grid[coord].enemy_supply = 0.0

    salient_mod.apply_salient_supply(w)

    floor = w.params.destroy_corridor_supply_floor
    for coord in sal.corridor:
        assert w.grid[coord].enemy_supply >= floor


def test_apply_salient_pressure_stamps_corridor_only():
    w = make_world(width=8)
    poi = w.place_poi("artillery", Ownership.SUPER_EARTH, (3, 0))
    sal = salient_mod.spawn_destroy_salient(w, poi.id)
    assert sal is not None

    salient_mod.apply_salient_pressure(w)

    magnitude = w.params.salient_pressure_magnitude
    corridor_set = set(sal.corridor)
    for coord, cell in w.grid.items():
        if coord in corridor_set:
            assert cell.salient_pressure == magnitude
        else:
            assert cell.salient_pressure == 0.0


def test_apply_salient_pressure_clears_when_salient_ends():
    w = make_world(width=8)
    poi = w.place_poi("artillery", Ownership.SUPER_EARTH, (3, 0))
    sal = salient_mod.spawn_destroy_salient(w, poi.id)
    assert sal is not None

    salient_mod.apply_salient_pressure(w)
    # Drop the salient (simulate expiry/success).
    w.salients.clear()
    salient_mod.apply_salient_pressure(w)

    for cell in w.grid.values():
        assert cell.salient_pressure == 0.0


def test_salient_pressure_drives_progress_against_fob():
    """A FOB within radius of the contested corridor cell used to stall the
    salient (rate became net-positive). With salient_pressure stamped on the
    corridor, the enemy attacker should drive progress negative."""
    from server.sim.cell import Ownership

    w = make_world(width=8)
    # Isolate the salient/FOB physics: with the diver allocator now
    # diverting forces to defensive contests on a salient corridor, a
    # non-zero pool would actively repulse the lead and mask the rate
    # interaction this test exists to cover. SE diver reinforcement is
    # exercised in test_se_ai.
    w.params.diver_pool = 0.0
    target = w.place_poi("artillery", Ownership.SUPER_EARTH, (3, 0))
    # FOB adjacent to the lead cell — within fob_radius of any corridor cell
    # that is currently contested.
    w.place_poi("fob", Ownership.SUPER_EARTH, (4, 0))

    sal = salient_mod.spawn_destroy_salient(w, target.id)
    assert sal is not None

    # Step the world for a few seconds and check the contested corridor
    # cell's progress is now negative (enemy gaining), not clamped at 0.
    w.match_state = "running"
    for _ in range(int(w.params.tick_hz * 3)):  # ~3 simulated seconds
        w.step()

    # The lead cell that was opened should have moved off zero in the enemy
    # direction. (corridor[1] is the first SE cell, set to enemy-attacker
    # by spawn_destroy_salient.)
    lead = w.grid[sal.corridor[1]]
    if lead.attacker == Ownership.ENEMY:
        assert lead.progress < 0.0, (
            f"expected enemy progress, got {lead.progress}"
        )
    else:
        # Already flipped — the salient won, that also satisfies the test.
        assert lead.defender == Ownership.ENEMY
