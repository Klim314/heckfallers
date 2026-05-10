// Controller panel: wires DOM inputs to REST endpoints and reflects
// world-state updates back into the panel.

import { coordKey, distance, type Coord } from "./hex";
import { renderParamsPanel } from "./params_panel";
import { postControl, type PoiKind, type PoiState, type WorldStore } from "./state";

export class Controls {
  private pressureInput = document.getElementById("pressure") as HTMLInputElement;
  private pressureVal = document.getElementById("pressure-val")!;
  private cellInfo = document.getElementById("cell-info")!;
  private poiInfo = document.getElementById("poi-info")!;
  private removeBtn = document.getElementById("btn-remove-poi") as HTMLButtonElement;
  private fireBtn = document.getElementById("btn-fire") as HTMLButtonElement;
  private banner = document.getElementById("banner")!;
  private statSe = document.getElementById("stat-se")!;
  private statEnemy = document.getElementById("stat-enemy")!;
  private statContested = document.getElementById("stat-contested")!;
  private statTick = document.getElementById("stat-tick")!;
  private statElapsed = document.getElementById("stat-elapsed")!;
  private statRequisition = document.getElementById("stat-requisition")!;
  private statStatus = document.getElementById("stat-status")!;
  private speedInput = document.getElementById("speed") as HTMLInputElement;
  private speedVal = document.getElementById("speed-val")!;

  // Per-cell pressure that the user has set, so the slider doesn't fight
  // with re-renders snapping it back to the server's value.
  private localPressure: Map<string, number> = new Map();

  constructor(private store: WorldStore) {}

  init(): void {
    document.getElementById("btn-start")!.addEventListener("click", () => {
      postControl("/control/sim", { action: "start" }).catch(console.error);
    });
    document.getElementById("btn-pause")!.addEventListener("click", () => {
      postControl("/control/sim", { action: "pause" }).catch(console.error);
    });
    document.getElementById("btn-reset")!.addEventListener("click", () => {
      postControl("/control/sim", { action: "reset" }).catch(console.error);
    });

    this.speedInput.addEventListener("input", () => {
      const v = parseFloat(this.speedInput.value);
      this.speedVal.textContent = `${v}x`;
      postControl("/control/sim", { action: "start", speed: v })
        .catch(() => postControl("/control/sim", { action: "pause", speed: v }));
    });

    this.pressureInput.addEventListener("input", () => {
      const sel = this.store.ui.selectedCell;
      if (!sel) return;
      const v = parseFloat(this.pressureInput.value);
      this.pressureVal.textContent = String(v);
      this.localPressure.set(coordKey(sel), v);
      postControl("/control/pressure", { coord: sel, pressure: v }).catch(console.error);
    });

    this.fireBtn.addEventListener("click", () => {
      const sel = this.store.ui.selectedCell;
      if (!sel) return;
      const snap = this.store.current;
      if (!snap) return;
      const arty = snap.pois.find((p) => p.kind === "artillery" && p.owner === "se");
      if (!arty) return;
      postControl("/control/artillery/fire", { poi_id: arty.id, target: sel }).catch(console.error);
    });

    document.querySelectorAll<HTMLButtonElement>(".poi-buttons button").forEach((btn) => {
      btn.addEventListener("click", () => {
        const sel = this.store.ui.selectedCell;
        if (!sel) return;
        const kind = btn.dataset.poi as PoiKind;
        const owner = btn.dataset.owner!;
        postControl("/control/poi/place", { kind, owner, coord: sel }).catch(console.error);
      });
    });

    this.removeBtn.addEventListener("click", () => {
      const id = this.store.ui.selectedPoiId;
      if (!id) return;
      postControl("/control/poi/remove", { id }).catch(console.error);
      this.store.ui.selectedPoiId = null;
    });

    const paramsBody = document.getElementById("params-body");
    if (paramsBody) renderParamsPanel(paramsBody);

    const paramsToggle = document.getElementById("btn-params-toggle") as HTMLButtonElement | null;
    if (paramsToggle && paramsBody) {
      paramsToggle.addEventListener("click", () => {
        const open = paramsBody.classList.toggle("hidden") === false;
        paramsToggle.setAttribute("aria-expanded", String(open));
        paramsToggle.textContent = open ? "hide" : "show";
      });
    }

    document.querySelectorAll<HTMLInputElement>("[data-param]").forEach((input) => {
      const handler = () => {
        const param = input.dataset.param!;
        const isBool = input.type === "checkbox";
        const v: number | boolean = isBool ? input.checked : parseFloat(input.value);
        const display = document.querySelector(`[data-show="${param}"]`);
        if (display) display.textContent = isBool ? (v ? "on" : "off") : String(v);
        postControl("/control/params", { params: { [param]: isBool ? (v ? 1 : 0) : v } }).catch(console.error);
      };
      input.addEventListener(input.type === "checkbox" ? "change" : "input", handler);
    });

    this.store.subscribe(() => this.refresh());
  }

  selectCell(coord: Coord | null): void {
    this.store.ui.selectedCell = coord;
    const poi = coord ? this.store.poiAt(coord) : undefined;
    this.store.ui.selectedPoiId = poi ? poi.id : null;
    this.refresh();
  }

  private refresh(): void {
    const snap = this.store.current;
    if (!snap) return;

    this.statSe.textContent = `SE: ${snap.stats.se_pct}%`;
    this.statEnemy.textContent = `Enemy: ${snap.stats.enemy_pct}%`;
    this.statContested.textContent = `Contested: ${snap.stats.contested}`;
    this.statTick.textContent = `Tick ${snap.tick}`;
    this.statElapsed.textContent = formatElapsed(snap.elapsed_s);
    this.statRequisition.textContent = `Req: ${Math.round(snap.requisition ?? 0)}`;
    this.statStatus.textContent = snap.match_state.replace("_", " ");

    if (snap.match_state === "se_won") {
      this.banner.textContent = "Super Earth victorious";
      this.banner.classList.remove("hidden");
    } else if (snap.match_state === "enemy_won") {
      this.banner.textContent = "Front lost";
      this.banner.classList.remove("hidden");
    } else {
      this.banner.classList.add("hidden");
    }

    document.querySelectorAll<HTMLInputElement>("[data-param]").forEach((input) => {
      const param = input.dataset.param!;
      const val = snap.params[param];
      if (document.activeElement === input) return;
      if (input.type === "checkbox") {
        const on = !!val;
        input.checked = on;
        const display = document.querySelector(`[data-show="${param}"]`);
        if (display) display.textContent = on ? "on" : "off";
      } else if (typeof val === "number") {
        input.value = String(val);
        const display = document.querySelector(`[data-show="${param}"]`);
        if (display) display.textContent = String(val);
      }
    });

    if (document.activeElement !== this.speedInput) {
      this.speedInput.value = String(snap.speed);
      this.speedVal.textContent = `${snap.speed}x`;
    }

    const sel = this.store.ui.selectedCell;
    if (sel) {
      const cell = this.store.cellAt(sel);
      if (cell) {
        const isContested = cell.attacker !== null;
        const stateLine = isContested
          ? `defender: ${cell.defender} (attacked by ${cell.attacker})`
          : `held by: ${cell.defender}`;
        this.cellInfo.textContent =
          `(${cell.q}, ${cell.r})\n` +
          `${stateLine}\n` +
          `progress: ${cell.progress.toFixed(1)}\n` +
          `pressure: ${cell.diver_pressure.toFixed(0)}${cell.diver_pin ? " (pinned)" : ""}\n` +
          `resistance: ${cell.enemy_resistance.toFixed(1)}` +
          (cell.is_capital ? "\n*capital*" : "");
        this.pressureInput.disabled = !isContested;
        if (document.activeElement !== this.pressureInput) {
          const local = this.localPressure.get(coordKey(sel));
          const v = local ?? cell.diver_pressure;
          this.pressureInput.value = String(Math.round(v));
          this.pressureVal.textContent = String(Math.round(v));
        }
      } else {
        this.cellInfo.textContent = "no cell here";
        this.pressureInput.disabled = true;
      }

      const poi = this.store.poiAt(sel);
      if (poi) {
        const tickHz = (snap.params.tick_hz as number | undefined) ?? 5;
        this.poiInfo.textContent = poiSummary(poi, snap.tick, tickHz);
        this.removeBtn.disabled = false;
        this.store.ui.selectedPoiId = poi.id;
      } else {
        this.poiInfo.textContent = "click a POI";
        this.removeBtn.disabled = true;
        this.store.ui.selectedPoiId = null;
      }

      const arty = snap.pois.find((p) => p.kind === "artillery" && p.owner === "se");
      const cellAt = this.store.cellAt(sel);
      const artyRange = (snap.params.arty_range as number | undefined) ?? 0;
      const inRange = !!arty && distance([arty.q, arty.r], sel) <= artyRange;
      this.fireBtn.disabled = !arty || ((arty.state.shells as number | undefined) ?? 0) <= 0
        || !cellAt || cellAt.attacker === null || !inRange;

      // Block manual placement of a kind that's already pending at this
      // cell (e.g., HighCommand has a build site here for the same kind).
      const pendingTargetKind = poi && poi.kind === "build_site"
        ? (poi.state.target_kind as string | undefined)
        : undefined;
      document.querySelectorAll<HTMLButtonElement>(".poi-buttons button").forEach((btn) => {
        const kind = btn.dataset.poi;
        btn.disabled = pendingTargetKind !== undefined && kind === pendingTargetKind;
      });
    } else {
      this.cellInfo.textContent = "click a hex";
      this.poiInfo.textContent = "click a POI";
      this.pressureInput.disabled = true;
      this.removeBtn.disabled = true;
      this.fireBtn.disabled = true;
      document.querySelectorAll<HTMLButtonElement>(".poi-buttons button").forEach((btn) => {
        btn.disabled = false;
      });
    }
  }
}

function poiSummary(poi: PoiState, currentTick: number, tickHz: number): string {
  const lines = [`${poi.kind} (${poi.owner})`, `at (${poi.q}, ${poi.r})`];
  if (poi.kind === "artillery") {
    lines.push(`shells: ${(poi.state.shells as number | undefined) ?? 0}`);
    if (poi.state.target) lines.push(`target: ${JSON.stringify(poi.state.target)}`);
  } else if (poi.kind === "build_site") {
    const target = poi.state.target_kind as string | undefined;
    if (target) lines.push(`building: ${target}`);
    const completesAt = poi.state.completes_at as number | undefined;
    if (completesAt !== undefined) {
      const remaining = Math.max(0, completesAt - currentTick);
      const seconds = Math.ceil(remaining / Math.max(tickHz, 0.001));
      lines.push(`ready in ${seconds}s`);
    }
  }
  return lines.join("\n");
}

function formatElapsed(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}
