// Canvas hex renderer.
//
// Single source of truth for visual mappings: ownership colors, progress
// fills, POI icons. Drawing reads from a WorldStore snapshot; mutations
// only happen via WS callbacks (the render loop is read-only).

import { axialToPixel, hexCorners, type Coord, type Layout } from "./hex";
import type { CellState, PoiKind, PoiState, SalientState, WorldStore } from "./state";

const COLOR = {
  bg: "#07090d",
  area: "#0c1018",
  areaBorder: "#1d2733",
  se: "#1f4d80",
  seBorder: "#4aa3ff",
  enemy: "#7a1f1f",
  enemyBorder: "#ff5252",
  contestedBorder: "#f0c43a",
  contestedBrewing: "#7a6420",
  capital: "#ffffff",
  selection: "#ffffff",
  text: "#d8dde6",
  pressure: "#4aa3ff",
  resistance: "#ff5252",
  salient: "#ff3838",
};

const POI_GLYPH: Record<PoiKind, string> = {
  fob: "F",
  artillery: "A",
  fortress: "X",
  resistance_node: "n",
  build_site: "?",   // overridden at draw time by state.target_kind glyph
  factory: "f",
};

const POI_RADIUS_PARAM: Record<PoiKind, string> = {
  fob: "fob_radius",
  artillery: "arty_range",
  fortress: "fortress_radius",
  resistance_node: "node_radius",
  build_site: "",   // no halo for pending sites — skipped in the halo pass
  factory: "factory_radius",
};

interface AreaCircle {
  cx: number;
  cy: number;
  radius: number;
}

export class Renderer {
  private ctx: CanvasRenderingContext2D;
  private layout: Layout = { size: 28, origin: { x: 0, y: 0 } };
  private layoutDirty = true;   // set when canvas resized or snapshot first arrives
  private area: AreaCircle | null = null;

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
      this.area = null;
      return;
    }

    // Fit using cell vertices (not just centers) so the circumscribing
    // boundary circle stays inside the canvas.
    const baseSize = 28;
    const tmp: Layout = { size: baseSize, origin: { x: 0, y: 0 } };
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const c of snap.cells) {
      for (const p of hexCorners([c.q, c.r], tmp)) {
        if (p.x < minX) minX = p.x;
        if (p.x > maxX) maxX = p.x;
        if (p.y < minY) minY = p.y;
        if (p.y > maxY) maxY = p.y;
      }
    }
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;

    // Smallest enclosing circle — approximated by max vertex distance
    // from the bbox centroid. Tight enough for symmetric hex layouts.
    let maxR = 0;
    for (const c of snap.cells) {
      for (const p of hexCorners([c.q, c.r], tmp)) {
        const r = Math.hypot(p.x - cx, p.y - cy);
        if (r > maxR) maxR = r;
      }
    }

    const margin = 12;
    const areaPad = 1.1;            // boundary circle is ~10% larger than the hex
    const paddedR = maxR * areaPad;
    const scale = Math.min(
      (w - margin * 2) / (2 * paddedR),
      (h - margin * 2) / (2 * paddedR),
      1.0,
    );
    this.layout.size = baseSize * scale;
    this.layout.origin = { x: w / 2 - cx * scale, y: h / 2 - cy * scale };
    this.area = { cx: w / 2, cy: h / 2, radius: paddedR * scale };
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

    // Pass 0: draw the area-of-operation circle behind everything.
    if (this.area) {
      ctx.beginPath();
      ctx.arc(this.area.cx, this.area.cy, this.area.radius, 0, Math.PI * 2);
      ctx.fillStyle = COLOR.area;
      ctx.fill();
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = COLOR.areaBorder;
      ctx.stroke();
    }

    // Pass 1: draw POI effect halos (under cells but visible). Build sites
    // contribute no buff so they get no halo; artillery now has a real
    // arty_range firing gate so its range cone is meaningful to render.
    for (const poi of snap.pois) {
      if (poi.kind === "build_site") continue;
      const param = POI_RADIUS_PARAM[poi.kind];
      const radius = (snap.params[param] ?? 0) as number;
      if (radius <= 0) continue;
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

    // Pass 3.5: salient overlays (after cells/POIs so the arrow sits on top).
    for (const s of snap.salients) {
      this.drawSalient(s);
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

    // Fill reflects the defender. The border tells a 3-state story:
    //   held    -> defender stroke
    //   brewing -> dim yellow dashed (contested but the attacker isn't
    //              currently winning ground; signals "fight here, may pile on")
    //   active  -> bold yellow (sim has stamped active_until_tick because
    //              the attacker recently held > epsilon of progress)
    const fill = c.defender === "se" ? COLOR.se : COLOR.enemy;
    const factionStroke = c.defender === "se" ? COLOR.seBorder : COLOR.enemyBorder;
    const isContested = c.attacker !== null;
    const tick = this.store.current?.tick ?? 0;
    const isActive = isContested && tick < c.active_until_tick;

    ctx.fillStyle = fill;
    ctx.fill();
    if (c.is_capital) {
      ctx.lineWidth = 2.5;
      ctx.strokeStyle = COLOR.capital;
      ctx.setLineDash([]);
      ctx.stroke();
    } else if (isActive) {
      ctx.lineWidth = 2;
      ctx.strokeStyle = COLOR.contestedBorder;
      ctx.setLineDash([]);
      ctx.stroke();
    } else if (isContested) {
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = COLOR.contestedBrewing;
      ctx.setLineDash([4, 3]);
      ctx.stroke();
      ctx.setLineDash([]);
    } else {
      ctx.lineWidth = 1;
      ctx.strokeStyle = factionStroke;
      ctx.setLineDash([]);
      ctx.stroke();
    }

    if (isContested) {
      this.drawProgressBar([c.q, c.r], c.progress, c.diver_pressure, c.enemy_resistance);
    }

    if (c.diver_pin) {
      const center = axialToPixel([c.q, c.r], this.layout);
      ctx.fillStyle = COLOR.capital;
      ctx.beginPath();
      ctx.arc(center.x, center.y - this.layout.size * 0.55, this.layout.size * 0.1, 0, Math.PI * 2);
      ctx.fill();
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
    if (poi.kind === "build_site") {
      this.drawBuildSite(poi);
      return;
    }
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

    if (poi.kind === "factory") {
      const targets = (poi.state.active_targets as [number, number][] | undefined) ?? [];
      if (targets.length > 0) {
        ctx.save();
        ctx.strokeStyle = COLOR.enemyBorder;
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 3]);
        for (const [tq, tr] of targets) {
          const tCenter = axialToPixel([tq, tr], this.layout);
          ctx.beginPath();
          ctx.moveTo(center.x, center.y);
          ctx.lineTo(tCenter.x, tCenter.y);
          ctx.stroke();
        }
        ctx.restore();
      }
    }
  }

  private drawBuildSite(poi: PoiState): void {
    const ctx = this.ctx;
    const center = axialToPixel([poi.q, poi.r], this.layout);
    const r = this.layout.size * 0.42;
    const targetKind = poi.state.target_kind as PoiKind | undefined;
    const completesAt = (poi.state.completes_at as number | undefined) ?? 0;
    const tick = this.store.current?.tick ?? 0;
    const tickHz = (this.store.current?.params.tick_hz ?? 5) as number;
    const remaining = Math.max(0, completesAt - tick);
    const stroke = poi.owner === "se" ? COLOR.seBorder : COLOR.enemyBorder;

    // Translucent dashed circle conveys "pending / under construction".
    ctx.save();
    ctx.beginPath();
    ctx.arc(center.x, center.y + this.layout.size * 0.05, r, 0, Math.PI * 2);
    ctx.fillStyle = poi.owner === "se" ? "#0d1f33" : "#330d0d";
    ctx.globalAlpha = 0.45;
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.strokeStyle = stroke;
    ctx.lineWidth = 1.5;
    ctx.setLineDash([4, 3]);
    ctx.stroke();
    ctx.setLineDash([]);

    // Dimmed glyph of the *target* kind so the player can read what's
    // being built without knowing the build_site abstraction.
    const glyph = targetKind ? POI_GLYPH[targetKind] : POI_GLYPH.build_site;
    ctx.fillStyle = stroke;
    ctx.globalAlpha = 0.65;
    ctx.font = `bold ${Math.round(this.layout.size * 0.5)}px sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(glyph, center.x, center.y + this.layout.size * 0.05);
    ctx.globalAlpha = 1;

    // Countdown above the glyph in seconds.
    if (remaining > 0) {
      const seconds = Math.ceil(remaining / tickHz);
      ctx.fillStyle = COLOR.text;
      ctx.font = `${Math.round(this.layout.size * 0.32)}px sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(`${seconds}s`, center.x, center.y - this.layout.size * 0.55);
    }
    ctx.restore();
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

  private drawSalient(s: SalientState): void {
    const ctx = this.ctx;
    if (s.corridor.length < 2) return;

    // Faint cell-shaped overlay along the corridor so the path reads even
    // when the arrow sits over hex centers.
    ctx.save();
    ctx.fillStyle = COLOR.salient;
    ctx.globalAlpha = 0.08;
    for (const [q, r] of s.corridor) {
      const corners = hexCorners([q, r], this.layout);
      ctx.beginPath();
      for (let i = 0; i < corners.length; i++) {
        const p = corners[i];
        if (i === 0) ctx.moveTo(p.x, p.y);
        else ctx.lineTo(p.x, p.y);
      }
      ctx.closePath();
      ctx.fill();
    }
    ctx.restore();

    // Polyline through corridor cell centers.
    const points = s.corridor.map(([q, r]) => axialToPixel([q, r], this.layout));

    // Animate dash offset for a "marching ants" feel toward the target.
    const dashOffset = -(performance.now() / 60) % 12;

    ctx.save();
    ctx.strokeStyle = COLOR.salient;
    ctx.lineWidth = Math.max(2, this.layout.size * 0.12);
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.setLineDash([8, 4]);
    ctx.lineDashOffset = dashOffset;
    ctx.beginPath();
    ctx.moveTo(points[0].x, points[0].y);
    for (let i = 1; i < points.length; i++) {
      ctx.lineTo(points[i].x, points[i].y);
    }
    ctx.stroke();
    ctx.setLineDash([]);

    // Arrowhead at target.
    const last = points[points.length - 1];
    const prev = points[points.length - 2];
    const ang = Math.atan2(last.y - prev.y, last.x - prev.x);
    const head = this.layout.size * 0.45;
    ctx.fillStyle = COLOR.salient;
    ctx.beginPath();
    ctx.moveTo(last.x, last.y);
    ctx.lineTo(last.x - head * Math.cos(ang - Math.PI / 6), last.y - head * Math.sin(ang - Math.PI / 6));
    ctx.lineTo(last.x - head * Math.cos(ang + Math.PI / 6), last.y - head * Math.sin(ang + Math.PI / 6));
    ctx.closePath();
    ctx.fill();
    ctx.restore();

    // Pulsing target ring. Only destroy salients have a target; conquer
    // salients short-circuit above on the empty corridor.
    if (s.target === null) return;
    const pulse = (Math.sin(performance.now() / 250) + 1) / 2; // 0..1
    const targetCenter = axialToPixel(s.target, this.layout);
    const baseR = this.layout.size * 0.7;
    ctx.save();
    ctx.strokeStyle = COLOR.salient;
    ctx.lineWidth = 2 + pulse * 2;
    ctx.globalAlpha = 0.6 + pulse * 0.4;
    ctx.beginPath();
    ctx.arc(targetCenter.x, targetCenter.y, baseR + pulse * this.layout.size * 0.15, 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
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
