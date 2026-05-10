// Right-sidebar event log: subscribes to the world store and renders the
// most-recent-first list of match events emitted by the server. Formatting
// per event type lives here so the wire payload stays minimal.

import type { MatchEvent, WorldSnapshot, WorldStore } from "./state";

type FactionSide = "se" | "enemy" | "contested" | "system";

interface FormattedEvent {
  side: FactionSide;
  text: string;
}

export class EventsPanel {
  private list: HTMLUListElement;
  private lastTick: number | null = null;
  private lastLen = 0;

  constructor(private store: WorldStore) {
    this.list = document.getElementById("event-log") as HTMLUListElement;
  }

  init(): void {
    this.store.subscribe((snap) => this.refresh(snap));
  }

  private refresh(snap: WorldSnapshot): void {
    const events = snap.match_events ?? [];
    // Skip rerender when nothing changed — the WS pumps every tick and the
    // event list mostly stays still between flips.
    if (events.length === this.lastLen && snap.tick === this.lastTick) return;
    this.lastLen = events.length;
    this.lastTick = snap.tick;

    if (events.length === 0) {
      this.list.innerHTML = '<li class="event-empty">no events yet</li>';
      return;
    }

    const tickHz = (snap.params.tick_hz ?? 5) as number;
    // Render newest first, cap at 50 visible to keep DOM cheap.
    const recent = events.slice(-50).reverse();
    const frag = document.createDocumentFragment();
    for (const e of recent) {
      const formatted = formatEvent(e);
      const li = document.createElement("li");
      li.className = `ev-${formatted.side}`;
      const elapsed = Math.max(0, snap.tick - e.tick);
      const elapsedSec = elapsed / Math.max(tickHz, 0.001);
      const tickEl = document.createElement("span");
      tickEl.className = "ev-tick";
      tickEl.textContent = elapsedLabel(elapsedSec);
      const textEl = document.createElement("span");
      textEl.className = "ev-text";
      textEl.textContent = formatted.text;
      li.appendChild(tickEl);
      li.appendChild(textEl);
      frag.appendChild(li);
    }
    this.list.replaceChildren(frag);
  }
}

function elapsedLabel(seconds: number): string {
  if (seconds < 1) return "now";
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}m${s.toString().padStart(2, "0")}`;
}

function coordStr(c: unknown): string {
  if (Array.isArray(c) && c.length === 2) return `(${c[0]}, ${c[1]})`;
  return "?";
}

function kindLabel(k: unknown): string {
  if (typeof k !== "string") return "POI";
  const map: Record<string, string> = {
    fob: "FOB",
    artillery: "artillery",
    fortress: "fortress",
    resistance_node: "resistance node",
    factory: "factory",
    build_site: "build site",
    salient_staging: "salient staging",
  };
  return map[k] ?? k;
}

function ownerSide(owner: unknown): FactionSide {
  return owner === "se" ? "se" : owner === "enemy" ? "enemy" : "system";
}

function formatEvent(e: MatchEvent): FormattedEvent {
  switch (e.type) {
    case "cell_captured": {
      const defender = e.defender as string | undefined;
      const breakthrough = e.breakthrough === true;
      const side: FactionSide = defender === "se" ? "se" : "enemy";
      const who = defender === "se" ? "SE captured" : "Enemy captured";
      const tag = breakthrough ? " (breakthrough)" : "";
      return { side, text: `${who} ${coordStr(e.coord)}${tag}` };
    }
    case "cell_repulsed": {
      const defender = e.defender as string | undefined;
      const side: FactionSide = defender === "se" ? "se" : "enemy";
      const who = defender === "se" ? "SE repulsed attack on" : "Enemy held";
      return { side, text: `${who} ${coordStr(e.coord)}` };
    }
    case "salient_spawned": {
      const kind = e.kind as string | undefined;
      if (kind === "destroy") {
        const tk = kindLabel(e.target_kind);
        return {
          side: "enemy",
          text: `Strike incoming → ${tk} at ${coordStr(e.target)}`,
        };
      }
      // Conquer salients no longer emit salient_spawned (they emit
      // salient_staging_spawned then salient_activated). Only destroy reaches here.
      return { side: "enemy", text: `Salient spawned (${kind ?? "?"})` };
    }
    case "salient_staging_spawned": {
      return {
        side: "enemy",
        text: `Salient charging at ${coordStr(e.staging_coord)}`,
      };
    }
    case "salient_activated": {
      const axis = e.axis;
      const axisStr = Array.isArray(axis) && axis.length === 2 ? `(${axis[0]}, ${axis[1]})` : "?";
      return {
        side: "enemy",
        text: `Salient activated, axis ${axisStr}`,
      };
    }
    case "salient_ended": {
      const kind = e.kind as string | undefined;
      const reason = e.reason as string | undefined;
      if (reason === "success" && kind === "destroy") {
        return {
          side: "enemy",
          text: `Strike succeeded at ${coordStr(e.target)}`,
        };
      }
      if (reason === "expired") {
        const label = kind === "conquer" ? "Retaliation" : "Strike";
        return { side: "contested", text: `${label} ended (timed out)` };
      }
      if (reason === "intercepted") {
        return { side: "se", text: "Salient intercepted!" };
      }
      if (reason === "extinguished") {
        return { side: "se", text: "Salient extinguished" };
      }
      return { side: "system", text: `Salient ended (${reason ?? "?"})` };
    }
    case "build_started": {
      const tk = kindLabel(e.target_kind);
      return {
        side: ownerSide(e.owner),
        text: `Build started: ${tk} at ${coordStr(e.coord)}`,
      };
    }
    case "build_completed": {
      const k = kindLabel(e.kind);
      return {
        side: ownerSide(e.owner),
        text: `Build complete: ${k} at ${coordStr(e.coord)}`,
      };
    }
    case "poi_placed": {
      const k = kindLabel(e.kind);
      return {
        side: ownerSide(e.owner),
        text: `${k} placed at ${coordStr(e.coord)}`,
      };
    }
    case "factory_strike": {
      return {
        side: "enemy",
        text: `Factory strike at ${coordStr(e.coord)}`,
      };
    }
    case "match_ended": {
      const winner = e.winner as string | undefined;
      const text = winner === "se" ? "Match ended — SE victorious" : "Match ended — front lost";
      return { side: "system", text };
    }
    default:
      return { side: "system", text: String(e.type) };
  }
}
