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
