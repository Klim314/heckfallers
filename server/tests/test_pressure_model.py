"""Asymmetric "uncoordinated mob" pressure model.

Divers are an uncoordinated player mob. Past `resistance + base_headroom`
their per-cell contribution sublinearly diminishes — they get in each
other's way. Critically, the threshold is *resistance-aware*: stacking
to match a real threat is rewarded (linear), only over-committing past
that point is penalized.

Enemy contributions (salient/factory/resistance/enemy POIs) stay linear:
they represent a coordinated AI assault.

These tests cover:
  - The effective_diver_pressure transform in isolation: threshold,
    continuity, resistance-awareness, and the alpha (power-law) shape.
  - Integration through _apply_pressure: stacking against resistance is
    NOT penalized; over-stacking IS penalized; enemy pressure is never
    transformed.
"""
from __future__ import annotations

import math

from server.sim.cell import Cell, Ownership
from server.sim.params import effective_diver_pressure
from server.sim.world import World


# --------------------------------------------------------------------------- #
# Unit: effective_diver_pressure
# --------------------------------------------------------------------------- #

HEADROOM = 5.0
FACTOR = 0.3


def test_zero_pressure_zero_resistance():
    assert effective_diver_pressure(0.0, 0.0, HEADROOM, FACTOR) == 0.0


def test_below_threshold_no_resistance_is_linear():
    # threshold = 5 + 0 = 5 rate-units
    assert effective_diver_pressure(3.0, 0.0, HEADROOM, FACTOR) == 3.0


def test_at_threshold_no_resistance_is_linear():
    assert effective_diver_pressure(5.0, 0.0, HEADROOM, FACTOR) == 5.0


def test_above_threshold_no_resistance_is_sublinear():
    # threshold = 5; raw 15 → 5 + 0.3 * 10 = 8.0
    assert effective_diver_pressure(15.0, 0.0, HEADROOM, FACTOR) == 8.0


def test_resistance_lifts_threshold_so_stacking_against_threat_is_linear():
    # resistance = 10 raises threshold to 15. raw 15 still linear.
    assert effective_diver_pressure(15.0, 10.0, HEADROOM, FACTOR) == 15.0
    # raw 20 with same resistance: 15 + 0.3 * 5 = 16.5
    assert effective_diver_pressure(20.0, 10.0, HEADROOM, FACTOR) == 16.5


def test_continuous_at_threshold():
    # Approaching the threshold from both sides should converge.
    threshold = 5.0
    just_below = effective_diver_pressure(threshold - 1e-6, 0.0, HEADROOM, FACTOR)
    just_above = effective_diver_pressure(threshold + 1e-6, 0.0, HEADROOM, FACTOR)
    assert math.isclose(just_below, just_above, abs_tol=1e-5)


def test_alpha_one_matches_piecewise_linear():
    # Default alpha=1.0 should match the explicit-linear branch exactly.
    raw, res = 25.0, 5.0  # threshold = 10
    linear = effective_diver_pressure(raw, res, HEADROOM, FACTOR)
    explicit = effective_diver_pressure(raw, res, HEADROOM, FACTOR, excess_alpha=1.0)
    assert linear == explicit


def test_alpha_below_one_imposes_heavier_penalty_far_above_threshold():
    # threshold = 10. raw = 30 → excess = 20 = 2 * threshold.
    # Linear: 10 + 0.3 * 20 = 16.
    # Power-law alpha=0.7: 10 + 0.3 * 10 * (2.0 ** 0.7) ≈ 10 + 3 * 1.6245 ≈ 14.87
    raw, res = 30.0, 5.0
    linear = effective_diver_pressure(raw, res, HEADROOM, FACTOR, excess_alpha=1.0)
    powerlaw = effective_diver_pressure(raw, res, HEADROOM, FACTOR, excess_alpha=0.7)
    assert linear > powerlaw  # power-law penalizes more
    assert math.isclose(powerlaw, 10.0 + 0.3 * 10.0 * (2.0 ** 0.7), abs_tol=1e-9)


def test_alpha_branch_continuous_with_linear_at_threshold():
    # At excess=threshold (raw = 2*threshold), the power-law branch matches
    # the linear branch exactly regardless of alpha. This is the design
    # anchor for the curve.
    raw, res = 10.0, 5.0  # threshold = 10, excess = 0 — boundary
    # Just above: raw = threshold + threshold = 2 * threshold = 20
    # Wait — for "excess == threshold", raw = 2 * threshold = 20.
    raw_at_anchor = 20.0
    linear = effective_diver_pressure(raw_at_anchor, res, HEADROOM, FACTOR, excess_alpha=1.0)
    powerlaw = effective_diver_pressure(raw_at_anchor, res, HEADROOM, FACTOR, excess_alpha=0.5)
    assert math.isclose(linear, powerlaw, abs_tol=1e-9)


# --------------------------------------------------------------------------- #
# Integration: _apply_pressure uses resistance-aware transform on divers,
# never on enemy contributions
# --------------------------------------------------------------------------- #

def _single_cell_world(diver_pressure: float, salient_pressure: float) -> tuple[World, Cell]:
    """Minimal world: one enemy-attacker contested SE cell with pinned
    supply. Lets us read the per-tick rate as the observed progress delta
    without AI/POI noise."""
    w = World()
    # Disable resistance so the only enemy contribution is salient_pressure.
    w.params.enemy_resistance_base = 0.0
    coord = (0, 0)
    cell = Cell(
        coord=coord,
        defender=Ownership.SUPER_EARTH,
        attacker=Ownership.ENEMY,
        progress=0.0,
        diver_pressure=diver_pressure,
        salient_pressure=salient_pressure,
        # Pin supply at 1.0 on both sides so se_factor == en_factor == 1.0.
        se_supply=1.0,
        enemy_supply=1.0,
    )
    w.grid[coord] = cell
    return w, cell


def test_stacking_against_high_resistance_is_not_penalized():
    """diver=600, salient=400 (matched). Both contribute fully; no mob
    penalty because divers are matching the threat, not over-committing."""
    w, cell = _single_cell_world(diver_pressure=600.0, salient_pressure=400.0)
    p = w.params
    dt = 1.0 / p.tick_hz

    # Resistance in rate units: 400 * 0.05 * 1.0 = 20.
    # Threshold = 5 (headroom) + 20 = 25.
    # raw_diver_rate = 600 * 0.05 * 1.0 = 30. Excess over threshold = 5.
    # Effective = 25 + 0.3 * 5 = 26.5 (small penalty for the 5 over-stack).
    # Linear comparison would be 30. So effective is 26.5 vs 30 linear.
    raw_diver_rate = 600.0 * p.pressure_coefficient * 1.0
    resistance_rate = 400.0 * p.pressure_coefficient * 1.0
    threshold = p.diver_base_headroom + resistance_rate
    expected_eff = threshold + p.diver_excess_factor * (raw_diver_rate - threshold)
    expected_rate = p.base_rate + expected_eff - resistance_rate

    w._apply_pressure(dt)
    observed_rate = cell.progress / dt
    assert math.isclose(observed_rate, expected_rate, abs_tol=1e-9)


def test_overstacking_low_resistance_is_penalized():
    """diver=600, salient=80 (over-committed). Most divers exceed the
    resistance + headroom and face the mob penalty."""
    w, cell = _single_cell_world(diver_pressure=600.0, salient_pressure=80.0)
    p = w.params
    dt = 1.0 / p.tick_hz

    raw_diver_rate = 600.0 * p.pressure_coefficient * 1.0  # 30
    resistance_rate = 80.0 * p.pressure_coefficient * 1.0  # 4
    threshold = p.diver_base_headroom + resistance_rate    # 9
    excess = raw_diver_rate - threshold                    # 21
    expected_eff = threshold + p.diver_excess_factor * excess  # 9 + 6.3 = 15.3
    expected_rate = p.base_rate + expected_eff - resistance_rate

    # Linear baseline: would have used 30 directly → rate dominated by divers.
    expected_rate_linear = p.base_rate + raw_diver_rate - resistance_rate

    w._apply_pressure(dt)
    observed_rate = cell.progress / dt
    assert math.isclose(observed_rate, expected_rate, abs_tol=1e-9)
    # Sanity: real penalty applied — observed is meaningfully below linear.
    assert observed_rate < expected_rate_linear - 1.0


def test_enemy_salient_pressure_stays_linear():
    """salient=300, divers=0: no transform on enemy pressure regardless
    of magnitude. The rate must reflect the full 300."""
    w, cell = _single_cell_world(diver_pressure=0.0, salient_pressure=300.0)
    p = w.params
    dt = 1.0 / p.tick_hz

    resistance_rate = 300.0 * p.pressure_coefficient * 1.0
    expected_rate = p.base_rate - resistance_rate

    w._apply_pressure(dt)
    observed_rate = cell.progress / dt
    assert math.isclose(observed_rate, expected_rate, abs_tol=1e-9)


def test_no_resistance_low_divers_is_linear():
    """diver=50 with no enemy contributions: well below the always-linear
    headroom (5 rate-units = 100 raw at default coefficient). So divers act
    1:1, no mob penalty for tiny pickets."""
    w, cell = _single_cell_world(diver_pressure=50.0, salient_pressure=0.0)
    p = w.params
    dt = 1.0 / p.tick_hz

    raw_diver_rate = 50.0 * p.pressure_coefficient * 1.0  # 2.5
    # threshold = 5 + 0 = 5. raw 2.5 < 5 → linear (effective = 2.5).
    expected_rate = p.base_rate + raw_diver_rate

    w._apply_pressure(dt)
    observed_rate = cell.progress / dt
    assert math.isclose(observed_rate, expected_rate, abs_tol=1e-9)
