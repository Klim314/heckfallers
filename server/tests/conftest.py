"""Test helpers shared across the salient/controller suite."""
from __future__ import annotations

from server.sim.cell import Cell, Ownership
from server.sim.world import World


def make_world(width: int = 12) -> World:
    """A 1-row strip of cells from q=0..width-1, all SE except the rightmost
    which is the enemy capital. Lets tests place a POI mid-strip and observe
    a clean corridor running across.
    """
    w = World()
    for q in range(width):
        defender = Ownership.ENEMY if q == width - 1 else Ownership.SUPER_EARTH
        w.grid[(q, 0)] = Cell(coord=(q, 0), defender=defender, is_capital=(q == width - 1))
    return w
