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

    fortress_resist: float = 6.0        # added to enemy contribution within radius 3
    fortress_radius: int = 3
    fortress_siege_multiplier: float = 2.0  # cells under fortress need this x progress to flip

    node_resist: float = 2.0            # node + neighbors
    node_radius: int = 1

    flip_threshold: float = 100.0       # progress magnitude required to flip a cell

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

    def to_dict(self) -> dict:
        return asdict(self)

    def update_from(self, partial: dict) -> None:
        for k, v in partial.items():
            if hasattr(self, k):
                setattr(self, k, type(getattr(self, k))(v))
