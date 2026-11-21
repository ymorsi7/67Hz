"""Run Phase 3b live benchmark: Noop, B1, B0 (full PDF, 3 seeds), B3 (random search, 3 seeds), and GridSafe (3 seeds).

Evaluates every dev utility profile (7) and every held-out utility profile (4)
on BOTH scenario families:
  - Fast family: 4 traces (20 s @ 0.002 s = 2x largest 10 s fast-rule window)
  - Slow family: 3 traces (30 min = 1800 s @ 0.5 s: idle, job start, checkpoints, job stop)
Every trace starts at facility idle, prepended by the checker so t=0 jumps count.
Runs B0, B3 (matched simulator evaluation budget), and GridSafe 3 times per profile
with distinct seeds [101, 202, 303] and reports pass rates, per-family scores,
overhead/throughput trade-offs, and unseen random-suite (N=20) generalization.
Saves full results to eval/cache/phase3_results.json.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading
import time
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from eval.random_suite import sample_random_suite_for_ruleset
from evolve.baselines import (
    resolve_pdf_path,
    run_b0_baseline,
    run_b1_baseline,
    run_b3_baseline,
    run_uncontrolled_baseline,
)
from evolve.loop import evolve_controller
from evolve.sandbox import evaluate_controller_sandboxed
from rules.profiles import UtilityProfile, load_utility_profiles
from workload.synth import all_preset_workloads

OUT_PATH = REPO_ROOT / "eval" / "cache" / "phase3_results.json"
DEFAULT_SEEDS = (101, 202, 303)
_PRINT_LOCK = threading.Lock()


def _log(msg: str) -> None:
    with _PRINT_LOCK:
        print(msg, flush=True)


def _fam_tag(ev: dict[str, Any] | None, fam: str) -> str:
    if not ev:
        return "REF"
    fs = (ev.get("family_summary") or {}).get(fam)
    if not fs:
        return "-"
    st = fs.get("status")
    if st == "not_evaluable":
        return "N/E"
    if st == "pass":
        return f"PASS({fs['workloads_passed']}/{fs['workloads_evaluable']})"
    return f"FAIL({fs['workloads_passed']}/{fs['workloads_evaluable']})"


def _aggregate_seed_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize multi-seed reliability runs (pass rate, mean metrics on passing runs, best run)."""
    n = len(runs)
    n_pass = sum(1 for r in runs if r["passed"])
    passing = [r for r in runs if r["passed"] and r.get("eval_result")]
    all_valid = [r for r in runs if r.get("eval_result")]

    def _mean(items: list[dict[str, Any]], key: str) -> float | None:
        if not items:
            return None
        return round(sum(float(x["eval_result"][key]) for x in items) / len(items), 3)

    def _fam_pass_rate(fam: str) -> dict[str, Any]:
        statuses = [
            (r["eval_result"].get("family_summary") or {}).get(fam, {}).get("status")
            for r in runs
            if r.get("eval_result")
        ]
        if statuses and all(s == "not_evaluable" for s in statuses):
            return {"status": "not_evaluable", "passed": 0, "total": n}
        p_cnt = sum(1 for s in statuses if s == "pass")
        return {"status": "pass" if p_cnt == n else ("partial" if p_cnt > 0 else "fail"), "passed": p_cnt, "total": n}

    # Select best representative run (passing beats failing, then lowest composite score)
    best_run = min(
        all_valid,
        key=lambda r: (0 if r["passed"] else 1, float(r["eval_result"]["composite_score"])),
    ) if all_valid else None

    return {
        "runs": n,
        "passed_runs": n_pass,
        "pass_rate": round(n_pass / max(1, n), 4),
        "fast_reliability": _fam_pass_rate("fast"),
        "slow_reliability": _fam_pass_rate("slow"),
        "mean_overhead_pct_all": _mean(all_valid, "mean_energy_overhead_pct"),
        "mean_throughput_loss_pct_all": _mean(all_valid, "mean_throughput_loss_pct"),
        "mean_wear_all": _mean(all_valid, "mean_battery_wear"),
        "mean_overhead_pct_pass": _mean(passing, "mean_energy_overhead_pct"),
        "mean_throughput_loss_pct_pass": _mean(passing, "mean_throughput_loss_pct"),
        "mean_wear_pass": _mean(passing, "mean_battery_wear"),
        "mean_elapsed_s": round(sum(float(r["elapsed_s"]) for r in runs) / max(1, n), 2),
        "best_seed": best_run["seed"] if best_run else None,
        "best_code": best_run["code"] if best_run else None,
        "best_eval": best_run["eval_result"] if best_run else None,
    }


def _run_one_profile(
    prof: UtilityProfile,
    workloads: dict[str, Any],
    *,
    random_seed: int,
    random_n: int,
    seeds: tuple[int, ...],
    population_size: int,
    budget_s: float,
    max_generations: int,
) -> dict[str, Any]:
    tag = f"[{prof.split}:{prof.profile_id}]"
    t_prof0 = time.perf_counter()
    random_workloads = sample_random_suite_for_ruleset(prof.ruleset, n=random_n, seed=random_seed, cluster_mw=100.0)
    _log(
        f"{tag} Starting ({prof.utility}, {len(prof.ruleset.constraints)} rule(s), "
        f"seeds={list(seeds)}, workloads={len(workloads)}, rand_eval={len(random_workloads)})..."
    )

    noop = run_uncontrolled_baseline(prof.ruleset, workloads)
    b1 = run_b1_baseline(prof.ruleset, workloads)

    pdf_path = resolve_pdf_path(REPO_ROOT / prof.source_file)
    if pdf_path is None:
        raise FileNotFoundError(f"Missing PDF for {prof.profile_id}: {prof.source_file}")

    b0_runs: list[dict[str, Any]] = []
    gs_runs: list[dict[str, Any]] = []
    b3_runs: list[dict[str, Any]] = []

    def _run_seed_pair(seed: int) -> tuple[dict[str, Any], dict[str, Any]]:
        with ThreadPoolExecutor(max_workers=2) as pair_pool:
            f_b0 = pair_pool.submit(
                run_b0_baseline,
                prof.ruleset,
                workloads,
                utility_name=prof.utility,
                pdf_path=pdf_path,
                seed=seed,
            )
            f_gs = pair_pool.submit(
                evolve_controller,
                prof.ruleset,
                workloads,
                population_size=population_size,
                budget_s=budget_s,
                max_generations=max_generations,
                seed=seed,
            )
            b0_res = f_b0.result()
            gs_res = f_gs.result()

        b0_entry = {
            "seed": seed,
            "passed": b0_res.eval_result.passed,
            "elapsed_s": round(b0_res.elapsed_s, 2),
            "model": b0_res.model,
            "notes": b0_res.notes,
            "code": b0_res.code,
            "eval_result": b0_res.eval_result.to_dict(),
        }
        gs_entry = {
            "seed": seed,
            "outcome": gs_res.outcome,
            "passed": gs_res.passed,
            "elapsed_s": round(gs_res.elapsed_s, 2),
            "model": gs_res.model,
            "thinking_level": gs_res.thinking_level,
            "generations_completed": gs_res.generations_completed,
            "total_candidates_evaluated": gs_res.total_candidates_evaluated,
            "total_candidates_passed": gs_res.total_candidates_passed,
            "total_static_rejected": gs_res.total_static_rejected,
            "refusal_reason": gs_res.refusal_reason,
            "code": gs_res.best_code,
            "eval_result": gs_res.best_eval.to_dict() if gs_res.best_eval else None,
        }
        return b0_entry, gs_entry

    # Run the 3 seeds for this profile concurrently
    with ThreadPoolExecutor(max_workers=len(seeds)) as seed_pool:
        futs = {seed_pool.submit(_run_seed_pair, s): s for s in seeds}
        seed_results: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
        for fut in as_completed(futs):
            s = futs[fut]
            seed_results[s] = fut.result()

    for s in seeds:
        b0_e, gs_e = seed_results[s]
        b0_runs.append(b0_e)
        gs_runs.append(gs_e)

        # Match B3 simulator evaluation budget to the exact number of candidates GridSafe evaluated!
        n_evals = max(1, int(gs_e["total_candidates_evaluated"]))
        b3_res = run_b3_baseline(
            prof.ruleset,
            workloads,
            n_evals=n_evals,
            seed=s,
        )
        b3_runs.append(
            {
                "seed": s,
                "n_evals": n_evals,
                "passed": b3_res.eval_result.passed,
                "elapsed_s": round(b3_res.elapsed_s, 2),
                "model": b3_res.model,
                "notes": b3_res.notes,
                "code": b3_res.code,
                "eval_result": b3_res.eval_result.to_dict(),
            }
        )

    b0_agg = _aggregate_seed_runs(b0_runs)
    b3_agg = _aggregate_seed_runs(b3_runs)
    gs_agg = _aggregate_seed_runs(gs_runs)

    # Evaluate representative controllers on the unseen random suite (N=20)
    random_evals: dict[str, Any] = {}
    for label, code_str in [
        ("uncontrolled", noop.code),
        ("b1", b1.code),
        ("b0", b0_agg.get("best_code")),
        ("b3", b3_agg.get("best_code")),
        ("gridsafe", gs_agg.get("best_code")),
    ]:
        if code_str:
            rev = evaluate_controller_sandboxed(code_str, prof.ruleset, random_workloads, timeout_s=20.0)
            random_evals[label] = {
                "passed": rev.passed,
                "workloads_passed": rev.workloads_passed,
                "workloads_evaluable": rev.workloads_evaluable,
                "workloads_total": rev.workloads_total,
                "mean_energy_overhead_pct": round(rev.mean_energy_overhead_pct, 3),
                "mean_throughput_loss_pct": round(rev.mean_throughput_loss_pct, 3),
                "family_summary": rev.family_summary,
                "worst_margin_pct": rev.worst_margin_pct,
            }
        else:
            random_evals[label] = {"passed": False, "workloads_passed": 0, "workloads_total": len(random_workloads)}

    _log(
        f"{tag} DONE ({time.perf_counter()-t_prof0:.1f}s) | "
        f"Noop={noop.eval_result.passed} | B1={b1.eval_result.passed} | "
        f"B0={b0_agg['passed_runs']}/{b0_agg['runs']} | "
        f"B3={b3_agg['passed_runs']}/{b3_agg['runs']} | "
        f"GS={gs_agg['passed_runs']}/{gs_agg['runs']} "
        f"(OH={gs_agg['mean_overhead_pct_pass']}%, TP={gs_agg['mean_throughput_loss_pct_pass']}%) | "
        f"Rand20 GS={random_evals['gridsafe']['workloads_passed']}/{random_evals['gridsafe'].get('workloads_evaluable', 20)}"
    )

    return {
        "profile": prof.to_dict(),
        "uncontrolled": noop.to_dict(),
        "b1": b1.to_dict(),
        "b0": {"summary": b0_agg, "runs": b0_runs},
        "b3": {"summary": b3_agg, "runs": b3_runs},
        "gridsafe": {"summary": gs_agg, "runs": gs_runs},
        "random_suite_eval": random_evals,
    }


def print_summary_table(payload: dict[str, Any]) -> None:
    print(
        "\n=== Phase 3b Comparison Table (Idle Start Prepended | Fast 20s + Slow 30m | 3 Seeds per Profile) ==="
    )
    hdr = (
        f"{'Profile':17s} | {'Split':8s} | {'Noop (F/S)':14s} | {'B1 90% (F/S)':14s} | "
        f"{'B0 PDF-in':16s} | {'B3 RandSrch':18s} | {'GridSafe (3 seeds)':30s} | {'Rand20 GS':10s}"
    )
    print(hdr)
    print("-" * len(hdr))

    def _fs_short(ev: dict[str, Any] | None) -> str:
        if not ev:
            return "REF"
        fs = ev.get("family_summary") or {}
        f_s = fs.get("fast", {}).get("status", "-")
        s_s = fs.get("slow", {}).get("status", "-")
        m = {"pass": "P", "fail": "F", "not_evaluable": "N/E", "refused": "R"}
        return f"{'PASS' if ev['passed'] else 'FAIL'} ({m.get(f_s,'-')}/{m.get(s_s,'-')})"

    def _agg_short(agg: dict[str, Any], include_metrics: bool = False) -> str:
        pr = f"{agg['passed_runs']}/{agg['runs']}"
        fr = agg["fast_reliability"]
        sr = agg["slow_reliability"]
        f_ch = "N/E" if fr["status"] == "not_evaluable" else f"{fr['passed']}/{fr['total']}"
        s_ch = "N/E" if sr["status"] == "not_evaluable" else f"{sr['passed']}/{sr['total']}"
        if not include_metrics:
            return f"{pr} ({f_ch}|{s_ch})"
        oh = agg["mean_overhead_pct_pass"]
        tp = agg["mean_throughput_loss_pct_pass"]
        if oh is not None and tp is not None:
            return f"{pr} ({f_ch}|{s_ch}) OH={oh:4.1f}% TP={tp:4.1f}%"
        return f"{pr} ({f_ch}|{s_ch}) OH=n/a"

    for rec in payload["profiles"]:
        p = rec["profile"]
        u_s = _fs_short(rec["uncontrolled"]["eval_result"])
        b1_s = _fs_short(rec["b1"]["eval_result"])
        b0_s = _agg_short(rec["b0"]["summary"])
        b3_s = _agg_short(rec["b3"]["summary"])
        gs_s = _agg_short(rec["gridsafe"]["summary"], include_metrics=True)
        rs = rec.get("random_suite_eval", {}).get("gridsafe", {})
        r_s = (
            f"{'PASS' if rs.get('passed') else 'FAIL'} {rs.get('workloads_passed',0)}/{rs.get('workloads_evaluable',0)}"
        )
        print(
            f"{p['profile_id']:17s} | {p['split']:8s} | {u_s:14s} | {b1_s:14s} | "
            f"{b0_s:16s} | {b3_s:18s} | {gs_s:30s} | {r_s:10s}"
        )


def run_benchmark(
    *,
    seeds: tuple[int, ...] = DEFAULT_SEEDS,
    population_size: int = 6,
    budget_s: float = 75.0,
    max_generations: int = 3,
    max_parallel_profiles: int = 3,
    random_seed: int = 20261121,
    random_n: int = 20,
    split: str = "dev",
    profiles_filter: set[str] | None = None,
    new_only: bool = False,
) -> dict[str, Any]:
    profiles = load_utility_profiles(rated_mw=100.0, split=None if split == "all" else split)
    if profiles_filter:
        profiles = [p for p in profiles if p.profile_id in profiles_filter]

    existing_by_id: dict[str, dict[str, Any]] = {}
    if new_only and OUT_PATH.exists():
        prev = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        for rec in prev.get("profiles", []):
            existing_by_id[rec["profile"]["profile_id"]] = rec

    workloads = all_preset_workloads(100.0)
    by_id: dict[str, dict[str, Any]] = dict(existing_by_id)

    to_run = [p for p in profiles if p.profile_id not in by_id]
    if to_run:
        with ThreadPoolExecutor(max_workers=max_parallel_profiles) as pool:
            futs = {
                pool.submit(
                    _run_one_profile,
                    prof,
                    workloads,
                    random_seed=random_seed,
                    random_n=random_n,
                    seeds=seeds,
                    population_size=population_size,
                    budget_s=budget_s,
                    max_generations=max_generations,
                ): prof.profile_id
                for prof in to_run
            }
            for fut in as_completed(futs):
                pid = futs[fut]
                by_id[pid] = fut.result()

    all_ordered = load_utility_profiles(rated_mw=100.0)
    records = [by_id[p.profile_id] for p in all_ordered if p.profile_id in by_id]

    payload = {
        "schema_version": "phase3b",
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "rated_mw": 100.0,
        "seeds": list(seeds),
        "random_suite_seed": random_seed,
        "random_suite_n": random_n,
        "workloads": {k: v.to_dict() for k, v in workloads.items()},
        "population_size": population_size,
        "budget_s": budget_s,
        "profiles": records,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nSaved Phase 3b results -> {OUT_PATH.relative_to(REPO_ROOT)}", flush=True)
    print_summary_table(payload)
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Phase 3b multi-seed dual-family benchmark.")
    parser.add_argument("--new-only", action="store_true")
    parser.add_argument("--split", choices=["dev", "all"], default="dev", help="Default 'dev'; held_out only for final eval")
    parser.add_argument("--seeds", type=int, nargs="+", default=list(DEFAULT_SEEDS))
    parser.add_argument("--profiles", type=str, nargs="*", default=None)
    parser.add_argument("--max-parallel-profiles", type=int, default=3)
    parser.add_argument("--random-seed", type=int, default=20261121)
    parser.add_argument("--random-n", type=int, default=20)
    args = parser.parse_args()
    run_benchmark(
        seeds=tuple(args.seeds),
        max_parallel_profiles=args.max_parallel_profiles,
        random_seed=args.random_seed,
        random_n=args.random_n,
        split=args.split,
        profiles_filter=set(args.profiles) if args.profiles else None,
        new_only=args.new_only,
    )
