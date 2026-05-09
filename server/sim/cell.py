"""Cell ownership and state."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .grid import Coord


class Ownership(str, Enum):
    SUPER_EARTH = "se"
    ENEMY = "enemy"
    CONTESTED = "contested"


@dataclass
class Cell:
    coord: Coord
    ownership: Ownership
    progress: float = 0.0           # -100..+100; +100 -> SE flip, -100 -> Enemy flip
    diver_pressure: float = 0.0     # set by controllers / players
    enemy_resistance: float = 0.0   # set by enemy AI each tick
    is_capital: bool = False

    def to_wire(self) -> dict:
        return {
            "q": self.coord[0],
            "r": self.coord[1],
            "ownership": self.ownership.value,
            "progress": round(self.progress, 2),
            "diver_pressure": round(self.diver_pressure, 2),
            "enemy_resistance": round(self.enemy_resistance, 2),
            "is_capital": self.is_capital,
        }
