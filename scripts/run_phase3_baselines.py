"""Run Phase 3 live benchmark: Uncontrolled, B1 (90% floor), B0 (Neon 1-shot), and GridSafe Designer.

Evaluates every dev utility profile (CAISO, ERCOT, ATC, AESO, Southern-dev, LIPA)
and every held-out utility profile (Southern held-out + any newly added specs PDFs)
using the exact same sandboxed deterministic checker on the exact same default
workloads. Saves full results and winning controllers to eval/cache/phase3_results.json.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evolve.baselines import run_b0_baseline, run_b1_baseline, run_uncontrolled_baseline
from evolve.loop import evolve_controller
from rules.profiles import UtilityProfile, load_utility_profiles
from workload.synth import preset_workloads

OUT_PATH = REPO_ROOT / "eval" / "cache" / "phase3_results.json"
_PRINT_LOCK = threading.Lock()


def _log(msg: str) -> None:
    with _PRINT_LOCK:
        print(msg, flush=True)


def _fmt_ev(ev: dict[str, Any] | None) -> str:
    if ev is None:
        return "REFUSED"
    status = "PASS" if ev["passed"] else f"FAIL({ev['workloads_passed']}/{ev['workloads_total']})"
    oh = ev["mean_energy_overhead_pct"]
    tp = ev["mean_throughput_loss_pct"]
    wear = ev["mean_battery_wear"]
    return f"{status:10s} OH={oh:5.1f}% TP={tp:4.1f}% wear={wear:.5f}"


def _run_one_profile(
    prof: UtilityProfile,
    workloads: dict[str, Any],
    *,
    population_size: int,
    budget_s: float,
    max_generations: int,
) -> dict[str, Any]:
    tag = f"[{prof.split}:{prof.profile_id}]"
    _log(f"{tag} Starting ({prof.utility}, {len(prof.ruleset.constraints)} checkable rule(s))...")

    noop = run_uncontrolled_baseline(prof.ruleset, workloads)
    b1 = run_b1_baseline(prof.ruleset, workloads)

    pdf_path = REPO_ROOT / prof.source_file
    # Run B0 (single-shot Neon) and GridSafe Designer (parallel Hydrogen) concurrently for this profile
    with ThreadPoolExecutor(max_workers=2) as subpool:
        b0_fut = subpool.submit(
            run_b0_baseline,
            prof.ruleset,
            workloads,
            utility_name=prof.utility,
            pdf_path=pdf_path if pdf_path.exists() else None,
            pages=prof.pages,
        )

        def _on_ev(e: dict[str, Any]) -> None:
            if e["event"] == "new_best":
                _log(
                    f"  {tag} gen {e['generation']} w{e['worker_id']} ({e['strategy']}): "
                    f"pass={e['passed']} OH={e['overhead_pct']}% TP={e['throughput_loss_pct']}% "
                    f"wear={e['battery_wear']:.5f} score={e['score']}"
                )

        gs_fut = subpool.submit(
            evolve_controller,
            prof.ruleset,
            workloads,
            population_size=population_size,
            budget_s=budget_s,
            max_generations=max_generations,
            on_event=_on_ev,
        )
        b0 = b0_fut.result()
        ev_res = gs_fut.result()

    failed_ids = sorted({f["constraint_id"] for f in noop.eval_result.failing_constraints})
    _log(
        f"{tag} DONE | Noop: {_fmt_ev(noop.eval_result.to_dict())} failed={failed_ids}\n"
        f"{tag}      | B1  : {_fmt_ev(b1.eval_result.to_dict())}\n"
        f"{tag}      | B0  : {_fmt_ev(b0.eval_result.to_dict())} ({b0.elapsed_s:.1f}s)\n"
        f"{tag}      | GS  : [{ev_res.outcome}] "
        f"{_fmt_ev(ev_res.best_eval.to_dict() if ev_res.best_eval else None)} "
        f"(gens={ev_res.generations_completed}, cands={ev_res.total_candidates_evaluated}, "
        f"passed={ev_res.total_candidates_passed}, static_rej={ev_res.total_static_rejected}, {ev_res.elapsed_s:.1f}s)"
    )

    compact_history = [
        {
            "generation": h.generation,
            "worker_id": h.worker_id,
            "strategy": h.strategy,
            "passed": h.eval_result.passed,
            "workloads_passed": h.eval_result.workloads_passed,
            "workloads_total": h.eval_result.workloads_total,
            "composite_score": round(h.eval_result.composite_score, 4),
            "mean_energy_overhead_pct": round(h.eval_result.mean_energy_overhead_pct, 3),
            "mean_throughput_loss_pct": round(h.eval_result.mean_throughput_loss_pct, 3),
            "mean_battery_wear": round(h.eval_result.mean_battery_wear, 6),
            "worst_margin_pct": h.eval_result.worst_margin_pct,
            "static_violations": h.eval_result.static_violations,
            "error": h.eval_result.error,
            "api_time_s": round(h.api_time_s, 2),
        }
        for h in ev_res.history
    ]

    return {
        "profile": prof.to_dict(),
        "workloads": {k: v.to_dict() for k, v in workloads.items()},
        "uncontrolled": noop.to_dict(),
        "b1": b1.to_dict(),
        "b0": b0.to_dict(),
        "gridsafe": {
            "outcome": ev_res.outcome,
            "passed": ev_res.passed,
            "model": ev_res.model,
            "thinking_level": ev_res.thinking_level,
            "best_code": ev_res.best_code,
            "best_eval": ev_res.best_eval.to_dict() if ev_res.best_eval else None,
            "seed_eval": ev_res.seed_eval.to_dict(),
            "fallback_eval": ev_res.fallback_eval.to_dict() if ev_res.fallback_eval else None,
            "generations_completed": ev_res.generations_completed,
            "total_candidates_evaluated": ev_res.total_candidates_evaluated,
            "total_candidates_passed": ev_res.total_candidates_passed,
            "total_static_rejected": ev_res.total_static_rejected,
            "elapsed_s": round(ev_res.elapsed_s, 2),
            "refusal_reason": ev_res.refusal_reason,
            "history_summary": compact_history,
        },
    }


def run_benchmark(
    *,
    population_size: int = 6,
    budget_s: float = 75.0,
    max_generations: int = 3,
    max_parallel_profiles: int = 4,
    profiles_filter: set[str] | None = None,
    new_only: bool = False,
) -> dict[str, Any]:
    profiles = load_utility_profiles(rated_mw=100.0)
    if profiles_filter:
        profiles = [p for p in profiles if p.profile_id in profiles_filter]

    existing_by_id: dict[str, dict[str, Any]] = {}
    if new_only and OUT_PATH.exists():
        prev = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        for rec in prev.get("profiles", []):
            existing_by_id[rec["profile"]["profile_id"]] = rec

    workloads = preset_workloads(100.0)
    by_id: dict[str, dict[str, Any]] = dict(existing_by_id)

    to_run = [p for p in profiles if p.profile_id not in by_id]
    if to_run:
        with ThreadPoolExecutor(max_workers=max_parallel_profiles) as pool:
            futs = {
                pool.submit(
                    _run_one_profile,
                    prof,
                    workloads,
                    population_size=population_size,
                    budget_s=budget_s,
                    max_generations=max_generations,
                ): prof.profile_id
                for prof in to_run
            }
            for fut in as_completed(futs):
                pid = futs[fut]
                by_id[pid] = fut.result()

    # Preserve deterministic profile order
    records = [by_id[p.profile_id] for p in profiles if p.profile_id in by_id]

    payload = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "rated_mw": 100.0,
        "workloads": list(workloads.keys()),
        "population_size": population_size,
        "budget_s": budget_s,
        "profiles": records,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nSaved Phase 3 results -> {OUT_PATH.relative_to(REPO_ROOT)}", flush=True)
    print_summary_table(payload)
    return payload


def print_summary_table(payload: dict[str, Any]) -> None:
    print("\n=== Phase 3 Comparison Table (Same Deterministic Checker, 4 Workloads per Profile) ===")
    hdr = (
        f"{'Profile':18s} | {'Split':8s} | {'Noop':9s} | {'B1 (90%)':19s} | "
        f"{'B0 (Neon 1x)':25s} | {'GridSafe (Hydrogen)':33s}"
    )
    print(hdr)
    print("-" * len(hdr))
    for rec in payload["profiles"]:
        p = rec["profile"]
        u_ev = rec["uncontrolled"]["eval_result"]
        b1_ev = rec["b1"]["eval_result"]
        b0_ev = rec["b0"]["eval_result"]
        gs = rec["gridsafe"]
        gs_ev = gs["best_eval"]

        u_s = f"{'PASS' if u_ev['passed'] else 'FAIL'} {u_ev['workloads_passed']}/{u_ev['workloads_total']}"
        b1_s = (
            f"{'PASS' if b1_ev['passed'] else 'FAIL'} {b1_ev['workloads_passed']}/{b1_ev['workloads_total']} "
            f"OH={b1_ev['mean_energy_overhead_pct']:4.1f}%"
        )
        b0_s = (
            f"{'PASS' if b0_ev['passed'] else 'FAIL'} {b0_ev['workloads_passed']}/{b0_ev['workloads_total']} "
            f"OH={b0_ev['mean_energy_overhead_pct']:4.1f}% TP={b0_ev['mean_throughput_loss_pct']:4.1f}%"
        )
        if gs_ev:
            gs_s = (
                f"{'PASS' if gs['passed'] else 'FAIL'} {gs_ev['workloads_passed']}/{gs_ev['workloads_total']} "
                f"OH={gs_ev['mean_energy_overhead_pct']:4.1f}% TP={gs_ev['mean_throughput_loss_pct']:4.1f}% [{gs['elapsed_s']:.0f}s]"
            )
        else:
            gs_s = f"REFUSED [{gs['elapsed_s']:.0f}s]"
        print(f"{p['profile_id']:18s} | {p['split']:8s} | {u_s:9s} | {b1_s:19s} | {b0_s:25s} | {gs_s:33s}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run Phase 3 baselines + GridSafe Designer benchmark.")
    parser.add_argument(
        "--new-only",
        action="store_true",
        help="Keep cached results for existing profiles and only run newly discovered profiles.",
    )
    args = parser.parse_args()
    run_benchmark(new_only=args.new_only)
