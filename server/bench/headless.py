"""Headless single-match runner for benchmark / parameter sweeps.

Instantiates a fresh ``World``, applies any param overrides, and steps the
sim to a terminal state without going through the FastAPI tick-loop sleep.
Returns a metrics dict per match. The full event stream is collected
tick-by-tick so the in-place 100-event buffer cap in ``World.step`` doesn't
lose data when a tick emits a burst.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from typing import Any

from ..sim.scenarios import load_scenario
from ..sim.world import World


def run_match(
    seed: int,
    params_override: dict[str, Any] | None = None,
    scenario: str = "demo_planet",
    max_ticks: int = 600,
    speed: float = 10.0,
) -> dict:
    """Run one match end-to-end. Pure function over (seed, params, speed).

    ``speed`` mirrors ``World.speed``. Default 10.0 matches the dev-workflow
    fast-forward used in UI/API runs — at this speed a typical demo_planet
    match completes in 200–500 ticks. NOTE: retaliation_gauge_decay is
    per-tick (not per-second), so changing speed changes effective
    gauge accumulation; results are only comparable within a fixed speed.
    """
    random.seed(seed)

    w = World()
    load_scenario(w, scenario)
    if params_override:
        w.params.update_from(params_override)
    w.speed = speed
    w.match_state = "running"

    all_events: list[dict] = []
    peak_gauge = 0.0

    for _ in range(max_ticks):
        if w.match_state != "running":
            break
        w.step()
        # Events emitted during the step have tick == w.tick - 1 (step
        # increments tick at the end). Pull them off the buffer before any
        # future trim can evict them.
        target_tick = w.tick - 1
        for ev in w.match_events:
            if ev["tick"] == target_tick:
                all_events.append(ev)
        if w.retaliation_gauge > peak_gauge:
            peak_gauge = w.retaliation_gauge

    captures = [e for e in all_events if e["type"] == "cell_captured"]
    repulses = [e for e in all_events if e["type"] == "cell_repulsed"]
    salients_spawned = [e for e in all_events if e["type"] == "salient_spawned"]
    salients_ended = [e for e in all_events if e["type"] == "salient_ended"]
    factory_strikes = [e for e in all_events if e["type"] == "factory_strike"]
    builds_done = [e for e in all_events if e["type"] == "build_completed"]

    se_caps = sum(1 for e in captures if e.get("defender") == "se")
    en_caps = sum(1 for e in captures if e.get("defender") == "enemy")

    salient_kinds = Counter(e.get("kind") for e in salients_spawned)
    salient_reasons = Counter(e.get("reason") for e in salients_ended)

    return {
        "seed": seed,
        "end_state": w.match_state,
        "final_tick": w.tick,
        "final_elapsed_s": round(w.elapsed_s, 2),
        "final_stats": w.stats(),
        "se_captures": se_caps,
        "enemy_captures": en_caps,
        "breakthroughs": sum(1 for e in captures if e.get("breakthrough")),
        "captures_total": len(captures),
        "repulses": len(repulses),
        "salients_spawned_total": len(salients_spawned),
        "salients_destroy": salient_kinds.get("destroy", 0),
        "salients_conquer": salient_kinds.get("conquer", 0),
        "salients_ended": len(salients_ended),
        "salient_success": salient_reasons.get("success", 0),
        "salient_expired": salient_reasons.get("expired", 0),
        "factory_strikes": len(factory_strikes),
        "builds_completed": len(builds_done),
        "peak_retaliation_gauge": round(peak_gauge, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a single headless sim match.")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--scenario", default="demo_planet")
    ap.add_argument("--max-ticks", type=int, default=600)
    ap.add_argument("--speed", type=float, default=10.0)
    ap.add_argument(
        "--param", action="append", default=[],
        help="Param override as name=value, repeatable. Example: --param retaliation_gauge_threshold=20",
    )
    args = ap.parse_args()

    overrides: dict[str, Any] = {}
    for kv in args.param:
        k, _, v = kv.partition("=")
        try:
            v_parsed: Any = int(v)
        except ValueError:
            try:
                v_parsed = float(v)
            except ValueError:
                v_parsed = v
        overrides[k] = v_parsed

    result = run_match(
        seed=args.seed,
        params_override=overrides or None,
        scenario=args.scenario,
        max_ticks=args.max_ticks,
        speed=args.speed,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
