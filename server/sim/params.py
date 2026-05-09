"""Tunable simulation parameters.

These are exposed via the controller panel — sliders bind to fields here.
Defaults targeted at a ~5 minute match when controllers actively
concentrate diver pressure.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class SimParams:
    tick_hz: float = 5.0                # sim ticks per second
    base_rate: float = 0.5              # baseline progress per second on a contested cell
    pressure_coefficient: float = 0.05  # progress per (pressure-unit * second)
    enemy_resistance_base: float = 6.0  # defender resistance magnitude per contested cell; supply-modulated at apply
    enemy_ai_period_ticks: int = 5      # enemy AI runs every N ticks
    enemy_spawn_period_ticks: int = 50  # resistance-node spawn cadence

    fob_buff: float = 5.0               # +rate to contested cells in FOB radius (radius 2)
    fob_radius: int = 2

    arty_buff: float = 12.0             # +rate during artillery effect
    arty_duration_s: float = 8.0        # seconds of effect per shell
    arty_default_shells: int = 5
    arty_range: int = 3                 # max hex distance from artillery to firing target

    fortress_resist: float = 6.0        # added to enemy contribution within radius 3
    fortress_radius: int = 3
    fortress_siege_multiplier: float = 2.0  # cells under fortress need this x progress to flip

    node_resist: float = 2.0            # node + neighbors
    node_radius: int = 1

    flip_threshold: float = 100.0       # progress magnitude required to flip a cell
    # Repulse: a contested cell's incursion is driven off when progress moves
    # against the attacker by this magnitude. Smaller than flip_threshold —
    # losing a foothold is faster than capturing a cell.
    repulse_threshold_ratio: float = 0.5
    # Active-front latch. When progress is in the attacker's favor by more
    # than the epsilon, the cell is stamped "active" for this many seconds so
    # the visual signal doesn't strobe on momentary dips. Sim default is a
    # few seconds for fast iteration; the live game would set this in minutes.
    active_progress_epsilon: float = 5.0
    active_latch_s: float = 4.0

    # Supply system. Defender uses BFS over same-faction cells from rooted
    # sources (enemy capital + fortresses). Attacker uses local same-faction
    # density plus FOB proximity. Supply only modulates contested-cell rates.
    supply_floor: float = 0.3                # min effective supply factor (0..1)
    supply_max_depth: int = 5                # BFS depth at which defender supply hits 0
    attacker_density_radius: int = 2         # SE same-faction neighbor count radius
    fob_supply_bonus: float = 0.4            # added to attacker supply within FOB radius

    # Breakthrough: a flip cascades when the defender's supply was already
    # weak or another flip just happened nearby. Effect is a temporary
    # supply shock on defender-owned neighbors of the flipped cell.
    breakthrough_supply_threshold: float = 0.4
    breakthrough_window_s: float = 3.0
    breakthrough_duration_s: float = 5.0

    # Salients: directed enemy operations spawned by a controller.
    # Destroy salients drive a corridor of cells from the front to a
    # high-value SE POI, projecting supply along the corridor regardless
    # of normal BFS. The controller decides when to spawn; the salient
    # primitive owns the mechanics.
    salient_period_ticks: int = 150              # strategic cadence (~30s at 5Hz)
    max_active_destroy_salients: int = 1
    destroy_salient_lifetime_s: float = 90.0
    destroy_max_range: int = 8                   # hops from enemy front to target POI
    destroy_corridor_supply_floor: float = 0.7
    destroy_min_score_threshold: float = 0.2     # opportunistic spawn gate
    salient_pressure_magnitude: float = 200.0    # offensive force stamped on corridor cells; consumed in _apply_pressure as salient_pressure * pressure_coefficient * en_factor

    # SE diver allocation. The diver pool is a constant abstraction of
    # playerbase size; each allocation pass distributes it over contested
    # SE-attacker cells via softmax(utility / temperature). User-pinned
    # cells (set via the controller pressure slider) consume from the pool
    # first; their value is preserved until the cell flips or the user
    # releases by setting pressure back to 0.
    diver_pool: float = 1500.0                   # total SE force distributed each allocation pass
    allocation_period_ticks: int = 5             # ~1s at 5Hz
    allocation_temperature: float = 1.0          # low=concentrate, high=spread
    diver_supply_max_hops: int = 2               # contested cells beyond this hex-distance from any SE-held cell are cut off

    # SE high command (strategic infrastructure planner). Sibling to the
    # diver allocator above: the allocator is the player-proxy executor,
    # high command is the planner that places/moves FOBs and artillery.
    # Costs draw from a shared ``requisition`` pool that accrues every
    # tick; per-type cost curves and action wiring land in later phases.
    high_command_enabled: bool = False           # off by default until later phases land
    high_command_period_ticks: int = 100         # ~20s at 5Hz — strategic cadence
    requisition_per_tick: float = 0.5            # smooth accrual rate of the build pool

    # FOB siting (Phase 2). Cost scales as base * (n+1)^exponent over the
    # current SE FOB count; with base=50, exponent=2, requisition_per_tick=0.5
    # the first FOB lands ~tick 100, second ~tick 500, third ~tick 1400 —
    # softly capping the count and rewarding coverage gains over count.
    fob_base_cost: float = 50.0
    fob_cost_exponent: float = 2.0
    fob_min_coverage_threshold: int = 1          # min uncovered contested cells in reach to bother placing

    # Artillery siting (Phase 3). Same shape as FOB siting but parameterized
    # over arty_range and a steeper cost base — artillery is heavier infra
    # and the soft cap should land at fewer total emplacements than FOBs.
    arty_base_cost: float = 100.0
    arty_cost_exponent: float = 2.0
    arty_min_coverage_threshold: int = 1

    # Build site duration (Phase 4a). Fresh FOB / artillery placements
    # route through a build_site POI that resolves to the real structure
    # after this many ticks. During the wait the site provides no buff
    # and is destroyable by an enemy flip.
    fresh_build_ticks: int = 75              # ~15s at 5Hz

    # Decommission + move (Phase 4b). The planner relocates structures by
    # tearing down the source and placing a build site at the destination
    # with a shorter ``move_build_ticks`` window — moves are *faster* than
    # fresh builds because the materiel was already constructed. Stale
    # POIs (zero individual coverage for N consecutive strategic ticks)
    # decommission for free, returning the slot to the cost curve.
    decommission_stale_ticks: int = 3        # ~60s at default cadence
    move_build_ticks: int = 25               # ~5s at 5Hz
    fob_move_cost: float = 25.0              # half of fob_base_cost
    arty_move_cost: float = 50.0             # half of arty_base_cost

    def to_dict(self) -> dict:
        return asdict(self)

    def update_from(self, partial: dict) -> None:
        for k, v in partial.items():
            if hasattr(self, k):
                setattr(self, k, type(getattr(self, k))(v))
