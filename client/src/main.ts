import { Controls } from "./controls";
import { Renderer } from "./render";
import { connectStream, WorldStore } from "./state";

const canvas = document.getElementById("canvas") as HTMLCanvasElement;
const store = new WorldStore();
const controls = new Controls(store);
const renderer = new Renderer(canvas, store);

controls.init();
renderer.start();

const onResize = () => renderer.resize();
window.addEventListener("resize", onResize);

canvas.addEventListener("click", (ev) => {
  const rect = canvas.getBoundingClientRect();
  const x = ev.clientX - rect.left;
  const y = ev.clientY - rect.top;
  const coord = renderer.pickCell(x, y);
  controls.selectCell(coord);
});

connectStream(store);
store.subscribe(() => {
  // First snapshot triggers a layout pass.
  if (canvas.width === 0) renderer.resize();
});

// Initial sizing
requestAnimationFrame(onResize);
