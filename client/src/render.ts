// Canvas hex renderer.
//
// Single source of truth for visual mappings: ownership colors, progress
// fills, POI icons. Drawing reads from a WorldStore snapshot; mutations
// only happen via WS callbacks (the render loop is read-only).

import { axialToPixel, hexCorners, type Coord, type Layout } from "./hex";
import type { CellState, PoiState, WorldStore } from "./state";

const COLOR = {
  bg: "#07090d",
  se: "#1f4d80",
  seBorder: "#4aa3ff",
  enemy: "#7a1f1f",
  enemyBorder: "#ff5252",
  contested: "#3a3320",
  contestedBorder: "#f0c43a",
  capital: "#ffffff",
  selection: "#ffffff",
  text: "#d8dde6",
  pressure: "#4aa3ff",
  resistance: "#ff5252",
};

const POI_GLYPH: Record<PoiState["kind"], string> = {
  fob: "F",
  artillery: "A",
  fortress: "X",
  resistance_node: "n",
};

const POI_RADIUS_PARAM: Record<PoiState["kind"], string> = {
  fob: "fob_radius",
  artillery: "fob_radius", // arty has a single-cell effect; show small marker
  fortress: "fortress_radius",
  resistance_node: "node_radius",
};

export class Renderer {
  private ctx: CanvasRenderingContext2D;
  private layout: Layout = { size: 28, origin: { x: 0, y: 0 } };
  private layoutDirty = true;   // set when canvas resized or snapshot first arrives

  constructor(private canvas: HTMLCanvasElement, private store: WorldStore) {
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("2d context unavailable");
    this.ctx = ctx;
    store.subscribe(() => { this.layoutDirty = true; });
  }

  start(): void {
    const tick = () => {
      this.render();
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  resize(): void {
    const dpr = window.devicePixelRatio || 1;
    const w = this.canvas.clientWidth;
    const h = this.canvas.clientHeight;
    this.canvas.width = Math.floor(w * dpr);
    this.canvas.height = Math.floor(h * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.layoutDirty = true;
  }

  pickCell(px: number, py: number): Coord | null {
    const snap = this.store.current;
    if (!snap) return null;
    // Find nearest hex by center-distance — robust against fractional rounding.
    let best: { coord: Coord; d2: number } | null = null;
    for (const c of snap.cells) {
      const center = axialToPixel([c.q, c.r], this.layout);
      const dx = center.x - px;
      const dy = center.y - py;
      const d2 = dx * dx + dy * dy;
      if (best == null || d2 < best.d2) best = { coord: [c.q, c.r], d2 };
    }
    if (best == null) return null;
    if (best.d2 > this.layout.size * this.layout.size) return null;
    return best.coord;
  }

  private recenter(): void {
    const snap = this.store.current;
    const w = this.canvas.clientWidth;
    const h = this.canvas.clientHeight;
    if (!snap || snap.cells.length === 0) {
      this.layout.origin = { x: w / 2, y: h / 2 };
      return;
    }
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    const tmp: Layout = { size: this.layout.size, origin: { x: 0, y: 0 } };
    for (const c of snap.cells) {
      const p = axialToPixel([c.q, c.r], tmp);
      if (p.x < minX) minX = p.x;
      if (p.x > maxX) maxX = p.x;
      if (p.y < minY) minY = p.y;
      if (p.y > maxY) maxY = p.y;
    }
    const gridW = maxX - minX + this.layout.size * 2;
    const gridH = maxY - minY + this.layout.size * 2;
    const scale = Math.min(w / gridW, h / gridH, 1.0);
    this.layout.size = 28 * scale;
    // Recompute extents at new size
    const tmp2: Layout = { size: this.layout.size, origin: { x: 0, y: 0 } };
    let minX2 = Infinity, maxX2 = -Infinity, minY2 = Infinity, maxY2 = -Infinity;
    for (const c of snap.cells) {
      const p = axialToPixel([c.q, c.r], tmp2);
      if (p.x < minX2) minX2 = p.x;
      if (p.x > maxX2) maxX2 = p.x;
      if (p.y < minY2) minY2 = p.y;
      if (p.y > maxY2) maxY2 = p.y;
    }
    this.layout.origin = {
      x: (w - (minX2 + maxX2)) / 2,
      y: (h - (minY2 + maxY2)) / 2,
    };
  }

  private render(): void {
    const ctx = this.ctx;
    const w = this.canvas.clientWidth;
    const h = this.canvas.clientHeight;
    ctx.fillStyle = COLOR.bg;
    ctx.fillRect(0, 0, w, h);

    const snap = this.store.current;
    if (!snap) return;

    if (this.layoutDirty) {
      this.recenter();
      this.layoutDirty = false;
    }

    // Pass 1: draw POI effect halos (under cells but visible).
    for (const poi of snap.pois) {
      const radius = (snap.params[POI_RADIUS_PARAM[poi.kind]] ?? 0) as number;
      if (radius <= 0 || poi.kind === "artillery") continue;
      this.drawHaloRing([poi.q, poi.r], radius, poi.owner === "se" ? COLOR.seBorder : COLOR.enemyBorder);
    }

    // Pass 2: draw cells.
    for (const c of snap.cells) {
      this.drawCell(c);
    }

    // Pass 3: draw POIs.
    for (const poi of snap.pois) {
      this.drawPoi(poi);
    }

    // Pass 4: selection ring on top.
    if (this.store.ui.selectedCell) {
      this.drawSelectionRing(this.store.ui.selectedCell);
    }
  }

  private drawCell(c: CellState): void {
    const ctx = this.ctx;
    const corners = hexCorners([c.q, c.r], this.layout);
    ctx.beginPath();
    for (let i = 0; i < corners.length; i++) {
      const p = corners[i];
      if (i === 0) ctx.moveTo(p.x, p.y);
      else ctx.lineTo(p.x, p.y);
    }
    ctx.closePath();

    let fill = COLOR.contested;
    let stroke = COLOR.contestedBorder;
    if (c.ownership === "se") { fill = COLOR.se; stroke = COLOR.seBorder; }
    else if (c.ownership === "enemy") { fill = COLOR.enemy; stroke = COLOR.enemyBorder; }

    ctx.fillStyle = fill;
    ctx.fill();
    ctx.lineWidth = c.is_capital ? 2.5 : 1;
    ctx.strokeStyle = c.is_capital ? COLOR.capital : stroke;
    ctx.stroke();

    // Contested cells get a horizontal progress fill (-100..+100 → red..blue).
    if (c.ownership === "contested") {
      this.drawProgressBar([c.q, c.r], c.progress, c.diver_pressure, c.enemy_resistance);
    }

    if (c.is_capital) {
      const center = axialToPixel([c.q, c.r], this.layout);
      ctx.fillStyle = COLOR.capital;
      ctx.font = `bold ${Math.round(this.layout.size * 0.55)}px sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("★", center.x, center.y);
    }
  }

  private drawProgressBar(coord: Coord, progress: number, pressure: number, resistance: number): void {
    const ctx = this.ctx;
    const center = axialToPixel(coord, this.layout);
    const barW = this.layout.size * 1.2;
    const barH = Math.max(3, this.layout.size * 0.18);
    const x = center.x - barW / 2;
    const y = center.y - barH / 2;

    // Background
    ctx.fillStyle = "#1a1f29";
    ctx.fillRect(x, y, barW, barH);

    // Progress: midpoint at center, leans left (red/enemy) or right (blue/se).
    const mid = x + barW / 2;
    const fillW = (Math.abs(progress) / 100) * (barW / 2);
    ctx.fillStyle = progress >= 0 ? COLOR.seBorder : COLOR.enemyBorder;
    if (progress >= 0) ctx.fillRect(mid, y, fillW, barH);
    else ctx.fillRect(mid - fillW, y, fillW, barH);

    // Pressure / resistance dots above the bar.
    if (pressure > 0) {
      const radius = Math.min(this.layout.size * 0.18, 4 + pressure / 80);
      ctx.beginPath();
      ctx.arc(center.x - this.layout.size * 0.35, center.y - this.layout.size * 0.45, radius, 0, Math.PI * 2);
      ctx.fillStyle = COLOR.pressure;
      ctx.fill();
    }
    if (resistance > 0) {
      const radius = Math.min(this.layout.size * 0.18, 3 + resistance / 4);
      ctx.beginPath();
      ctx.arc(center.x + this.layout.size * 0.35, center.y - this.layout.size * 0.45, radius, 0, Math.PI * 2);
      ctx.fillStyle = COLOR.resistance;
      ctx.fill();
    }
  }

  private drawPoi(poi: PoiState): void {
    const ctx = this.ctx;
    const center = axialToPixel([poi.q, poi.r], this.layout);
    const r = this.layout.size * 0.42;
    ctx.beginPath();
    ctx.arc(center.x, center.y + this.layout.size * 0.05, r, 0, Math.PI * 2);
    ctx.fillStyle = poi.owner === "se" ? "#0d1f33" : "#330d0d";
    ctx.fill();
    ctx.strokeStyle = poi.owner === "se" ? COLOR.seBorder : COLOR.enemyBorder;
    ctx.lineWidth = 1.5;
    ctx.stroke();
    ctx.fillStyle = poi.owner === "se" ? COLOR.seBorder : COLOR.enemyBorder;
    ctx.font = `bold ${Math.round(this.layout.size * 0.5)}px sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(POI_GLYPH[poi.kind], center.x, center.y + this.layout.size * 0.05);

    if (poi.kind === "artillery") {
      const target = (poi.state.target as [number, number] | null | undefined);
      if (target) {
        const tCenter = axialToPixel([target[0], target[1]], this.layout);
        ctx.strokeStyle = COLOR.seBorder;
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 3]);
        ctx.beginPath();
        ctx.moveTo(center.x, center.y);
        ctx.lineTo(tCenter.x, tCenter.y);
        ctx.stroke();
        ctx.setLineDash([]);
      }
    }
  }

  private drawHaloRing(coord: Coord, radius: number, color: string): void {
    // Render a soft halo of pixels within the hex radius.
    const snap = this.store.current;
    if (!snap) return;
    const ctx = this.ctx;
    ctx.fillStyle = color;
    ctx.globalAlpha = 0.06;
    for (const c of snap.cells) {
      const d = (Math.abs(coord[0] - c.q) + Math.abs(coord[0] + coord[1] - c.q - c.r) + Math.abs(coord[1] - c.r)) / 2;
      if (d > radius || d === 0) continue;
      const corners = hexCorners([c.q, c.r], this.layout);
      ctx.beginPath();
      for (let i = 0; i < corners.length; i++) {
        const p = corners[i];
        if (i === 0) ctx.moveTo(p.x, p.y);
        else ctx.lineTo(p.x, p.y);
      }
      ctx.closePath();
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  private drawSelectionRing(coord: Coord): void {
    const ctx = this.ctx;
    const corners = hexCorners(coord, this.layout);
    ctx.beginPath();
    for (let i = 0; i < corners.length; i++) {
      const p = corners[i];
      if (i === 0) ctx.moveTo(p.x, p.y);
      else ctx.lineTo(p.x, p.y);
    }
    ctx.closePath();
    ctx.lineWidth = 3;
    ctx.strokeStyle = COLOR.selection;
    ctx.stroke();
  }
}
