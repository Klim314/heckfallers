import { Controls } from "./controls";
import { EventsPanel } from "./events_panel";
import { Renderer } from "./render";
import { connectStream, WorldStore } from "./state";

const canvas = document.getElementById("canvas") as HTMLCanvasElement;
const store = new WorldStore();
const controls = new Controls(store);
const renderer = new Renderer(canvas, store);
const events = new EventsPanel(store);

controls.init();
events.init();
renderer.start();

// Salient hover tooltip — created here so the rest of the app stays unaware.
const tooltip = document.createElement("div");
tooltip.id = "salient-tooltip";
tooltip.classList.add("hidden");
(document.getElementById("map") ?? document.body).appendChild(tooltip);

const onResize = () => renderer.resize();
window.addEventListener("resize", onResize);

canvas.addEventListener("click", (ev) => {
  const rect = canvas.getBoundingClientRect();
  const x = ev.clientX - rect.left;
  const y = ev.clientY - rect.top;
  const coord = renderer.pickCell(x, y);
  controls.selectCell(coord);
});

canvas.addEventListener("mousemove", (ev) => {
  const rect = canvas.getBoundingClientRect();
  const x = ev.clientX - rect.left;
  const y = ev.clientY - rect.top;
  const coord = renderer.pickCell(x, y);
  store.ui.hoverCell = coord;

  const snap = store.current;
  if (!snap || !coord) {
    tooltip.classList.add("hidden");
    return;
  }
  // Find any salient whose corridor or target includes this cell. Conquer
  // salients have target=null and an empty corridor, so they're skipped.
  const hit = snap.salients.find((s) => {
    if (s.target && s.target[0] === coord[0] && s.target[1] === coord[1]) return true;
    return s.corridor.some(([q, r]) => q === coord[0] && r === coord[1]);
  });
  if (!hit) {
    tooltip.classList.add("hidden");
    return;
  }
  const targetPoi = snap.pois.find((p) => p.id === hit.target_poi_id);
  const targetKind = targetPoi ? targetPoi.kind.toUpperCase() : "?";
  const tickHz = (snap.params["tick_hz"] ?? 5) as number;
  const etaSec = Math.max(0, (hit.expires_tick - snap.tick) / tickHz);
  tooltip.innerHTML = `<div class="title">Strike incoming</div>Target: ${targetKind}\nETA: ${etaSec.toFixed(1)}s\nCorridor: ${hit.corridor.length} hops`;
  // Position relative to map container (tooltip is appended there).
  tooltip.style.left = `${ev.clientX - rect.left + 14}px`;
  tooltip.style.top = `${ev.clientY - rect.top + 14}px`;
  tooltip.classList.remove("hidden");
});

canvas.addEventListener("mouseleave", () => {
  store.ui.hoverCell = null;
  tooltip.classList.add("hidden");
});

connectStream(store);
store.subscribe(() => {
  // First snapshot triggers a layout pass.
  if (canvas.width === 0) renderer.resize();
});

// Initial sizing
requestAnimationFrame(onResize);
