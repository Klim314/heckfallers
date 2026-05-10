"""The World — sim state plus the tick loop that mutates it."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .cell import Cell, Ownership
from .events import emit
from .grid import Coord, distance, neighbors
from .params import SimParams
from .poi import POI, PoiKind
from .salient import Salient
from . import salient as salient_mod
from . import factory as factory_mod
from . import supply as supply_mod


MatchState = Literal["running", "paused", "se_won", "enemy_won"]


def _default_controller():
    # Local import keeps controllers free to import World types at module level.
    from .controllers import OpportunisticController
    return OpportunisticController()


def _default_se_controller():
    from .controllers import HighCommandController
    return HighCommandController()


@dataclass
class World:
    grid: dict[Coord, Cell] = field(default_factory=dict)
    pois: dict[str, POI] = field(default_factory=dict)
    salients: dict[str, Salient] = field(default_factory=dict)
    params: SimParams = field(default_factory=SimParams)
    tick: int = 0
    elapsed_s: float = 0.0
    speed: float = 1.0
    match_state: MatchState = "paused"
    scenario_name: str = "demo_planet"
    controller: object = field(default_factory=_default_controller)
    se_controller: object = field(default_factory=_default_se_controller)
    match_events: list[dict] = field(default_factory=list)
    _next_poi_id: int = 1
    _next_salient_id: int = 1
    _supply_dirty: bool = True
    _recent_flips: list[tuple[Coord, int]] = field(default_factory=list)
    # Retaliation gauge: leaky integrator of net SE captures. Floors at 0,
    # decays per tick, fires a conquer salient when threshold is crossed.
    # See params.retaliation_* for tuning.
    retaliation_gauge: float = 0.0
    # Buffer of recent SE flip events, used to choose conquer-salient
    # cluster centers. Pruned each step to recent_se_flip_window_ticks.
    _recent_se_flips: list[tuple[Coord, int]] = field(default_factory=list)

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

        # SE high command runs after the diver allocator so its placements
        # take effect before pressure resolves this tick. Cadence-gated
        # internally; defaults to a no-op until enabled in params.
        self.se_controller.tick(self)

        self._stamp_artillery_shock()
        salient_mod.apply_salient_pressure(self)
        factory_mod.tick_factories(self)
        factory_mod.apply_factory_pressure(self)
        self._apply_pressure(dt)
        self._resolve_flips()
        # Resolve any build sites whose timer has elapsed (and that
        # survived the flip pass above) into their target POI kind.
        self._resolve_build_sites()
        # Controller owns enemy decisions (tactical resistance + node spawn +
        # strategic salient spawning). Salient mechanics (lifetime, success
        # detection) run unconditionally regardless of which controller is in.
        self.controller.tick(self)
        salient_mod.update_salients(self)
        # Retaliation gauge: continuous decay + prune the SE-flip buffer
        # past its window. Runs after the controller so the controller sees
        # today's accumulated gauge before today's decay erodes it.
        self.retaliation_gauge = max(
            0.0, self.retaliation_gauge - self.params.retaliation_gauge_decay_per_tick
        )
        flip_window = self.params.recent_se_flip_window_ticks
        flip_cutoff = self.tick - flip_window
        self._recent_se_flips = [
            (c, t) for c, t in self._recent_se_flips if t >= flip_cutoff
        ]
        # Bound match_events so they don't grow without limit. Latest 100 wins.
        if len(self.match_events) > 100:
            del self.match_events[: len(self.match_events) - 100]
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
            rate -= cell.salient_pressure * params.pressure_coefficient * en_factor
            rate -= cell.factory_pressure * params.pressure_coefficient * en_factor
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
            # Symmetric clamp — progress can swing across zero in either
            # direction. Capture happens at +/- flip_threshold in the
            # attacker's favor; repulse happens at the opposite repulse
            # threshold, both resolved in _resolve_flips.
            cell.progress = max(-cap, min(cap, cell.progress))

            # Active-front latch: stamp whenever progress is in the
            # attacker's favor by more than the epsilon. Render uses this to
            # avoid strobing the "active" state on momentary dips.
            attacker_favor = cell.progress if cell.attacker == Ownership.SUPER_EARTH else -cell.progress
            if attacker_favor > params.active_progress_epsilon:
                cell.active_until_tick = self.tick + int(params.active_latch_s * params.tick_hz)

    def _resolve_build_sites(self) -> None:
        """Resolve any build_site POI whose ``completes_at`` has elapsed
        into its target kind, mutating in place so the POI id stays stable
        for client tracking. Triggers a supply recompute since FOBs change
        attacker supply via fob_supply_bonus."""
        for poi in self.pois.values():
            if poi.kind != "build_site":
                continue
            if self.tick < poi.state.get("completes_at", 0):
                continue
            target_kind = poi.state.get("target_kind")
            if target_kind is None:
                continue
            poi.kind = target_kind
            if target_kind == "artillery":
                poi.state = {
                    "shells": self.params.arty_default_shells,
                    "target": None,
                    "expires_at": -1,
                }
            else:
                poi.state = {}
            emit(
                self, "build_completed",
                coord=list(poi.coord),
                kind=target_kind,
                owner=poi.owner.value,
                poi_id=poi.id,
            )
            self._supply_dirty = True

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
        repulses: list[Cell] = []

        repulse_ratio = self.params.repulse_threshold_ratio
        for cell in self.grid.values():
            if cell.attacker is None:
                continue
            threshold = self._effective_threshold(cell)
            repulse = threshold * repulse_ratio
            if cell.attacker == Ownership.SUPER_EARTH:
                if cell.progress >= threshold:
                    flips.append((cell, Ownership.SUPER_EARTH))
                elif cell.progress <= -repulse:
                    repulses.append(cell)
            else:
                if cell.progress <= -threshold:
                    flips.append((cell, Ownership.ENEMY))
                elif cell.progress >= repulse:
                    repulses.append(cell)

        for cell, new_defender in flips:
            self._flip_cell(cell, new_defender)
        for cell in repulses:
            self._repulse_cell(cell)

    def _effective_threshold(self, cell: Cell) -> float:
        mult = 1.0
        for poi in self.pois.values():
            mult = max(mult, poi.siege_multiplier_for(cell, self.params))
        return self.params.flip_threshold * mult

    def _repulse_cell(self, cell: Cell) -> None:
        """Incursion driven off — defender keeps the cell, contest state clears."""
        defender = cell.defender.value
        cell.attacker = None
        cell.progress = 0.0
        cell.diver_pressure = 0.0
        cell.diver_pin = False
        cell.enemy_resistance = 0.0
        cell.active_until_tick = -1
        emit(self, "cell_repulsed", coord=list(cell.coord), defender=defender)

    def _flip_cell(self, cell: Cell, new_defender: Ownership) -> None:
        breakthrough = self._is_breakthrough(cell, new_defender)
        emit(
            self, "cell_captured",
            coord=list(cell.coord),
            defender=new_defender.value,
            breakthrough=breakthrough,
        )

        cell.defender = new_defender
        cell.attacker = None
        cell.progress = 0.0
        cell.diver_pressure = 0.0
        cell.diver_pin = False
        cell.enemy_resistance = 0.0
        cell.active_until_tick = -1

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

        # Retaliation gauge — SE captures push up, enemy captures resist.
        # Floor at 0; decay happens once per step in ``step``.
        if new_defender == Ownership.SUPER_EARTH:
            self.retaliation_gauge += self.params.retaliation_w_se_flip
            self._recent_se_flips.append((cell.coord, self.tick))
        else:
            self.retaliation_gauge = max(
                0.0, self.retaliation_gauge - self.params.retaliation_w_enemy_flip
            )

        self._supply_dirty = True

        # Conquer-salient spread hook fires last, after default bookkeeping.
        salient_mod.on_cell_flip(self, cell.coord, new_defender)

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
        prev_state = self.match_state

        # Capital is an SE-win shortcut: SE wins the moment they capture it.
        # Enemy never wins by holding it (the asymmetric Helldivers framing).
        capital = next((c for c in self.grid.values() if c.is_capital), None)
        if capital is not None and capital.defender == Ownership.SUPER_EARTH and capital.attacker is None:
            self.match_state = "se_won"
        else:
            has_enemy = any(c.defender == Ownership.ENEMY and c.attacker is None for c in self.grid.values())
            has_se = any(c.defender == Ownership.SUPER_EARTH and c.attacker is None for c in self.grid.values())
            has_contested = any(c.attacker is not None for c in self.grid.values())

            if not has_enemy and not has_contested and has_se:
                self.match_state = "se_won"
            elif not has_se and not has_contested and has_enemy:
                self.match_state = "enemy_won"

        if self.match_state != prev_state and self.match_state in ("se_won", "enemy_won"):
            winner = "se" if self.match_state == "se_won" else "enemy"
            emit(self, "match_ended", winner=winner)

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
        elif kind == "factory":
            state = {"active_targets": []}

        poi = POI(id=pid, kind=kind, owner=owner, coord=coord, state=state)
        self.pois[pid] = poi
        self._supply_dirty = True
        emit(
            self, "poi_placed",
            poi_id=pid, kind=kind, owner=owner.value, coord=list(coord),
        )
        return poi

    def place_build_site(
        self,
        target_kind: PoiKind,
        owner: Ownership,
        coord: Coord,
        duration_ticks: int | None = None,
    ) -> POI | None:
        """Create a pending build site that resolves to ``target_kind`` after
        ``duration_ticks`` ticks. Used for SE FOB / artillery placement —
        fresh builds and (Phase 4b) moves both route through this so the
        construction window is exposed to enemy interruption.

        Placement is permitted on cells where the *target* kind would be
        permitted; the build site itself is owner-tagged SE so the
        flip-cell teardown logic destroys it if the cell flips to enemy.
        """
        cell = self.grid.get(coord)
        if cell is None:
            return None
        if not self._poi_placement_allowed(target_kind, owner, cell):
            return None

        pid = f"poi_{self._next_poi_id}"
        self._next_poi_id += 1

        if duration_ticks is None:
            duration_ticks = self.params.fresh_build_ticks
        completes_at = self.tick + max(0, duration_ticks)

        poi = POI(
            id=pid,
            kind="build_site",
            owner=owner,
            coord=coord,
            state={"target_kind": target_kind, "completes_at": completes_at},
        )
        self.pois[pid] = poi
        emit(
            self, "build_started",
            coord=list(coord),
            target_kind=target_kind,
            owner=owner.value,
            completes_at=completes_at,
            poi_id=pid,
        )
        # Build sites don't change supply (no effect_on contribution), but a
        # later resolution will. Leave _supply_dirty alone here; the resolve
        # phase sets it.
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
        # Clamp the user-tunable param: negative values would silently brick
        # all firing (distance is always >= 0), zero allows only self-fire.
        if distance(poi.coord, target) > max(0, self.params.arty_range):
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
        self.retaliation_gauge = 0.0
        self._recent_se_flips = []
        self.salients.clear()
        self.match_events.clear()
        self._next_salient_id = 1
        # Re-instantiate controllers so any accrued state (requisition,
        # cooldowns) doesn't leak between matches.
        self.controller = _default_controller()
        self.se_controller = _default_se_controller()

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
        if kind == "factory":
            return (owner == Ownership.ENEMY
                    and cell.defender == Ownership.ENEMY
                    and cell.attacker is None)
        if kind == "salient_staging":
            return (owner == Ownership.ENEMY
                    and cell.defender == Ownership.ENEMY
                    and cell.attacker is None)
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
