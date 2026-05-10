# Conquer salient redesign — implementation plan

Status: design-approved, ready to implement.

## Why

The current conquer salient ([server/sim/salient.py](../server/sim/salient.py)) drops a static union-of-disks region and opens every SE cell in it to enemy contestation on tick 1, then stamps low pressure for ~120s. There's no temporal shape, no directional intent, and no progressive counterplay — divers either tank the whole stamp or don't, and the salient's footprint never moves.

The redesign replaces this with a **leapfrogging directional wedge** that:
- Telegraphs through a visible staging POI (counterplay A: divers can clear it before fire)
- Activates with a small initial fan (2 or 3 cells, rolled)
- Spreads on flip events into the forward hemisphere only (no retreat / curl-back)
- Uses additive probability across adjacent salient cells (multiple footholds → stronger push)
- Decays per generation (geometric `decay_base^gen`) so the wedge naturally peters out
- Lets divers blunt by repulsing contested cells before flip — drops them from the active set, lowering `k` for future spread rolls (counterplay B)

## Final design (locked)

### Staging phase
- New POI kind `"salient_staging"` (enemy-owned), visible from spawn.
- Placed on the closest enemy-defended cell to a **target SE cell** chosen from the recent-flip buffer (single best cluster center, k=1).
- `state["charge_completes_at"]` ticks down each step.
- Fortress-like siege multiplier active on its own cell — divers take meaningful time to clear it.
- If divers clear the staging POI before charge completes, the parent salient is destroyed (counterplay A).
- Self-destructs on activation. Does not persist as a spine.

### Activation
- Roll `fan_size ∈ {fan_min, fan_max}` (default {2, 3}).
- Pick that many cells along the axis from the staging coord toward the target SE cell:
  - Cell 1: pure-forward hex from staging.
  - Cells 2 and 3 (if rolled): the two forward-lateral hexes from staging.
- Contest each chosen cell (`cell.attacker = ENEMY`, `cell.progress = 0`); add to `tracked_cells` with `gen=0`.
- Freeze `axis = centroid(initial_cells) - staging_coord`. Axis does not re-aim after activation.
- Remove the staging POI.
- Set `salient.activated = True`.

### Propagation (flip-triggered, additive, decaying)
- Hook fires in `_flip_cell` ([server/sim/world.py:257](../server/sim/world.py#L257)) after existing bookkeeping, when a cell flips to `Ownership.ENEMY`. Synchronous (the new contestations cannot themselves flip in the same tick, so no cascade risk).
- For the flipping cell C, find the salient that tracks `C.coord`. (At most one — see edge cases.)
- For each neighbor N of C in the **forward hemisphere** of the salient's axis (3 of 6 offsets), if N is on-grid AND uncontested AND SE-defended:
  - Compute `k` = number of currently-tracked salient cells adjacent to N.
  - Compute `gen_new = C.gen + 1`. If `gen_new > conquer_max_gen`, skip.
  - Compute `p_per_source = conquer_spread_p_base * (conquer_spread_decay_base ** gen_new)`.
  - Compute `P_contest = 1 - (1 - p_per_source) ** k`.
  - Roll uniform [0,1); if `< P_contest`, contest N (`attacker = ENEMY`, `progress = 0`) and add to `tracked_cells` with `gen = gen_new`.

### Tracked-cells lifecycle
- Add: on initial fan creation (gen=0) and on successful spread roll (gen+1).
- Drop (lazy, in `update_salients`): any tracked coord whose cell has `defender == SE and attacker is None` — covers both repulse and SE recapture of a flipped salient cell.
- Cells flipped by the salient stay in `tracked_cells` (defender=ENEMY, attacker=None) so they keep contributing to `k` for adjacent neighbors when *those* neighbors flip later.

### Pressure stamping
- Each tick, `apply_salient_pressure` rebuilds `cell.salient_pressure` from `tracked_cells.keys()` of every activated conquer salient (max-saturating with destroy corridors as today).
- Pre-activation conquer salients stamp **no pressure** — staging POI is a target, not a pressure source.
- Non-conquer (destroy) salient logic in this function unchanged.

### Termination
- Lifetime expiry: unchanged (`expires_tick` on the salient).
- Natural extinction: when an activated conquer salient's `tracked_cells` becomes empty (all repulsed or recaptured), end immediately with reason `"extinguished"` (new reason value).
- Pre-activation salient where staging POI was destroyed: end with reason `"intercepted"` (new reason value).

### Hex forward-hemisphere math
Given `axis: Coord` (typically a multi-step vector), the 3 forward NEIGHBOR_DIRS are those with non-negative dot product against axis in **cube coordinates**:

```
def axial_to_cube(c: Coord) -> tuple[int, int, int]:
    q, r = c
    return (q, -q - r, r)

def forward_hemisphere(axis: Coord) -> tuple[Coord, ...]:
    ax_cube = axial_to_cube(axis)
    out = []
    for d in NEIGHBOR_DIRS:
        d_cube = axial_to_cube(d)
        dot = sum(a * b for a, b in zip(ax_cube, d_cube))
        if dot >= 0:
            out.append(d)
    # Tie-breakers (dot==0) are deterministic via NEIGHBOR_DIRS ordering.
    return tuple(out)
```

For a "pure" hex axis (multiple of one NEIGHBOR_DIR), this returns exactly 3 directions. For a diagonal axis (sum of two NEIGHBOR_DIRs), ties on `dot==0` may include a 4th — clamp to the top 3 by dot value if needed. Implementation note: if `len(out) > 3`, sort by dot desc and take 3.

## Files touched

### Server core
| File | Change |
|------|--------|
| [server/sim/params.py](../server/sim/params.py) | Add new params; remove two unused ones (see below) |
| [server/sim/poi.py](../server/sim/poi.py) | Extend `PoiKind`; add `salient_staging` branches in `effect_on` and `siege_multiplier_for` |
| [server/sim/grid.py](../server/sim/grid.py) | Add `forward_hemisphere(axis)` helper |
| [server/sim/salient.py](../server/sim/salient.py) | Heavy refactor: dataclass fields, staging spawn, activation, flip-triggered spread, tracked_cells pressure stamping |
| [server/sim/world.py](../server/sim/world.py) | Wire flip hook into `_flip_cell`; permit `salient_staging` placement |
| [server/sim/controllers/opportunistic.py](../server/sim/controllers/opportunistic.py) | Replace `spawn_conquer_salient(centers)` call with `spawn_conquer_staging(target_se_cell)` |
| [server/sim/events.py](../server/sim/events.py) | New event kinds: `salient_staging_spawned`, `salient_activated`; new end reasons `extinguished`, `intercepted` |
| [server/api/serialize.py](../server/api/serialize.py) | Verify new POI kind + Salient fields serialize correctly |

### Client
| File | Change |
|------|--------|
| [client/src/state.ts](../client/src/state.ts) | Add `salient_staging` POI kind; update Salient shape (drop `region`, add `tracked_cells`, `axis`, `activated`, `staging_poi_id`) |
| [client/src/render.ts](../client/src/render.ts) | Render staging POI as warning glyph; render salient footprint from `tracked_cells` |
| [client/src/events_panel.ts](../client/src/events_panel.ts) | Render new event kinds + new end reasons |

### Tests
| File | Change |
|------|--------|
| [server/tests/test_salient.py](../server/tests/test_salient.py) | Replace conquer-related cases (see Test surface below) |
| [server/tests/test_retaliation.py](../server/tests/test_retaliation.py) | Update: gauge crossing now produces a staging POI, not an active spreading salient |
| [server/tests/conftest.py](../server/tests/conftest.py) | Update fixture params; remove old `conquer_cluster_*` references |

## Param changes

### Add
| Name | Default | Notes |
|------|---------|-------|
| `conquer_staging_charge_s` | `15.0` | Time from staging spawn to activation |
| `conquer_staging_siege_mult` | `2.0` | Multiplier on flip threshold for the staging POI's own cell — slows divers clearing it |
| `conquer_fan_min` | `2` | Inclusive lower bound of initial fan-size roll |
| `conquer_fan_max` | `3` | Inclusive upper bound |
| `conquer_spread_p_base` | `0.35` | Per-source probability before generation decay |
| `conquer_spread_decay_base` | `0.7` | `decay_base^gen` reduces probability with depth |
| `conquer_max_gen` | `6` | Hard safety cap on wedge depth |

### Retire
- `conquer_cluster_radius` — no more region-based spawning
- `conquer_cluster_count` — single-target staging replaces multi-center

### Keep
- `conquer_pressure_magnitude`, `conquer_salient_lifetime_s`, `max_active_conquer_salients`
- `retaliation_*` (unchanged)
- `recent_se_flip_window_ticks` (still used to pick target SE cell)

## Salient dataclass shape

```python
@dataclass
class Salient:
    id: str
    kind: SalientKind                       # "destroy" | "conquer"
    spawned_tick: int
    expires_tick: int

    # destroy-only
    corridor: list[Coord] = field(default_factory=list)
    target: Coord | None = None
    target_poi_id: str | None = None

    # conquer-only (new shape)
    activated: bool = False
    staging_poi_id: str | None = None
    axis: Coord | None = None               # (dq, dr); None until activation
    fan_size: int = 0                       # rolled at staging spawn
    tracked_cells: dict[Coord, int] = field(default_factory=dict)  # coord -> gen

    state: dict = field(default_factory=dict)  # kept for forward-compat
```

The old `region: list[Coord]` field is **removed**. Any test or serialization touching `region` for conquer must move to `tracked_cells`.

## Execution order

Each phase is internally consistent; do not skip ahead because later phases assume the foundation laid by earlier ones.

### Phase 1 — Foundation (no behavior change visible to a running sim)
1. **params.py**: add the seven new params with defaults above. Leave `conquer_cluster_radius` / `conquer_cluster_count` in place for now (deleted in Phase 3).
2. **grid.py**: add `forward_hemisphere(axis: Coord) -> tuple[Coord, ...]` per the math snippet. Add a docstring noting the cube-coord dot product and the `len > 3` clamp behavior.
3. **poi.py**: extend `PoiKind` literal with `"salient_staging"`. In `effect_on`, return `0.0` for it (no buff/debuff). In `siege_multiplier_for`, return `params.conquer_staging_siege_mult` if `kind == "salient_staging"` and `coord == cell.coord`. Otherwise return existing default.
4. **salient.py**: extend `Salient` dataclass per the shape above. Keep all existing functions intact — just additive field changes. Update `to_wire` to include the new fields when set.
5. **world.py**: extend `_poi_placement_allowed` to permit `"salient_staging"` when `owner == ENEMY` and the cell is enemy-defended uncontested.

After phase 1: tests should still pass; nothing yet calls the new code paths.

### Phase 2 — Core mechanics (new code paths added but old still wins)
6. **salient.py — new `spawn_conquer_staging(world, target_se_cell)`**:
   - Find closest enemy-defended uncontested cell to `target_se_cell` within some range cap (reuse `destroy_max_range` or hard-code 8 for now). Return `None` if none found.
   - Use `world.place_poi("salient_staging", Ownership.ENEMY, staging_coord)`.
   - Construct `Salient(kind="conquer", activated=False, staging_poi_id=poi.id, ...)`. Set `expires_tick = world.tick + lifetime_ticks` (the lifetime starts at staging, not activation — keeps the duration knob simple).
   - Roll `fan_size = randint(conquer_fan_min, conquer_fan_max)` and store on the salient (used at activation).
   - Compute initial axis hint: `target_se_cell - staging_coord` and store in `salient.axis` temporarily (replaced at activation with the centroid-based axis, but useful before then for choosing fan cells).
   - Set `staging_poi.state["charge_completes_at"] = world.tick + int(conquer_staging_charge_s * tick_hz)` and `staging_poi.state["parent_salient_id"] = salient.id`.
   - Emit `salient_staging_spawned` event with `salient_id`, `staging_coord`, `target_coord`, `charge_completes_at`.
   - Register the salient in `world.salients` and return it.
7. **salient.py — new `activate_conquer_salient(world, salient)`**:
   - Compute forward + forward-laterals from `salient.axis` (use `forward_hemisphere`; sort by dot to identify the pure-forward direction first).
   - Pick fan cells: cell 1 = staging_coord + forward dir, cells 2 and 3 = staging_coord + forward-lateral dirs in NEIGHBOR_DIRS order. Skip any that are off-grid or already enemy-defended uncontested. If fewer than 1 cell can be picked, abort (set salient to end with reason `"intercepted"` — terrain blocked it).
   - Trim to `salient.fan_size` cells.
   - For each picked cell: `cell.attacker = ENEMY`, `cell.progress = 0.0`, `salient.tracked_cells[coord] = 0`.
   - Recompute frozen axis: `axis = centroid(picked_cells) - staging_coord`. Replace `salient.axis`.
   - Remove the staging POI: `world.remove_poi(salient.staging_poi_id)`. Set `salient.staging_poi_id = None`.
   - Set `salient.activated = True`.
   - Set `world._supply_dirty = True` (newly contested cells affect supply).
   - Emit `salient_activated` event with `salient_id`, `axis`, fan coords.
8. **salient.py — extend `update_salients`**:
   - For each conquer salient where `not activated`:
     - If `staging_poi_id` is no longer in `world.pois` → end the salient with reason `"intercepted"`.
     - Else if `world.tick >= staging_poi.state["charge_completes_at"]` → call `activate_conquer_salient`.
   - For each conquer salient where `activated`:
     - Prune `tracked_cells`: drop any coord where `cell.defender == SUPER_EARTH and cell.attacker is None`.
     - If `tracked_cells` is now empty → end the salient with reason `"extinguished"`.
   - Existing destroy + lifetime logic unchanged.
9. **salient.py — new `on_cell_flip(world, coord, new_defender)`** (called from `_flip_cell`):
   - Only fires for `new_defender == Ownership.ENEMY`.
   - Find the conquer salient (if any) where `coord in salient.tracked_cells`. If none, return.
   - `parent_gen = salient.tracked_cells[coord]`.
   - For each `d in forward_hemisphere(salient.axis)`:
     - `n_coord = (coord[0] + d[0], coord[1] + d[1])`.
     - Skip if `n_coord` not in grid, or already in `tracked_cells`, or cell is not SE-defended uncontested.
     - `gen_new = parent_gen + 1`. Skip if `gen_new > conquer_max_gen`.
     - `k` = count of `t in tracked_cells` with `distance(t, n_coord) == 1`.
     - `p_per_source = conquer_spread_p_base * (conquer_spread_decay_base ** gen_new)`.
     - `P = 1 - (1 - p_per_source) ** k`.
     - `if random() < P`: contest the cell, add to `tracked_cells` with `gen_new`. Set `world._supply_dirty = True`.
   - Use `world.rng` if one exists; otherwise `random.random()`. Check `salient.py` for existing RNG conventions before adding a new one.
10. **world.py — wire the flip hook**: at the end of `_flip_cell` ([world.py:257](../server/sim/world.py#L257)) after `self._supply_dirty = True`, call `salient_mod.on_cell_flip(self, cell.coord, new_defender)`.
11. **salient.py — extend `apply_salient_pressure`**: for activated conquer salients, iterate `salient.tracked_cells.keys()` instead of `salient.region`. For unactivated conquer salients, contribute nothing. Destroy logic unchanged.

After phase 2: the new code paths exist and would work if invoked, but the controller still calls the old `spawn_conquer_salient`.

### Phase 3 — Switch the retaliation flow + cleanup
12. **opportunistic.py** ([controllers/opportunistic.py:172-184](../server/sim/controllers/opportunistic.py#L172)): replace the `find_recent_flip_clusters(k=count, ...)` + `spawn_conquer_salient(centers)` call with:
    - `target = find_recent_flip_target(world)` — new helper (or inline) that returns the single best cluster center using `find_recent_flip_clusters(k=1, ...)`.
    - If `target is None`: return without resetting gauge (hold).
    - `salient = spawn_conquer_staging(world, target)`.
    - On non-None: `world.retaliation_gauge = 0.0`.
13. **salient.py — delete dead code**:
    - `spawn_conquer_salient(world, centers)` — gone.
    - The `region` field references in `to_wire` and `apply_salient_pressure` — gone.
    - `find_recent_flip_clusters` — keep, but the `k` param is now usually 1. (Could simplify, but the function is independently useful for future clustering work; leave generic.)
14. **params.py — delete `conquer_cluster_radius` and `conquer_cluster_count`**. Search-and-replace any test fixtures that reference them.
15. **events.py** — add the new event kinds in the schema (if events.py constrains them) and add new `reason` values to `salient_ended` event handling: `extinguished`, `intercepted`.

After phase 3: the simulator end-to-end uses the new flow. Tests will fail (they're still on the old shape) — fix in phase 5.

### Phase 4 — Client wire format + render
16. **state.ts**: add `"salient_staging"` to `PoiKind`. Update the `Salient` type — drop `region`, add `tracked_cells: Array<[Coord, number]>` (server serializes the dict as list-of-tuples), `axis: Coord | null`, `activated: boolean`, `staging_poi_id: string | null`.
17. **render.ts**: render the staging POI with a distinct warning glyph (e.g. red triangular outline pulsing toward `charge_completes_at`). For activated conquer salients, render the live `tracked_cells` set as the salient footprint (instead of the old static region). Maintain the existing color / opacity for conquer pressure cells.
18. **events_panel.ts**: log the new events with appropriate icons / messages (`Salient charging at (q,r) — fires in Ns`, `Salient activated, axis (dq,dr)`, `Salient intercepted!`, `Salient extinguished`).
19. **api/serialize.py**: ensure POI state including `charge_completes_at` and `parent_salient_id` is in the wire format. Salient `to_wire` should include `activated`, `axis`, `tracked_cells`, `staging_poi_id`, `fan_size` for conquer kind.

### Phase 5 — Tests
20. **conftest.py**: add fixture defaults for new params (low `conquer_staging_charge_s` for fast tests, high `conquer_spread_p_base` for deterministic spread cases). Remove `conquer_cluster_*` references.
21. **test_salient.py — replace conquer block** with cases:
    - **staging spawn** — `spawn_conquer_staging` places a `salient_staging` POI on the closest enemy-defended cell to the target; emits `salient_staging_spawned`; registers a salient with `activated=False`.
    - **staging visibility & siege** — staging POI cell has higher effective threshold via `siege_multiplier_for`.
    - **counterplay A** — kill staging POI before charge: `update_salients` ends the salient with reason `"intercepted"`; no fan ever spawns.
    - **activation timing** — at `charge_completes_at`, `update_salients` calls `activate_conquer_salient`; staging POI gone; `activated=True`; `fan_size` cells are now in `tracked_cells` with `gen=0`.
    - **fan picks forward + forward-laterals** — with axis pointing in a known direction, fan cells lie along forward hex and forward-lateral hexes (not lateral or backward).
    - **forward-hemisphere only** — synthesize a flipping cell on the salient frontier; assert that `on_cell_flip` only ever rolls into the 3 forward neighbors. Backward neighbors are never contested even at `p_base = 1.0`.
    - **additive probability** — `k=2` neighbor (two adjacent salient cells flipping near it) gets `1 - (1-p)^2` probability. Test by setting `p_base = 0.5`, mocking `random.random` to a known value, and asserting the contest decision.
    - **counterplay B** — repulse a tracked cell before flip: `update_salients` drops it from `tracked_cells`; `k` for adjacent neighbors is now 1 less; spread roll on subsequent flips uses the lower `k`.
    - **generation decay** — at `decay_base = 0.5`, gen=2 cell's effective `p = p_base * 0.25`. Verify across multiple flip events.
    - **max_gen cap** — set `conquer_max_gen=2`; spread stops at depth 2 even with `p_base=1.0`.
    - **natural extinction** — repulse all tracked cells; `update_salients` ends the salient with reason `"extinguished"`.
    - **lifetime expiry** — unchanged behavior, ends with reason `"expired"`.
    - **pre-activation no-pressure** — `apply_salient_pressure` does not stamp on any cell while `activated=False`.
    - **post-flip cells stay tracked** — a flipped salient cell remains in `tracked_cells` and continues contributing to `k` for its neighbors.
22. **test_retaliation.py — update**:
    - Gauge crossing produces a staging POI (`salient_staging`), not an active spreading salient. Assert `activated=False`.
    - Cap on `max_active_conquer_salients` counts unactivated + activated together (a staging-phase salient still occupies a slot).
    - Empty flip buffer → no spawn, gauge held (existing test, ensure it still passes with new flow).

### Phase 6 — Smoke test
23. Run `pytest server/tests/`. All green.
24. Start the dev server, open the client. Trigger retaliation (let SE flip a cluster of cells fast). Verify visually:
    - Staging POI appears with warning glyph
    - Diver-cleared staging actually cancels the salient
    - Activation fans 2-3 cells in a directional pattern
    - Wedge spreads forward as cells flip
    - Wedge cannot retreat
    - Repulsing leading-edge cells visibly slows spread

## Edge cases to handle in code

1. **Multi-salient cell overlap.** If two conquer salients are active and one's spread roll picks a cell already in another's `tracked_cells`, skip it (no double-tracking). With `max_active_conquer_salients=1`, this is unreachable but the guard is cheap.
2. **Staging POI on a cell that gets captured by SE before charge completes.** When SE captures the staging POI's host cell, `_flip_cell` already destroys opposite-owner POIs on that cell ([world.py:274-278](../server/sim/world.py#L274)) — the staging POI vanishes, and the next `update_salients` tick will end the parent salient with `"intercepted"`. No extra code needed.
3. **Activation finds zero placeable fan cells** (all forward hexes off-grid or enemy-defended). End with `"intercepted"`. Defensive — should be near-impossible given staging placement requires an enemy-defended host with at least some forward exposure.
4. **`on_cell_flip` called from a flip the salient didn't track** (e.g. a destroy salient capture, or an organic enemy advance). Fast-path return: only act if `coord` is in some conquer salient's `tracked_cells`. O(salients × tracked_size); given caps, trivial.
5. **Axis is the zero vector** (centroid of picked cells == staging coord — pathological). `forward_hemisphere` would return all 6 directions on tie. Defensive: if `axis == (0, 0)`, fall back to the original axis hint from `spawn_conquer_staging`.
6. **RNG determinism in tests.** `on_cell_flip` uses `random.random()`. Tests must seed `random` or monkey-patch. Check whether `world` already has an injectable RNG; if not, this PR doesn't need to add one — tests can use `random.seed`.

## Open considerations (defer to post-implementation playtesting)

- **Periodic per-tick spread** — left out. Add later only if wedges feel "stuck" too often.
- **Staging POI persistence as anchor** — explicitly rejected. Self-destruct on activation.
- **Re-aiming axis** — explicitly rejected. Axis fixed at activation.
- **Fan size distribution** — currently uniform {2, 3}. Could weight (e.g. 60% chance of 3) once balance is set.
- **Stall escalation** — if a salient stalls for N ticks without spreading, should it re-fire its initial fan? Not in this PR.

## Verification checklist

Before merge:
- [ ] `pytest server/tests/` all green
- [ ] Old `region`-based conquer code paths fully deleted (grep `\.region` in salient.py — no conquer matches)
- [ ] Old `conquer_cluster_*` params not referenced anywhere (grep `conquer_cluster`)
- [ ] Client renders staging POI distinctly from other POIs
- [ ] Client renders salient footprint from `tracked_cells`, not `region`
- [ ] Manual smoke test in browser: counterplay A (kill staging) and B (repulse front cells) both visibly work
- [ ] No regression in destroy salient behavior (existing destroy tests pass)
