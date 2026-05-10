"""Parallel parameter sweep over the headless match runner.

Runs N matches per parameter value across a process pool, then prints a
side-by-side comparison table and writes a JSON dump with full per-match
results. Generic over which param is being swept.

Each (param_value, match_index) pair gets a deterministic seed so reruns
are reproducible. Workers pin their own ``random`` state per match via
``run_match``.

The summary borrows ``summarize_results`` from ``headless.py`` so the
trajectory-shape and conquer-impact metrics surface in sweep output too.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any

from .headless import format_summary, run_match, summarize_results


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


def _table_row(value: Any, s: dict[str, Any]) -> str:
    """Compact one-line summary for the comparison table."""
    n = s["n"]
    w = s["wins"]
    return (
        f"{str(value):>14} | "
        f"{w['se']:>2}/{n:<2} | "
        f"{w['running']:>3} | "
        f"{s['max_swing_se_pct']['mean']:>6.1f} | "
        f"{s['max_swing_se_pct']['p50']:>6.1f} | "
        f"{s['time_below_75_se_ticks']['mean']:>6.1f} | "
        f"{s['pre_steamroll_volatility']['mean']:>7.1f} | "
        f"{s['competitive_match_count']:>4}/{n:<3} | "
        f"{s['conquer_staged']['mean']:>5.2f} | "
        f"{s['conquer_activated']['mean']:>5.2f} | "
        f"{s['conquer_mean_impact']['mean']:>5.2f} | "
        f"{s['conquer_max_impact']['p90']:>6.2f} | "
        f"{s['final_tick']['p50']:>5.0f} | "
        f"{s['se_pct_final']['p50']:>5.1f}"
    )


def _table_header(param_name: str) -> str:
    return (
        f"{param_name:>14} | "
        f"{'wins':>5} | "
        f"{'run':>3} | "
        f"{'swng_x':>6} | "
        f"{'swng50':>6} | "
        f"{'<75tk':>6} | "
        f"{'volat':>7} | "
        f"{'comp':>8} | "
        f"{'cstg':>5} | "
        f"{'cact':>5} | "
        f"{'cimp':>5} | "
        f"{'cmx90':>6} | "
        f"{'tk50':>5} | "
        f"{'sef50':>5}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Parallel param sweep over headless matches.")
    ap.add_argument("--param", required=True, help="param name to sweep, e.g. retaliation_gauge_threshold")
    ap.add_argument("--values", required=True, help="comma-separated values, e.g. 50,30,20,15,10")
    ap.add_argument("--matches-per-value", type=int, default=50)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--scenario", default="demo_planet")
    ap.add_argument("--max-ticks", type=int, default=600)
    ap.add_argument("--speed", type=float, default=10.0)
    ap.add_argument("--seed-base", type=int, default=1000)
    ap.add_argument("--out", default="server/bench/sweep_results.json")
    ap.add_argument(
        "--detail", action="store_true",
        help="Also print the full per-value summary block (trajectory + conquer metrics with quantiles).",
    )
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
    print(f"Done in {elapsed:.1f}s real ({elapsed / len(jobs):.3f}s per match serial-equivalent).")
    print()

    by_value: dict[Any, list[dict]] = {}
    for r in results:
        by_value.setdefault(r["_param_value"], []).append(r)

    summaries: dict[str, dict[str, Any]] = {}
    header = _table_header(args.param)
    print(header)
    print("-" * len(header))
    for value in values:
        rs = by_value.get(value, [])
        s = summarize_results(rs)
        summaries[str(value)] = s
        print(_table_row(value, s))

    print()
    print(
        "wins=SE/n  run=timeouts  swng_x/50=max_swing mean/p50  <75tk=ticks SE<75% mean  "
        "volat=pre-steamroll volatility mean  comp=competitive matches (no steamroll)/n  "
        "cstg/cact=conquer staged/activated mean  cimp/cmx90=conquer mean impact / max impact p90  "
        "tk50=match length p50  sef50=final SE% p50"
    )

    if args.detail:
        for value in values:
            rs = by_value.get(value, [])
            s = summarize_results(rs)
            print()
            print(format_summary(s, f"{args.param}={value}"))

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
