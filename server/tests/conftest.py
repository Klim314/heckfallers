"""Test helpers shared across the salient/controller suite."""
from __future__ import annotations

from server.sim.cell import Cell, Ownership
from server.sim.world import World


def pin_deterministic_allocation(w: World) -> None:
    """Disable allocator stochasticity for reproducible test assertions."""
    w.params.allocation_pool_jitter_sigma = 0.0
    w.params.allocation_temperature_jitter = 0.0
    w.params.allocation_chunk_count = 0


def make_world(width: int = 12) -> World:
    """A 1-row strip of cells from q=0..width-1, all SE except the rightmost
    which is the enemy capital. Lets tests place a POI mid-strip and observe
    a clean corridor running across.
    """
    w = World()
    pin_deterministic_allocation(w)
    for q in range(width):
        defender = Ownership.ENEMY if q == width - 1 else Ownership.SUPER_EARTH
        w.grid[(q, 0)] = Cell(coord=(q, 0), defender=defender, is_capital=(q == width - 1))
    return w
