"""FastAPI entrypoint: wires the sim, REST endpoints, and WebSocket fan-out."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.controls import make_router as make_control_router
from .api.serialize import world_to_wire
from .api.ws import Hub, make_router as make_ws_router
from .sim.scenarios import load_scenario
from .sim.world import World


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def _build_app() -> FastAPI:
    world = World()
    load_scenario(world, os.environ.get("HEXA_SCENARIO", "demo_planet"))
    world_ref: dict[str, World] = {"world": world}

    hub = Hub()
    sim_task: asyncio.Task | None = None

    async def sim_loop() -> None:
        try:
            while True:
                w = world_ref["world"]
                w.step()
                await hub.broadcast(world_to_wire(w))
                await asyncio.sleep(1.0 / w.params.tick_hz)
        except asyncio.CancelledError:
            return

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal sim_task
        sim_task = asyncio.create_task(sim_loop())
        try:
            yield
        finally:
            if sim_task:
                sim_task.cancel()
                try:
                    await sim_task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(title="Hexa War Sim", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(make_control_router(world_ref))
    app.include_router(make_ws_router(world_ref, hub))

    @app.get("/state")
    def get_state() -> dict:
        return world_to_wire(world_ref["world"])

    @app.get("/healthz")
    def healthz() -> dict:
        w = world_ref["world"]
        return {"ok": True, "tick": w.tick, "match_state": w.match_state}

    return app


app = _build_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server.main:app",
        host=os.environ.get("HEXA_HOST", "0.0.0.0"),
        port=int(os.environ.get("HEXA_PORT", "8800")),
        reload=False,
    )
