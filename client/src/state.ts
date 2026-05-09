// World state mirrored from the server's WebSocket stream.

import type { Coord } from "./hex";

export type Faction = "se" | "enemy";

export interface CellState {
  q: number;
  r: number;
  defender: Faction;
  attacker: Faction | null;
  progress: number;
  diver_pressure: number;
  diver_pin: boolean;
  enemy_resistance: number;
  is_capital: boolean;
  enemy_supply: number;
  se_supply: number;
  supply_shock_until: number;
  active_until_tick: number;
}

export type PoiKind =
  | "fob"
  | "artillery"
  | "fortress"
  | "resistance_node"
  | "build_site"
  | "factory";

export interface PoiState {
  id: string;
  kind: PoiKind;
  owner: "se" | "enemy";
  q: number;
  r: number;
  // build_site state carries `target_kind: PoiKind` and `completes_at: number`.
  // artillery state carries `shells`, `target`, `expires_at`.
  state: Record<string, unknown>;
}

export interface SalientState {
  id: string;
  kind: "destroy" | "conquer";
  corridor: [number, number][];
  target: [number, number] | null;
  target_poi_id: string;
  spawned_tick: number;
  expires_tick: number;
  region?: [number, number][];
}

// Match events are append-only on the server (capped to ~100 entries).
// A `type` discriminator narrows the payload; unknown types render with a
// generic fallback so the client doesn't break when a new event ships.
export type MatchEventType =
  | "cell_captured"
  | "cell_repulsed"
  | "salient_spawned"
  | "salient_ended"
  | "build_started"
  | "build_completed"
  | "poi_placed"
  | "factory_strike"
  | "match_ended";

export interface MatchEvent {
  type: MatchEventType | string;
  tick: number;
  [key: string]: unknown;
}

export interface WorldSnapshot {
  tick: number;
  elapsed_s: number;
  match_state: "running" | "paused" | "se_won" | "enemy_won";
  speed: number;
  scenario_name: string;
  params: Record<string, number>;
  stats: { total: number; se: number; enemy: number; contested: number; se_pct: number; enemy_pct: number };
  cells: CellState[];
  pois: PoiState[];
  salients: SalientState[];
  controller: string;
  se_controller: string;
  requisition: number;
  match_events: MatchEvent[];
}

export interface UiState {
  selectedCell: Coord | null;
  selectedPoiId: string | null;
  hoverCell: Coord | null;
}

type Listener = (snapshot: WorldSnapshot) => void;

export class WorldStore {
  private snapshot: WorldSnapshot | null = null;
  private listeners: Set<Listener> = new Set();
  ui: UiState = { selectedCell: null, selectedPoiId: null, hoverCell: null };

  get current(): WorldSnapshot | null {
    return this.snapshot;
  }

  set(snapshot: WorldSnapshot): void {
    this.snapshot = snapshot;
    for (const l of this.listeners) l(snapshot);
  }

  subscribe(l: Listener): () => void {
    this.listeners.add(l);
    if (this.snapshot) l(this.snapshot);
    return () => this.listeners.delete(l);
  }

  cellAt(coord: Coord): CellState | undefined {
    return this.snapshot?.cells.find((c) => c.q === coord[0] && c.r === coord[1]);
  }

  poiAt(coord: Coord): PoiState | undefined {
    return this.snapshot?.pois.find((p) => p.q === coord[0] && p.r === coord[1]);
  }
}

export function connectStream(store: WorldStore, url = "/stream"): WebSocket {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}${url}`);
  ws.onmessage = (ev) => {
    try {
      const snapshot = JSON.parse(ev.data) as WorldSnapshot;
      store.set(snapshot);
    } catch (err) {
      console.warn("bad ws payload", err);
    }
  };
  ws.onopen = () => console.log("[ws] connected");
  ws.onclose = () => {
    console.log("[ws] disconnected; retrying in 1s");
    setTimeout(() => connectStream(store, url), 1000);
  };
  ws.onerror = (err) => console.warn("[ws] error", err);
  return ws;
}

export async function postControl(path: string, body: unknown): Promise<unknown> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${path} -> ${res.status}: ${text}`);
  }
  return res.json();
}
