"""Cell defender/attacker state.

Each cell is *defended* by exactly one faction (SE or Enemy). It may
also be *attacked* by the opposing faction during an active incursion.
A cell with ``attacker is None`` is held; otherwise it is contested.
``progress`` is only meaningful while contested, and is signed:
positive values mean SE is gaining, negative means Enemy is gaining,
matching the diver_pressure / enemy_resistance sign convention. Progress
swings freely across zero — the attacker captures at +/-flip_threshold
(in their favor) and the incursion is repulsed at the opposite repulse
threshold, clearing ``attacker`` back to None.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .grid import Coord


class Ownership(str, Enum):
    SUPER_EARTH = "se"
    ENEMY = "enemy"


@dataclass
class Cell:
    coord: Coord
    defender: Ownership
    attacker: Ownership | None = None
    progress: float = 0.0           # signed; +→SE direction, -→Enemy direction
    diver_pressure: float = 0.0     # set by SE diver AI; overridden by user pin
    diver_pin: bool = False         # user has manually pinned this cell's pressure; AI skips it until flip or release
    enemy_resistance: float = 0.0   # set by enemy AI each tick
    salient_pressure: float = 0.0   # offensive force projected by an active enemy salient on its corridor cells
    is_capital: bool = False
    enemy_supply: float = 1.0       # BFS from enemy capital + fortress sources
    se_supply: float = 1.0          # local SE-density + FOB bonus
    supply_shock_until: int = -1    # tick number; while world.tick < this, enemy_supply reads as 0
    active_until_tick: int = -1     # tick number; while world.tick < this, the cell renders as an active front

    @property
    def is_contested(self) -> bool:
        return self.attacker is not None

    def to_wire(self) -> dict:
        return {
            "q": self.coord[0],
            "r": self.coord[1],
            "defender": self.defender.value,
            "attacker": self.attacker.value if self.attacker is not None else None,
            "progress": round(self.progress, 2),
            "diver_pressure": round(self.diver_pressure, 2),
            "diver_pin": self.diver_pin,
            "enemy_resistance": round(self.enemy_resistance, 2),
            "salient_pressure": round(self.salient_pressure, 2),
            "is_capital": self.is_capital,
            "enemy_supply": round(self.enemy_supply, 2),
            "se_supply": round(self.se_supply, 2),
            "supply_shock_until": self.supply_shock_until,
            "active_until_tick": self.active_until_tick,
        }
