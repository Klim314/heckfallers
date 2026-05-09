// Pointy-top axial coordinates. Server is the source of truth for hex
// math; the client mirrors the parts it needs for rendering and click
// hit-testing.

export type Coord = readonly [number, number]; // (q, r)

export const NEIGHBOR_DIRS: ReadonlyArray<Coord> = [
  [+1, 0],
  [-1, 0],
  [0, +1],
  [0, -1],
  [+1, -1],
  [-1, +1],
];

export interface Layout {
  size: number;       // hex "size" (center-to-vertex distance)
  origin: { x: number; y: number };
}

export function axialToPixel(coord: Coord, layout: Layout): { x: number; y: number } {
  const [q, r] = coord;
  const x = layout.size * Math.sqrt(3) * (q + r / 2);
  const y = layout.size * 1.5 * r;
  return { x: x + layout.origin.x, y: y + layout.origin.y };
}

export function pixelToAxial(px: number, py: number, layout: Layout): Coord {
  const x = (px - layout.origin.x) / layout.size;
  const y = (py - layout.origin.y) / layout.size;
  const qFrac = (Math.sqrt(3) / 3) * x - (1 / 3) * y;
  const rFrac = (2 / 3) * y;
  return cubeRound(qFrac, rFrac);
}

function cubeRound(qFrac: number, rFrac: number): Coord {
  const xFrac = qFrac;
  const zFrac = rFrac;
  const yFrac = -xFrac - zFrac;
  let rx = Math.round(xFrac);
  let ry = Math.round(yFrac);
  let rz = Math.round(zFrac);
  const dx = Math.abs(rx - xFrac);
  const dy = Math.abs(ry - yFrac);
  const dz = Math.abs(rz - zFrac);
  if (dx > dy && dx > dz) rx = -ry - rz;
  else if (dy > dz) ry = -rx - rz;
  else rz = -rx - ry;
  return [rx, rz];
}

export function hexCorners(coord: Coord, layout: Layout): Array<{ x: number; y: number }> {
  const center = axialToPixel(coord, layout);
  const corners: Array<{ x: number; y: number }> = [];
  for (let i = 0; i < 6; i++) {
    const angle = (Math.PI / 180) * (60 * i - 30); // pointy-top
    corners.push({
      x: center.x + layout.size * Math.cos(angle),
      y: center.y + layout.size * Math.sin(angle),
    });
  }
  return corners;
}

export function distance(a: Coord, b: Coord): number {
  return (Math.abs(a[0] - b[0]) + Math.abs(a[0] + a[1] - b[0] - b[1]) + Math.abs(a[1] - b[1])) / 2;
}

export function coordKey(c: Coord): string {
  return `${c[0]},${c[1]}`;
}
