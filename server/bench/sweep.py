"""Parallel parameter sweep over the headless match runner.

Runs N matches per parameter value across a process pool, then prints a
summary table and writes a JSON dump. Generic over which param is being
swept — pass it as ``--param`` / ``--values`` and the sweep handles the
rest.

Each (param_value, match_index) pair gets a deterministic seed so reruns
are reproducible. Workers pin their own ``random`` state per match via
``run_match``.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any

from .headless import run_match


def _coerce(v: str) -> Any:
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v


def _job(args: tuple[str, Any, int, int, str, int, float]) -> dict:
    param_name, param_value, match_idx, seed, scenario, max_ticks, speed = args
    overrides = {param_name: param_value}
    result = run_match(
        seed=seed,
        params_override=overrides,
        scenario=scenario,
        max_ticks=max_ticks,
        speed=speed,
    )
    result["_param_name"] = param_name
    result["_param_value"] = param_value
    result["_match_idx"] = match_idx
    return result


def _summarize(results: list[dict]) -> dict:
    def mean(xs: list[float]) -> float:
        return round(statistics.mean(xs), 3) if xs else 0.0

    def pmean(xs: list[float]) -> float:
        return round(statistics.mean(xs), 2) if xs else 0.0

    end_states = [r["end_state"] for r in results]
    return {
        "n": len(results),
        "se_won": sum(1 for s in end_states if s == "se_won"),
        "enemy_won": sum(1 for s in end_states if s == "enemy_won"),
        "running": sum(1 for s in end_states if s == "running"),  # hit max_ticks
        "mean_conquer": mean([r["salients_conquer"] for r in results]),
        "mean_destroy": mean([r["salients_destroy"] for r in results]),
        "mean_peak_gauge": pmean([r["peak_retaliation_gauge"] for r in results]),
        "mean_match_ticks": int(mean([r["final_tick"] for r in results])),
        "mean_se_pct": pmean([r["final_stats"]["se_pct"] for r in results]),
        "mean_factory_strikes": mean([r["factory_strikes"] for r in results]),
        "mean_repulses": mean([r["repulses"] for r in results]),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Parallel param sweep over headless matches.")
    ap.add_argument("--param", required=True, help="param name to sweep, e.g. retaliation_gauge_threshold")
    ap.add_argument("--values", required=True, help="comma-separated values, e.g. 50,30,20,15,10")
    ap.add_argument("--matches-per-value", type=int, default=20)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--scenario", default="demo_planet")
    ap.add_argument("--max-ticks", type=int, default=600)
    ap.add_argument("--speed", type=float, default=10.0)
    ap.add_argument("--seed-base", type=int, default=1000)
    ap.add_argument("--out", default="server/bench/sweep_results.json")
    args = ap.parse_args()

    values = [_coerce(v.strip()) for v in args.values.split(",")]
    jobs: list[tuple[str, Any, int, int, str, int, float]] = []
    for vi, value in enumerate(values):
        for mi in range(args.matches_per_value):
            seed = args.seed_base + vi * 10_000 + mi
            jobs.append((args.param, value, mi, seed, args.scenario, args.max_ticks, args.speed))

    print(f"Running {len(jobs)} matches across {args.workers} workers "
          f"({len(values)} values × {args.matches_per_value} matches each)...")
    t0 = time.time()
    results: list[dict] = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(_job, j) for j in jobs]
        for f in as_completed(futures):
            results.append(f.result())
    elapsed = time.time() - t0
    print(f"Done in {elapsed:.1f}s real time ({elapsed / len(jobs):.2f}s per match equivalent serial).")

    by_value: dict[Any, list[dict]] = {}
    for r in results:
        by_value.setdefault(r["_param_value"], []).append(r)

    summaries = {}
    print()
    header = f"{args.param:>30} | {'n':>3} | {'se_won':>6} | {'running':>7} | "\
            f"{'conquer':>7} | {'destroy':>7} | {'peak_gauge':>10} | "\
            f"{'ticks':>5} | {'se_pct':>6} | {'fac_strikes':>11} | {'repulses':>8}"
    print(header)
    print("-" * len(header))
    for value in values:
        rs = by_value.get(value, [])
        s = _summarize(rs)
        summaries[str(value)] = s
        print(f"{str(value):>30} | {s['n']:>3} | {s['se_won']:>6} | {s['running']:>7} | "
              f"{s['mean_conquer']:>7} | {s['mean_destroy']:>7} | {s['mean_peak_gauge']:>10} | "
              f"{s['mean_match_ticks']:>5} | {s['mean_se_pct']:>6} | {s['mean_factory_strikes']:>11} | {s['mean_repulses']:>8}")

    with open(args.out, "w") as f:
        json.dump({
            "param": args.param,
            "values": values,
            "matches_per_value": args.matches_per_value,
            "summaries": summaries,
            "all_results": results,
        }, f, indent=2, default=str)
    print(f"\nFull results in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
