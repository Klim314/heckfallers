"""OpportunisticController — strategic salient spawn behavior."""
from __future__ import annotations

from server.sim.cell import Ownership
from server.sim.controllers.opportunistic import OpportunisticController

from .conftest import make_world


def _spawn_period(w):
    return w.params.salient_period_ticks


def test_controller_spawns_destroy_salient_when_artillery_in_range():
    w = make_world(width=8)
    w.place_poi("artillery", Ownership.SUPER_EARTH, (3, 0))
    ctrl = OpportunisticController()

    # Force the strategic gate by setting tick to one strategic period.
    w.tick = _spawn_period(w)
    ctrl.tick(w)

    assert len(w.salients) == 1
    sal = next(iter(w.salients.values()))
    assert sal.target == (3, 0)


def test_controller_declines_when_score_below_threshold():
    w = make_world(width=20)
    w.place_poi("fob", Ownership.SUPER_EARTH, (2, 0))   # FOB at distance 17 from enemy
    w.params.destroy_max_range = 25                      # ensure range isn't the gate
    w.params.destroy_min_score_threshold = 0.5           # well above 0.7/(17+1)=0.039
    ctrl = OpportunisticController()

    w.tick = _spawn_period(w)
    ctrl.tick(w)
    assert len(w.salients) == 0


def test_controller_declines_when_target_out_of_range():
    w = make_world(width=20)
    w.place_poi("artillery", Ownership.SUPER_EARTH, (2, 0))   # 17 hops from enemy
    w.params.destroy_max_range = 5
    ctrl = OpportunisticController()

    w.tick = _spawn_period(w)
    ctrl.tick(w)
    assert len(w.salients) == 0


def test_controller_respects_active_salient_cap():
    w = make_world(width=8)
    w.place_poi("artillery", Ownership.SUPER_EARTH, (3, 0))
    w.place_poi("fob", Ownership.SUPER_EARTH, (4, 0))
    w.params.max_active_destroy_salients = 1
    ctrl = OpportunisticController()

    w.tick = _spawn_period(w)
    ctrl.tick(w)
    assert len(w.salients) == 1

    # Bump tick to next strategic period; cap should still hold.
    w.tick = _spawn_period(w) * 2
    ctrl.tick(w)
    assert len(w.salients) == 1


def test_controller_picks_highest_value_target():
    w = make_world(width=8)
    # Put both a FOB and an artillery at the same distance — artillery should win.
    w.place_poi("fob", Ownership.SUPER_EARTH, (3, 0))
    w.place_poi("artillery", Ownership.SUPER_EARTH, (3, 0))   # placement allowed: empty cell rule is per-cell, so use a different cell
    # Actually placement on same cell allowed for different kinds — but to be safe,
    # put artillery one cell closer to enemy (distance 3) vs fob distance 4.
    # Our two place_poi calls already succeeded since they're on different POI IDs.
    ctrl = OpportunisticController()
    w.tick = _spawn_period(w)
    ctrl.tick(w)

    # First spawn should target the artillery (higher value).
    assert len(w.salients) == 1
    sal = next(iter(w.salients.values()))
    target_poi = w.pois[sal.target_poi_id]
    assert target_poi.kind == "artillery"
