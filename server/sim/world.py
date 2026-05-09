"""The World — sim state plus the tick loop that mutates it."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .cell import Cell, Ownership
from .grid import Coord, neighbors
from .params import SimParams
from .poi import POI, PoiKind


MatchState = Literal["running", "paused", "se_won", "enemy_won"]


@dataclass
class World:
    grid: dict[Coord, Cell] = field(default_factory=dict)
    pois: dict[str, POI] = field(default_factory=dict)
    params: SimParams = field(default_factory=SimParams)
    tick: int = 0
    elapsed_s: float = 0.0
    speed: float = 1.0
    match_state: MatchState = "paused"
    scenario_name: str = "demo_planet"
    _next_poi_id: int = 1

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def step(self) -> None:
        """Advance the simulation by one tick. No-op when paused/finished."""
        if self.match_state != "running":
            return

        dt = (1.0 / self.params.tick_hz) * self.speed

        self._apply_pressure(dt)
        self._resolve_flips()
        if self.tick % self.params.enemy_ai_period_ticks == 0:
            from .enemy_ai import update_enemy_pressure
            update_enemy_pressure(self)
        if self.tick % self.params.enemy_spawn_period_ticks == 0 and self.tick > 0:
            from .enemy_ai import maybe_spawn_resistance_node
            maybe_spawn_resistance_node(self)
        self._check_end_state()

        self.tick += 1
        self.elapsed_s += dt

    # ------------------------------------------------------------------ #
    # Tick phases
    # ------------------------------------------------------------------ #

    def _apply_pressure(self, dt: float) -> None:
        params = self.params
        for cell in self.grid.values():
            if cell.ownership != Ownership.CONTESTED:
                continue

            rate = params.base_rate
            rate += cell.diver_pressure * params.pressure_coefficient
            rate -= cell.enemy_resistance

            for poi in self.pois.values():
                contribution = poi.effect_on(cell, self)
                if contribution == 0.0:
                    continue
                if poi.owner == Ownership.SUPER_EARTH:
                    rate += contribution
                else:
                    rate -= contribution

            cell.progress += rate * dt
            cell.progress = max(-150.0, min(150.0, cell.progress))

    def _resolve_flips(self) -> None:
        params = self.params
        flips: list[tuple[Cell, Ownership]] = []

        for cell in self.grid.values():
            if cell.ownership != Ownership.CONTESTED:
                continue
            threshold = self._effective_threshold(cell)
            if cell.progress >= threshold:
                flips.append((cell, Ownership.SUPER_EARTH))
            elif cell.progress <= -threshold:
                flips.append((cell, Ownership.ENEMY))

        for cell, new_owner in flips:
            self._flip_cell(cell, new_owner)

    def _effective_threshold(self, cell: Cell) -> float:
        mult = 1.0
        for poi in self.pois.values():
            mult = max(mult, poi.siege_multiplier_for(cell, self.params))
        return self.params.flip_threshold * mult

    def _flip_cell(self, cell: Cell, new_owner: Ownership) -> None:
        cell.ownership = new_owner
        cell.progress = 0.0
        cell.diver_pressure = 0.0
        cell.enemy_resistance = 0.0

        # Destroy POIs on this cell whose owner is opposite to the new owner.
        opposite = Ownership.ENEMY if new_owner == Ownership.SUPER_EARTH else Ownership.SUPER_EARTH
        doomed = [pid for pid, poi in self.pois.items() if poi.coord == cell.coord and poi.owner == opposite]
        for pid in doomed:
            del self.pois[pid]

        # Open the new front: opposing-faction neighbors become Contested.
        opposing = Ownership.ENEMY if new_owner == Ownership.SUPER_EARTH else Ownership.SUPER_EARTH
        for ncoord in neighbors(cell.coord):
            ncell = self.grid.get(ncoord)
            if ncell is None:
                continue
            if ncell.ownership == opposing:
                ncell.ownership = Ownership.CONTESTED
                ncell.progress = 0.0

    def _check_end_state(self) -> None:
        # Capital is an SE-win shortcut: SE wins the moment they capture it.
        # Enemy never wins by holding it (the asymmetric Helldivers framing).
        capital = next((c for c in self.grid.values() if c.is_capital), None)
        if capital is not None and capital.ownership == Ownership.SUPER_EARTH:
            self.match_state = "se_won"
            return

        has_enemy = any(c.ownership == Ownership.ENEMY for c in self.grid.values())
        has_se = any(c.ownership == Ownership.SUPER_EARTH for c in self.grid.values())
        has_contested = any(c.ownership == Ownership.CONTESTED for c in self.grid.values())

        if not has_enemy and not has_contested and has_se:
            self.match_state = "se_won"
        elif not has_se and not has_contested and has_enemy:
            self.match_state = "enemy_won"

    # ------------------------------------------------------------------ #
    # Mutators called by control endpoints
    # ------------------------------------------------------------------ #

    def set_pressure(self, coord: Coord, pressure: float) -> bool:
        cell = self.grid.get(coord)
        if cell is None or cell.ownership != Ownership.CONTESTED:
            return False
        cell.diver_pressure = max(0.0, pressure)
        return True

    def place_poi(self, kind: PoiKind, owner: Ownership, coord: Coord) -> POI | None:
        cell = self.grid.get(coord)
        if cell is None:
            return None
        if not self._poi_placement_allowed(kind, owner, cell):
            return None

        pid = f"poi_{self._next_poi_id}"
        self._next_poi_id += 1

        state: dict = {}
        if kind == "artillery":
            state = {
                "shells": self.params.arty_default_shells,
                "target": None,
                "expires_at": -1,
            }

        poi = POI(id=pid, kind=kind, owner=owner, coord=coord, state=state)
        self.pois[pid] = poi
        return poi

    def remove_poi(self, poi_id: str) -> bool:
        return self.pois.pop(poi_id, None) is not None

    def fire_artillery(self, poi_id: str, target: Coord) -> bool:
        poi = self.pois.get(poi_id)
        if poi is None or poi.kind != "artillery":
            return False
        if self.grid.get(target) is None:
            return False
        if poi.state.get("shells", 0) <= 0:
            return False
        poi.state["shells"] -= 1
        poi.state["target"] = list(target)
        duration_ticks = int(self.params.arty_duration_s * self.params.tick_hz)
        poi.state["expires_at"] = self.tick + duration_ticks
        return True

    def reset_match(self) -> None:
        from .scenarios import load_scenario
        load_scenario(self, self.scenario_name)
        self.match_state = "paused"
        self.tick = 0
        self.elapsed_s = 0.0

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _poi_placement_allowed(self, kind: PoiKind, owner: Ownership, cell: Cell) -> bool:
        if kind == "fob":
            return owner == Ownership.SUPER_EARTH and cell.ownership == Ownership.SUPER_EARTH
        if kind == "artillery":
            return owner == Ownership.SUPER_EARTH and cell.ownership == Ownership.SUPER_EARTH
        if kind == "fortress":
            return owner == Ownership.ENEMY and cell.ownership == Ownership.ENEMY
        if kind == "resistance_node":
            return owner == Ownership.ENEMY and cell.ownership in (Ownership.ENEMY, Ownership.CONTESTED)
        return False

    def contested_cells(self) -> list[Cell]:
        return [c for c in self.grid.values() if c.ownership == Ownership.CONTESTED]

    def stats(self) -> dict:
        total = len(self.grid)
        se = sum(1 for c in self.grid.values() if c.ownership == Ownership.SUPER_EARTH)
        enemy = sum(1 for c in self.grid.values() if c.ownership == Ownership.ENEMY)
        contested = total - se - enemy
        return {
            "total": total,
            "se": se,
            "enemy": enemy,
            "contested": contested,
            "se_pct": round(100 * se / total, 1) if total else 0,
            "enemy_pct": round(100 * enemy / total, 1) if total else 0,
        }
