"""Headless single-match runner for benchmark / parameter sweeps.

Instantiates a fresh ``World``, applies any param overrides, and steps the
sim to a terminal state without going through the FastAPI tick-loop sleep.
Returns a metrics dict per match. The full event stream is collected
tick-by-tick so the in-place 100-event buffer cap in ``World.step`` doesn't
lose data when a tick emits a burst.

Per-match output now includes:
- A per-tick trace (tick, se_pct, enemy_pct, contested, retaliation_gauge)
  recorded once per simulated tick.
- A per-salient lifecycle dict (kind, activation tick, peak tracked size,
  max gen, end reason, SE% impact during life). Computed by polling
  ``world.salients`` each tick and then joining against ``salient_ended``
  events.
- Trajectory-shape metrics derived from the trace: max_swing, ticks below
  75% SE, steamroll onset (first 50-tick window of >90% or <10% SE), and
  pre-steamroll volatility.

The raw trace is omitted from the per-match dict by default to keep sweep
output sizes manageable; pass ``include_trace=True`` (or ``--save-trace``)
to opt in for diagnostic dives.
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from typing import Any

from ..sim.scenarios import load_scenario
from ..sim.world import World


# ---------------------------------------------------------------- #
# Single match
# ---------------------------------------------------------------- #


def run_match(
    seed: int,
    params_override: dict[str, Any] | None = None,
    scenario: str = "demo_planet",
    max_ticks: int = 600,
    speed: float = 10.0,
    include_trace: bool = False,
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

    # Per-tick trace: (tick, se_pct, enemy_pct, contested_count, retaliation_gauge)
    trace: list[tuple[int, float, float, int, float]] = []

    # Per-salient lifecycle, populated lazily as salients first appear in
    # world.salients. End reason is filled in after the loop from events.
    salient_lives: dict[str, dict[str, Any]] = {}

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

        # Sample world state for the trace + lifecycle bookkeeping.
        stats = w.stats()
        trace.append(
            (w.tick, stats["se_pct"], stats["enemy_pct"], stats["contested"], w.retaliation_gauge)
        )

        for sid, s in w.salients.items():
            sl = salient_lives.get(sid)
            if sl is None:
                # Snapshot the pre-salient SE% baseline. Staging applies no
                # pressure, so the value at first_seen is the correct
                # baseline for measuring "did this wedge pull the gauge?".
                # We need the *previous* tick's value because by the time
                # we sample post-step, even a same-tick activation has
                # already contested its fan cells.
                baseline = trace[-2][1] if len(trace) >= 2 else stats["se_pct"]
                sl = {
                    "id": sid,
                    "kind": s.kind,
                    "first_seen_tick": w.tick,
                    "se_pct_at_first_seen": baseline,
                    "activated_tick": None,
                    "se_pct_pre_activation": None,
                    "peak_tracked": 0,
                    "max_gen": 0,
                    "last_seen_tick": w.tick,
                    "end_reason": None,
                    "end_tick": None,
                    "min_se_pct_during_life": None,
                    "impact_se_pct": None,
                }
                salient_lives[sid] = sl
            sl["last_seen_tick"] = w.tick
            if s.kind == "conquer":
                if s.activated and sl["activated_tick"] is None:
                    sl["activated_tick"] = w.tick
                    # Pre-activation SE% — use the previous trace entry,
                    # since the current entry already reflects activation
                    # mutating cells during this tick's update_salients.
                    sl["se_pct_pre_activation"] = (
                        trace[-2][1] if len(trace) >= 2 else baseline
                    )
                if s.activated and s.tracked_cells:
                    n = len(s.tracked_cells)
                    if n > sl["peak_tracked"]:
                        sl["peak_tracked"] = n
                    mg = max(s.tracked_cells.values())
                    if mg > sl["max_gen"]:
                        sl["max_gen"] = mg

    # Annotate end reason / tick from the salient_ended events.
    for ev in all_events:
        if ev["type"] != "salient_ended":
            continue
        sid = ev.get("salient_id")
        if sid in salient_lives:
            salient_lives[sid]["end_reason"] = ev.get("reason")
            salient_lives[sid]["end_tick"] = ev["tick"]

    # Compute SE% impact for each conquer salient that activated.
    # Impact = (SE% just before activation) - (min SE% over the wedge's
    # activated lifetime). Positive means the wedge pulled the gauge down
    # for SE; ~0 means it never moved the needle. Negative is possible if
    # SE was actively gaining elsewhere on the front during the wedge's
    # life (the wedge wasn't enough to halt SE momentum).
    for sl in salient_lives.values():
        if sl["kind"] != "conquer" or sl["activated_tick"] is None:
            continue
        start = sl["activated_tick"]
        end = sl["end_tick"] if sl["end_tick"] is not None else (trace[-1][0] if trace else start)
        relevant = [t for t in trace if start <= t[0] <= end]
        if not relevant:
            continue
        min_se = min(t[1] for t in relevant)
        baseline = sl["se_pct_pre_activation"]
        sl["min_se_pct_during_life"] = round(min_se, 2)
        sl["impact_se_pct"] = round(baseline - min_se, 2)

    captures = [e for e in all_events if e["type"] == "cell_captured"]
    repulses = [e for e in all_events if e["type"] == "cell_repulsed"]
    salients_spawned = [e for e in all_events if e["type"] == "salient_spawned"]
    staging_spawned = [e for e in all_events if e["type"] == "salient_staging_spawned"]
    activated = [e for e in all_events if e["type"] == "salient_activated"]
    salients_ended = [e for e in all_events if e["type"] == "salient_ended"]
    factory_strikes = [e for e in all_events if e["type"] == "factory_strike"]
    builds_done = [e for e in all_events if e["type"] == "build_completed"]

    se_caps = sum(1 for e in captures if e.get("defender") == "se")
    en_caps = sum(1 for e in captures if e.get("defender") == "enemy")

    salient_kinds = Counter(e.get("kind") for e in salients_spawned)
    salient_reasons = Counter(e.get("reason") for e in salients_ended)

    trajectory = _compute_trajectory_metrics(trace)

    conquer_impacts = [
        sl["impact_se_pct"] for sl in salient_lives.values()
        if sl["kind"] == "conquer" and sl["impact_se_pct"] is not None
    ]
    mean_conquer_impact = round(statistics.mean(conquer_impacts), 2) if conquer_impacts else 0.0
    max_conquer_impact = round(max(conquer_impacts), 2) if conquer_impacts else 0.0

    result: dict[str, Any] = {
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
        "salients_destroy": salient_kinds.get("destroy", 0),
        "salients_conquer_staged": len(staging_spawned),
        "salients_conquer_activated": len(activated),
        "salients_ended": len(salients_ended),
        "salient_success": salient_reasons.get("success", 0),
        "salient_expired": salient_reasons.get("expired", 0),
        "salient_intercepted": salient_reasons.get("intercepted", 0),
        "salient_extinguished": salient_reasons.get("extinguished", 0),
        "factory_strikes": len(factory_strikes),
        "builds_completed": len(builds_done),
        "peak_retaliation_gauge": round(peak_gauge, 2),
        # Trajectory shape (the dynamism signal).
        **trajectory,
        # Conquer-impact aggregates.
        "mean_conquer_impact": mean_conquer_impact,
        "max_conquer_impact": max_conquer_impact,
        # Per-salient lifecycle entries (cheap, ~3-5 per match).
        "salient_lives": list(salient_lives.values()),
    }
    if include_trace:
        result["trace"] = trace
    return result


# ---------------------------------------------------------------- #
# Trajectory-shape metrics
# ---------------------------------------------------------------- #


def _compute_trajectory_metrics(
    trace: list[tuple[int, float, float, int, float]],
    steamroll_se_high: float = 90.0,
    steamroll_se_low: float = 10.0,
    steamroll_min_run: int = 20,
) -> dict[str, Any]:
    """Derive shape metrics from a per-tick SE% trace.

    - max_swing_se_pct: max(se_pct) - min(se_pct) across the match.
    - time_below_75_se_ticks: tick count where SE was below 75% (i.e.,
      enemy held at least 25% of contested+enemy cells).
    - steamroll_onset_tick: earliest tick at which the eventual winner's
      lead became continuous through to match end. Detected by walking
      backward from the final tick: the steamroll onset is the earliest
      tick from which SE% stayed above ``steamroll_se_high`` (or below
      ``steamroll_se_low``) every tick to the end. Requires at least
      ``steamroll_min_run`` consecutive ticks of dominance to count, so
      a brief blip in the closing seconds doesn't get flagged.
      ``None`` if the match never reached sustained dominance — i.e.,
      stayed competitive throughout.
    - pre_steamroll_volatility: sum of |Δ se_pct| over the pre-steamroll
      segment (or full match if no steamroll). Captures total churn during
      the competitive phase, ignoring the boring tail.
    """
    if not trace:
        return {
            "max_swing_se_pct": 0.0,
            "time_below_75_se_ticks": 0,
            "steamroll_onset_tick": None,
            "pre_steamroll_volatility": 0.0,
        }

    se_pcts = [t[1] for t in trace]

    max_swing = round(max(se_pcts) - min(se_pcts), 2)
    time_below_75 = sum(1 for s in se_pcts if s < 75.0)

    # Walk backward from the final tick. If the trailing run of dominance
    # is long enough, its earliest tick is the steamroll onset.
    last = se_pcts[-1]
    pred = None
    if last > steamroll_se_high:
        pred = lambda s: s > steamroll_se_high
    elif last < steamroll_se_low:
        pred = lambda s: s < steamroll_se_low

    steamroll_onset: int | None = None
    if pred is not None:
        onset_idx = len(se_pcts)
        for i in range(len(se_pcts) - 1, -1, -1):
            if pred(se_pcts[i]):
                onset_idx = i
            else:
                break
        if len(se_pcts) - onset_idx >= steamroll_min_run:
            steamroll_onset = trace[onset_idx][0]

    if steamroll_onset is not None:
        pre = [s for tick, s, *_ in trace if tick < steamroll_onset]
    else:
        pre = se_pcts
    if len(pre) >= 2:
        volatility = round(sum(abs(pre[i] - pre[i - 1]) for i in range(1, len(pre))), 2)
    else:
        volatility = 0.0

    return {
        "max_swing_se_pct": max_swing,
        "time_below_75_se_ticks": time_below_75,
        "steamroll_onset_tick": steamroll_onset,
        "pre_steamroll_volatility": volatility,
    }


# ---------------------------------------------------------------- #
# Parallel multi-match runner + summary
# ---------------------------------------------------------------- #


def _worker(args: tuple[int, dict[str, Any] | None, str, int, float, bool]) -> dict:
    seed, overrides, scenario, max_ticks, speed, include_trace = args
    return run_match(
        seed=seed,
        params_override=overrides,
        scenario=scenario,
        max_ticks=max_ticks,
        speed=speed,
        include_trace=include_trace,
    )


def run_many(
    n: int,
    params_override: dict[str, Any] | None = None,
    scenario: str = "demo_planet",
    max_ticks: int = 600,
    speed: float = 10.0,
    base_seed: int = 1000,
    workers: int = 8,
    include_trace: bool = False,
) -> list[dict]:
    """Run ``n`` matches in parallel via ProcessPoolExecutor. Seeds are
    ``base_seed + i`` so reruns are deterministic per (n, base_seed)."""
    args_list = [
        (base_seed + i, params_override, scenario, max_ticks, speed, include_trace)
        for i in range(n)
    ]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(_worker, args_list))
    results.sort(key=lambda r: r["seed"])
    return results


def _quantile(values: list[float], p: float) -> float:
    """Linear-interpolated quantile. ``p`` in [0, 1]."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _q_summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "p10": 0.0, "p50": 0.0, "p90": 0.0}
    return {
        "mean": round(statistics.mean(values), 2),
        "p10": round(_quantile(values, 0.10), 2),
        "p50": round(_quantile(values, 0.50), 2),
        "p90": round(_quantile(values, 0.90), 2),
    }


def summarize_results(results: list[dict]) -> dict[str, Any]:
    """Build a compact summary suitable for printing or for dropping into
    a sweep table. Quantiles are p10/p50/p90 — wide enough to surface
    asymmetric distributions without bloating the output."""
    n = len(results)
    if n == 0:
        return {"n": 0}

    end_states = Counter(r["end_state"] for r in results)
    se_pct_final = [r["final_stats"]["se_pct"] for r in results]
    final_ticks = [r["final_tick"] for r in results]
    max_swing = [r["max_swing_se_pct"] for r in results]
    time_below_75 = [r["time_below_75_se_ticks"] for r in results]
    pre_volatility = [r["pre_steamroll_volatility"] for r in results]

    steamroll_ticks = [r["steamroll_onset_tick"] for r in results if r["steamroll_onset_tick"] is not None]
    competitive_count = sum(1 for r in results if r["steamroll_onset_tick"] is None)

    repulses = [r["repulses"] for r in results]
    factory_strikes = [r["factory_strikes"] for r in results]
    cnq_staged = [r["salients_conquer_staged"] for r in results]
    cnq_activated = [r["salients_conquer_activated"] for r in results]
    cnq_intercept = [r["salient_intercepted"] for r in results]
    cnq_exting = [r["salient_extinguished"] for r in results]
    cnq_impact = [r["mean_conquer_impact"] for r in results]
    cnq_max_impact = [r["max_conquer_impact"] for r in results]

    return {
        "n": n,
        "wins": {
            "se": end_states.get("se_won", 0),
            "enemy": end_states.get("enemy_won", 0),
            "running": end_states.get("running", 0),
        },
        "se_pct_final": _q_summary(se_pct_final),
        "final_tick": _q_summary(final_ticks),
        # Trajectory shape — the dynamism signal.
        "max_swing_se_pct": _q_summary(max_swing),
        "time_below_75_se_ticks": _q_summary(time_below_75),
        "pre_steamroll_volatility": _q_summary(pre_volatility),
        "steamroll_onset_tick": _q_summary([float(s) for s in steamroll_ticks]),
        "competitive_match_count": competitive_count,
        # Conquer salient health.
        "conquer_staged": _q_summary([float(x) for x in cnq_staged]),
        "conquer_activated": _q_summary([float(x) for x in cnq_activated]),
        "conquer_intercepted": _q_summary([float(x) for x in cnq_intercept]),
        "conquer_extinguished": _q_summary([float(x) for x in cnq_exting]),
        "conquer_mean_impact": _q_summary(cnq_impact),
        "conquer_max_impact": _q_summary(cnq_max_impact),
        # Coarse activity signals.
        "repulses": _q_summary([float(x) for x in repulses]),
        "factory_strikes": _q_summary([float(x) for x in factory_strikes]),
    }


def format_summary(summary: dict[str, Any], param_label: str | None = None) -> str:
    """Pretty-print a summary dict for terminal output."""
    if summary.get("n", 0) == 0:
        return "(no results)"
    lines: list[str] = []
    if param_label:
        lines.append(f"=== {param_label} ===")
    n = summary["n"]
    w = summary["wins"]
    lines.append(f"n={n}  SE wins={w['se']}/{n}  enemy={w['enemy']}/{n}  running={w['running']}/{n}")

    def fmt_q(label: str, qd: dict[str, float], unit: str = "") -> str:
        return (
            f"  {label:<28} mean={qd['mean']:>7.2f}{unit}  "
            f"p10={qd['p10']:>7.2f}  p50={qd['p50']:>7.2f}  p90={qd['p90']:>7.2f}"
        )

    lines.append("Trajectory shape:")
    lines.append(fmt_q("max_swing_se_pct", summary["max_swing_se_pct"], "%"))
    lines.append(fmt_q("time_below_75_se_ticks", summary["time_below_75_se_ticks"]))
    lines.append(fmt_q("pre_steamroll_volatility", summary["pre_steamroll_volatility"]))
    sr = summary["steamroll_onset_tick"]
    comp = summary["competitive_match_count"]
    lines.append(
        f"  steamroll_onset_tick         "
        f"mean={sr['mean']:>7.2f}   p10={sr['p10']:>7.2f}  p50={sr['p50']:>7.2f}  p90={sr['p90']:>7.2f}  "
        f"({comp}/{n} stayed competitive)"
    )

    lines.append("Conquer salients:")
    lines.append(fmt_q("staged",        summary["conquer_staged"]))
    lines.append(fmt_q("activated",     summary["conquer_activated"]))
    lines.append(fmt_q("intercepted",   summary["conquer_intercepted"]))
    lines.append(fmt_q("extinguished",  summary["conquer_extinguished"]))
    lines.append(fmt_q("mean_impact_se", summary["conquer_mean_impact"], "%"))
    lines.append(fmt_q("max_impact_se",  summary["conquer_max_impact"], "%"))

    lines.append("Other:")
    lines.append(fmt_q("se_pct_final",   summary["se_pct_final"], "%"))
    lines.append(fmt_q("final_tick",     summary["final_tick"]))
    lines.append(fmt_q("repulses",       summary["repulses"]))
    lines.append(fmt_q("factory_strikes", summary["factory_strikes"]))
    return "\n".join(lines)


# ---------------------------------------------------------------- #
# CLI
# ---------------------------------------------------------------- #


def _parse_overrides(items: list[str]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for kv in items:
        k, _, v = kv.partition("=")
        try:
            v_parsed: Any = int(v)
        except ValueError:
            try:
                v_parsed = float(v)
            except ValueError:
                v_parsed = v
        overrides[k] = v_parsed
    return overrides


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Headless sim runner. Default: run a single match (seed=1) and "
            "dump the JSON. With --n N, runs N matches in parallel and "
            "prints a quantile summary instead."
        )
    )
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument(
        "--n", type=int, default=None,
        help="If given, run N matches in parallel and print a summary. "
             "Seeds are seed_base+i (default seed_base=1000).",
    )
    ap.add_argument("--seed-base", type=int, default=1000)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--scenario", default="demo_planet")
    ap.add_argument("--max-ticks", type=int, default=600)
    ap.add_argument("--speed", type=float, default=10.0)
    ap.add_argument(
        "--param", action="append", default=[],
        help="Param override as name=value, repeatable. Example: --param retaliation_gauge_threshold=20",
    )
    ap.add_argument(
        "--save-trace", action="store_true",
        help="Include the per-tick trace in the per-match output (single-match mode only).",
    )
    ap.add_argument("--out", default=None, help="If set, write the full results JSON to this path.")
    args = ap.parse_args()

    overrides = _parse_overrides(args.param) or None

    if args.n is None:
        result = run_match(
            seed=args.seed,
            params_override=overrides,
            scenario=args.scenario,
            max_ticks=args.max_ticks,
            speed=args.speed,
            include_trace=args.save_trace,
        )
        text = json.dumps(result, indent=2, default=str)
        if args.out:
            with open(args.out, "w") as f:
                f.write(text)
            print(f"Wrote per-match result to {args.out}")
        else:
            print(text)
        return 0

    results = run_many(
        n=args.n,
        params_override=overrides,
        scenario=args.scenario,
        max_ticks=args.max_ticks,
        speed=args.speed,
        base_seed=args.seed_base,
        workers=args.workers,
    )
    summary = summarize_results(results)
    label = f"{args.n} matches, {args.scenario}"
    if overrides:
        label += f", overrides={overrides}"
    print(format_summary(summary, label))
    if args.out:
        with open(args.out, "w") as f:
            json.dump({"summary": summary, "results": results}, f, indent=2, default=str)
        print(f"\nFull results written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
