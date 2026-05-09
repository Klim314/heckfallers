"""The World — sim state plus the tick loop that mutates it."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .cell import Cell, Ownership
from .grid import Coord, distance, neighbors
from .params import SimParams
from .poi import POI, PoiKind
from . import supply as supply_mod


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
    _supply_dirty: bool = True
    _recent_flips: list[tuple[Coord, int]] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def step(self) -> None:
        """Advance the simulation by one tick. No-op when paused/finished."""
        if self.match_state != "running":
            return

        dt = (1.0 / self.params.tick_hz) * self.speed

        if self._supply_dirty:
            supply_mod.recompute_all(self)
            self._supply_dirty = False

        if self.tick % self.params.allocation_period_ticks == 0:
            from .se_ai import allocate_divers
            allocate_divers(self)

        self._stamp_artillery_shock()
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
        floor = params.supply_floor
        span = 1.0 - floor
        for cell in self.grid.values():
            if cell.attacker is None:
                continue

            se_factor = floor + span * cell.se_supply
            eff_enemy_supply = 0.0 if self.tick < cell.supply_shock_until else cell.enemy_supply
            en_factor = floor + span * eff_enemy_supply

            # base_rate stays SE-favored (Helldivers framing: SE is always the
            # global attacker on this planet). Enemy-attacker cells naturally
            # stall at progress=0 unless enemy AI actively pushes them.
            rate = params.base_rate
            rate += cell.diver_pressure * params.pressure_coefficient * se_factor
            rate -= cell.enemy_resistance * en_factor

            for poi in self.pois.values():
                contribution = poi.effect_on(cell, self)
                if contribution == 0.0:
                    continue
                if poi.owner == Ownership.SUPER_EARTH:
                    rate += contribution
                else:
                    rate -= contribution

            cell.progress += rate * dt
            cap = self._effective_threshold(cell) * 1.5
            # Strict-clamp: progress stays on the attacker's side. If pushed back to 0,
            # contestation stalls but persists (cell stays contested until flip).
            if cell.attacker == Ownership.SUPER_EARTH:
                cell.progress = max(0.0, min(cap, cell.progress))
            else:
                cell.progress = max(-cap, min(0.0, cell.progress))

    def _stamp_artillery_shock(self) -> None:
        """While an artillery effect is active, force its target cell's
        defender supply to 0 — the barrage suppresses defender activity in
        that cell."""
        for poi in self.pois.values():
            if poi.kind != "artillery":
                continue
            target = poi.state.get("target")
            if target is None:
                continue
            expires = poi.state.get("expires_at", -1)
            if self.tick > expires:
                continue
            target_cell = self.grid.get(tuple(target))
            if target_cell is None:
                continue
            if target_cell.supply_shock_until < expires:
                target_cell.supply_shock_until = expires

    def _resolve_flips(self) -> None:
        flips: list[tuple[Cell, Ownership]] = []

        for cell in self.grid.values():
            if cell.attacker is None:
                continue
            threshold = self._effective_threshold(cell)
            if cell.attacker == Ownership.SUPER_EARTH and cell.progress >= threshold:
                flips.append((cell, Ownership.SUPER_EARTH))
            elif cell.attacker == Ownership.ENEMY and cell.progress <= -threshold:
                flips.append((cell, Ownership.ENEMY))

        for cell, new_defender in flips:
            self._flip_cell(cell, new_defender)

    def _effective_threshold(self, cell: Cell) -> float:
        mult = 1.0
        for poi in self.pois.values():
            mult = max(mult, poi.siege_multiplier_for(cell, self.params))
        return self.params.flip_threshold * mult

    def _flip_cell(self, cell: Cell, new_defender: Ownership) -> None:
        breakthrough = self._is_breakthrough(cell, new_defender)

        cell.defender = new_defender
        cell.attacker = None
        cell.progress = 0.0
        cell.diver_pressure = 0.0
        cell.diver_pin = False
        cell.enemy_resistance = 0.0

        # Destroy POIs on this cell whose owner is opposite to the new defender.
        opposite = Ownership.ENEMY if new_defender == Ownership.SUPER_EARTH else Ownership.SUPER_EARTH
        doomed = [pid for pid, poi in self.pois.items() if poi.coord == cell.coord and poi.owner == opposite]
        for pid in doomed:
            del self.pois[pid]

        # Open the new front: opposing-defended neighbors that aren't already
        # under attack get attacked by the new defender.
        opposing = Ownership.ENEMY if new_defender == Ownership.SUPER_EARTH else Ownership.SUPER_EARTH
        breakthrough_until = -1
        if breakthrough:
            breakthrough_until = self.tick + int(
                self.params.breakthrough_duration_s * self.params.tick_hz
            )
        for ncoord in neighbors(cell.coord):
            ncell = self.grid.get(ncoord)
            if ncell is None:
                continue
            if ncell.defender == opposing and ncell.attacker is None:
                ncell.attacker = new_defender
                ncell.progress = 0.0
            if breakthrough and ncell.attacker is not None:
                if ncell.supply_shock_until < breakthrough_until:
                    ncell.supply_shock_until = breakthrough_until

        # Track for cascade detection. Prune to the breakthrough window only.
        window_ticks = int(self.params.breakthrough_window_s * self.params.tick_hz)
        cutoff = self.tick - window_ticks
        self._recent_flips = [(c, t) for c, t in self._recent_flips if t >= cutoff]
        self._recent_flips.append((cell.coord, self.tick))
        self._supply_dirty = True

    def _is_breakthrough(self, cell: Cell, new_defender: Ownership) -> bool:
        # v1: only fires when SE flips an enemy cell, since enemy is the
        # rooted defender. SE-defender supply isn't modeled yet.
        if new_defender != Ownership.SUPER_EARTH:
            return False

        params = self.params
        if cell.enemy_supply < params.breakthrough_supply_threshold:
            return True

        window_ticks = int(params.breakthrough_window_s * params.tick_hz)
        cutoff = self.tick - window_ticks
        for prev_coord, prev_tick in self._recent_flips:
            if prev_tick < cutoff:
                continue
            if distance(prev_coord, cell.coord) <= 2:
                return True
        return False

    def _check_end_state(self) -> None:
        # Capital is an SE-win shortcut: SE wins the moment they capture it.
        # Enemy never wins by holding it (the asymmetric Helldivers framing).
        capital = next((c for c in self.grid.values() if c.is_capital), None)
        if capital is not None and capital.defender == Ownership.SUPER_EARTH and capital.attacker is None:
            self.match_state = "se_won"
            return

        has_enemy = any(c.defender == Ownership.ENEMY and c.attacker is None for c in self.grid.values())
        has_se = any(c.defender == Ownership.SUPER_EARTH and c.attacker is None for c in self.grid.values())
        has_contested = any(c.attacker is not None for c in self.grid.values())

        if not has_enemy and not has_contested and has_se:
            self.match_state = "se_won"
        elif not has_se and not has_contested and has_enemy:
            self.match_state = "enemy_won"

    # ------------------------------------------------------------------ #
    # Mutators called by control endpoints
    # ------------------------------------------------------------------ #

    def set_pressure(self, coord: Coord, pressure: float) -> bool:
        cell = self.grid.get(coord)
        if cell is None or cell.attacker is None:
            return False
        v = max(0.0, pressure)
        cell.diver_pressure = v
        # Slider value doubles as the pin signal: any positive value pins the
        # cell (diver AI leaves it alone), 0 releases it back to the allocator.
        cell.diver_pin = v > 0.0
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
        self._supply_dirty = True
        return poi

    def remove_poi(self, poi_id: str) -> bool:
        removed = self.pois.pop(poi_id, None) is not None
        if removed:
            self._supply_dirty = True
        return removed

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
        self._supply_dirty = True
        self._recent_flips = []

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _poi_placement_allowed(self, kind: PoiKind, owner: Ownership, cell: Cell) -> bool:
        if kind == "fob":
            return (owner == Ownership.SUPER_EARTH
                    and cell.defender == Ownership.SUPER_EARTH
                    and cell.attacker is None)
        if kind == "artillery":
            return (owner == Ownership.SUPER_EARTH
                    and cell.defender == Ownership.SUPER_EARTH
                    and cell.attacker is None)
        if kind == "fortress":
            return (owner == Ownership.ENEMY
                    and cell.defender == Ownership.ENEMY
                    and cell.attacker is None)
        if kind == "resistance_node":
            return owner == Ownership.ENEMY and cell.defender == Ownership.ENEMY
        return False

    def contested_cells(self) -> list[Cell]:
        return [c for c in self.grid.values() if c.attacker is not None]

    def stats(self) -> dict:
        total = len(self.grid)
        se = sum(1 for c in self.grid.values()
                 if c.defender == Ownership.SUPER_EARTH and c.attacker is None)
        enemy = sum(1 for c in self.grid.values()
                    if c.defender == Ownership.ENEMY and c.attacker is None)
        contested = sum(1 for c in self.grid.values() if c.attacker is not None)
        return {
            "total": total,
            "se": se,
            "enemy": enemy,
            "contested": contested,
            "se_pct": round(100 * se / total, 1) if total else 0,
            "enemy_pct": round(100 * enemy / total, 1) if total else 0,
        }
