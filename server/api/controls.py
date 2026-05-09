"""Controller REST endpoints — POSTs that mutate world state."""
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..sim.cell import Ownership
from ..sim.scenarios import list_scenarios, load_scenario
from ..sim.world import World


class SimAction(BaseModel):
    action: str = Field(..., pattern="^(start|pause|reset)$")
    speed: float | None = None


class PressurePayload(BaseModel):
    coord: tuple[int, int]
    pressure: float


class PlacePoiPayload(BaseModel):
    kind: str
    owner: str
    coord: tuple[int, int]


class RemovePoiPayload(BaseModel):
    id: str


class FireArtyPayload(BaseModel):
    poi_id: str
    target: tuple[int, int]


class ParamsPayload(BaseModel):
    params: dict[str, Any]


class ScenarioPayload(BaseModel):
    name: str


def make_router(world_ref: dict[str, World]) -> APIRouter:
    """``world_ref`` holds the live World under key ``"world"`` so endpoints
    can pick up the current instance even after a reset."""

    router = APIRouter(prefix="/control", tags=["control"])

    def w() -> World:
        return world_ref["world"]

    @router.post("/sim")
    def sim_action(payload: SimAction) -> dict:
        world = w()
        if payload.action == "start":
            if world.match_state in ("se_won", "enemy_won"):
                raise HTTPException(409, "Match has ended; reset before starting.")
            world.match_state = "running"
        elif payload.action == "pause":
            if world.match_state == "running":
                world.match_state = "paused"
        elif payload.action == "reset":
            world.reset_match()
        if payload.speed is not None:
            world.speed = max(0.1, min(10.0, payload.speed))
        return {"ok": True, "match_state": world.match_state, "speed": world.speed}

    @router.post("/pressure")
    def set_pressure(payload: PressurePayload) -> dict:
        ok = w().set_pressure(payload.coord, payload.pressure)
        if not ok:
            raise HTTPException(400, "Cell not contested or out of bounds.")
        return {"ok": True}

    @router.post("/poi/place")
    def place_poi(payload: PlacePoiPayload) -> dict:
        if payload.kind not in ("fob", "artillery", "fortress", "resistance_node"):
            raise HTTPException(400, f"Unknown POI kind {payload.kind!r}.")
        try:
            owner = Ownership(payload.owner)
        except ValueError:
            raise HTTPException(400, f"Invalid owner {payload.owner!r}.")
        # SE FOB / artillery placements route through a build site that
        # resolves after fresh_build_ticks. Fortress and resistance_node
        # (enemy infra) stay instant — the build-site abstraction is
        # specifically for SE construction.
        if payload.kind in ("fob", "artillery"):
            poi = w().place_build_site(payload.kind, owner, payload.coord)  # type: ignore[arg-type]
        else:
            poi = w().place_poi(payload.kind, owner, payload.coord)  # type: ignore[arg-type]
        if poi is None:
            raise HTTPException(400, "POI placement not allowed at this cell.")
        return {"ok": True, "poi": poi.to_wire()}

    @router.post("/poi/remove")
    def remove_poi(payload: RemovePoiPayload) -> dict:
        ok = w().remove_poi(payload.id)
        if not ok:
            raise HTTPException(404, "POI not found.")
        return {"ok": True}

    @router.post("/artillery/fire")
    def fire_artillery(payload: FireArtyPayload) -> dict:
        ok = w().fire_artillery(payload.poi_id, payload.target)
        if not ok:
            raise HTTPException(400, "Cannot fire artillery (no shells, target out of range, bad target, or wrong POI).")
        return {"ok": True}

    @router.post("/params")
    def update_params(payload: ParamsPayload) -> dict:
        w().params.update_from(payload.params)
        return {"ok": True, "params": w().params.to_dict()}

    @router.post("/scenario/load")
    def load(payload: ScenarioPayload) -> dict:
        world = w()
        load_scenario(world, payload.name)
        world.match_state = "paused"
        world.tick = 0
        world.elapsed_s = 0.0
        return {"ok": True, "scenario": payload.name}

    @router.get("/scenarios")
    def scenarios() -> dict:
        return {"scenarios": list_scenarios()}

    return router
