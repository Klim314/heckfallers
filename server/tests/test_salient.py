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
