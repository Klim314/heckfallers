"""Points of Interest — friendly infrastructure and enemy fixtures.

Each POI exposes ``effect_on(cell, world)`` returning the magnitude of its
contribution to the cell's tick rate. The sign convention follows the
owner: SE POIs return positive numbers (friendly contribution); Enemy
POIs return positive numbers too (the *magnitude* of enemy contribution).
The world's tick logic decides how to combine them.

Artillery is special — its effect is time-bounded and targets a single
cell. ``state["target"]`` is set when fired and ``state["expires_at"]``
is a sim-tick number after which the effect is dropped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TYPE_CHECKING

from .cell import Cell, Ownership
from .grid import Coord, distance
from .params import SimParams

if TYPE_CHECKING:
    from .world import World


PoiKind = Literal[
    "fob",
    "artillery",
    "fortress",
    "resistance_node",
    "build_site",
    "factory",
    "salient_staging",
]


@dataclass
class POI:
    id: str
    kind: PoiKind
    owner: Ownership
    coord: Coord
    state: dict = field(default_factory=dict)

    def effect_on(self, cell: Cell, world: "World") -> float:
        params = world.params
        d = distance(self.coord, cell.coord)

        if self.kind == "fob":
            return params.fob_buff if d <= params.fob_radius else 0.0

        if self.kind == "artillery":
            target: tuple[int, int] | None = self.state.get("target")
            expires = self.state.get("expires_at", -1)
            if target is None or world.tick > expires:
                return 0.0
            if tuple(target) == cell.coord:
                return params.arty_buff
            return 0.0

        if self.kind == "fortress":
            return params.fortress_resist if d <= params.fortress_radius else 0.0

        if self.kind == "resistance_node":
            return params.node_resist if d <= params.node_radius else 0.0

        if self.kind == "build_site":
            # Build sites are visible placeholders, not active infrastructure.
            return 0.0

        if self.kind == "salient_staging":
            # Staging POI is a target, not an active buff/debuff source.
            return 0.0

        return 0.0

    def siege_multiplier_for(self, cell: Cell, params: SimParams) -> float:
        """Cells under a Fortress need extra progress to flip."""
        if self.kind == "fortress" and distance(self.coord, cell.coord) <= params.fortress_radius:
            return params.fortress_siege_multiplier
        if self.kind == "salient_staging" and self.coord == cell.coord:
            return params.conquer_staging_siege_mult
        return 1.0

    def to_wire(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "owner": self.owner.value,
            "q": self.coord[0],
            "r": self.coord[1],
            "state": self.state,
        }
