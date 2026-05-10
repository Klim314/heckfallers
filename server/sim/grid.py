"""Axial-coordinate hex grid utilities.

Coordinates are (q, r) integer pairs. The six neighbor directions are
fixed; pointy-top orientation is assumed but the server never needs to
reason about pixels — that's the client's job.
"""
from __future__ import annotations

Coord = tuple[int, int]

NEIGHBOR_DIRS: tuple[Coord, ...] = (
    (+1, 0),
    (-1, 0),
    (0, +1),
    (0, -1),
    (+1, -1),
    (-1, +1),
)


def neighbors(coord: Coord) -> list[Coord]:
    q, r = coord
    return [(q + dq, r + dr) for dq, dr in NEIGHBOR_DIRS]


def distance(a: Coord, b: Coord) -> int:
    aq, ar = a
    bq, br = b
    return (abs(aq - bq) + abs(aq + ar - bq - br) + abs(ar - br)) // 2


def cells_within(center: Coord, radius: int) -> list[Coord]:
    cq, cr = center
    out: list[Coord] = []
    for dq in range(-radius, radius + 1):
        for dr in range(max(-radius, -dq - radius), min(radius, -dq + radius) + 1):
            out.append((cq + dq, cr + dr))
    return out


def _axial_to_cube(c: Coord) -> tuple[int, int, int]:
    q, r = c
    return (q, -q - r, r)


def forward_hemisphere(axis: Coord) -> tuple[Coord, ...]:
    """Return the NEIGHBOR_DIRS in the forward hemisphere of ``axis``.

    Forward = non-negative dot product against ``axis`` in cube coords.
    For a "pure" hex axis (a multiple of one NEIGHBOR_DIR) this returns
    exactly 3 directions. For a diagonal axis (sum of two NEIGHBOR_DIRs),
    ties on dot==0 may include a 4th — clamp to the top 3 by dot value
    so the wedge never widens beyond a half-disk.
    """
    ax_cube = _axial_to_cube(axis)
    scored: list[tuple[int, Coord]] = []
    for d in NEIGHBOR_DIRS:
        d_cube = _axial_to_cube(d)
        dot = sum(a * b for a, b in zip(ax_cube, d_cube))
        if dot >= 0:
            scored.append((dot, d))
    if len(scored) > 3:
        # Sort by dot desc; deterministic tiebreak via NEIGHBOR_DIRS order
        # (stable sort preserves NEIGHBOR_DIRS insertion order on ties).
        scored.sort(key=lambda x: -x[0])
        scored = scored[:3]
    return tuple(d for _, d in scored)
