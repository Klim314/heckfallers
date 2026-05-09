"""WebSocket fan-out: pushes world snapshots to every connected client.

The server holds a single set of subscribers; when the sim ticks it
broadcasts the serialized snapshot to all of them. Clients send no
messages on this channel for v0 — controller actions go through REST.
"""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .serialize import world_to_wire

if TYPE_CHECKING:
    from ..sim.world import World


class Hub:
    def __init__(self) -> None:
        self._subscribers: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def add(self, ws: WebSocket) -> None:
        async with self._lock:
            self._subscribers.add(ws)

    async def remove(self, ws: WebSocket) -> None:
        async with self._lock:
            self._subscribers.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        if not self._subscribers:
            return
        text = json.dumps(payload)
        async with self._lock:
            stale: list[WebSocket] = []
            for ws in self._subscribers:
                try:
                    await ws.send_text(text)
                except Exception:
                    stale.append(ws)
            for ws in stale:
                self._subscribers.discard(ws)


def make_router(world_ref: dict[str, "World"], hub: Hub) -> APIRouter:
    router = APIRouter()

    @router.websocket("/stream")
    async def stream(ws: WebSocket) -> None:
        await ws.accept()
        await hub.add(ws)
        # Send initial snapshot immediately so the client can render before
        # the next tick fires.
        try:
            await ws.send_text(json.dumps(world_to_wire(world_ref["world"])))
            while True:
                # Drain anything the client sends to detect disconnects;
                # we don't act on inbound messages in v0.
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await hub.remove(ws)

    return router
