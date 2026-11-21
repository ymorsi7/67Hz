"""GridSafe evaluation entrypoint (Phase 1 physics gates + Phase 2 extraction + Phase 3b dual-family & random suite)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

import numpy as np

from eval.extraction import evaluate_extraction
from eval.random_suite import sample_random_suite
from evolve.sandbox import evaluate_controller_sandboxed
from rules.checker import check_compliance
from rules.profiles import load_utility_profiles
from rules.schema import Constraint, RuleSet
from sim.plant import fixed_floor_controller, noop_controller, simulate_plant
from sim.shaft import load_shaft_model
from workload.synth import all_preset_workloads, generate_workload

REPO_ROOT = Path(__file__).resolve().parent.parent
PHASE3_CACHE = REPO_ROOT / "eval" / "cache" / "phase3_results.json"


def run_phase1_validation() -> dict:
    """Execute deterministic Phase 1 physics and checker validation gates on BOTH fast and slow families."""
    shaft = load_shaft_model()
    mode_report = shaft.validate_modes(max_error_pct=2.0)

    presets = all_preset_workloads(cluster_mw=100.0)
    ruleset = RuleSet(
        source_name="phase1_gate_ruleset",
        rated_mw=100.0,
        constraints=[
            Constraint("ramp_rate", 30.0, "MW/min", page=1, snippet="30 MW/min", window_s=60.0),
            Constraint("max_step", 10.0, "MW", page=1, snippet="10 MW", window_s=5.0),
            Constraint(
                "fluctuation_band",
                2.0,
                "MW",
                page=1,
                snippet="2 MW in 10-50 Hz",
                freq_min_hz=10.0,
                freq_max_hz=50.0,
            ),
            Constraint("dynamic_range", 25.0, "MW", page=1, snippet="25 MW"),
        ],
    )

    timings_s: list[float] = []
    max_energy_err = 0.0
    preset_summary: list[dict] = []

    for name, params in presets.items():
        t0 = time.perf_counter()
        trace = generate_workload(params)
        sim_raw = simulate_plant(trace, controller=noop_controller, shaft_model=shaft)
        rep_raw = check_compliance(sim_raw, ruleset)
        elapsed = time.perf_counter() - t0
        timings_s.append(elapsed)
        max_energy_err = max(max_energy_err, sim_raw.metrics.energy_balance_error_mwh)

        sim_b1 = simulate_plant(trace, controller=fixed_floor_controller(0.90), shaft_model=shaft)
        rep_b1 = check_compliance(sim_b1, ruleset)
        max_energy_err = max(max_energy_err, sim_b1.metrics.energy_balance_error_mwh)

        def _active_batt_probe(obs: dict) -> dict[str, float]:
            return {
                "gpu_floor_mw": 45.0,
                "gpu_cap_mw": 92.0,
                "battery_setpoint_mw": 0.9 * (obs["p_raw_mw"] - 75.0),
            }

        sim_batt = simulate_plant(trace, controller=_active_batt_probe, shaft_model=shaft)
        max_energy_err = max(max_energy_err, sim_batt.metrics.energy_balance_error_mwh)

        preset_summary.append(
            {
                "preset": name,
                "family": params.family,
                "eval_time_s": elapsed,
                "raw_passed": rep_raw.passed,
                "raw_status": rep_raw.status,
                "raw_max_torque_pu": sim_raw.shaft.max_torque_pu,
                "b1_passed": rep_b1.passed,
                "b1_max_torque_pu": sim_b1.shaft.max_torque_pu,
                "b1_energy_overhead_pct": sim_b1.metrics.energy_overhead_pct,
            }
        )

    max_eval_s = max(timings_s)
    all_gates_pass = bool(mode_report["passed"] and max_eval_s < 0.5 and max_energy_err < 1e-9)
    return {
        "passed": all_gates_pass,
        "mode_report": mode_report,
        "max_eval_time_s": max_eval_s,
        "mean_eval_time_s": float(np.mean(timings_s)),
        "max_energy_balance_error_mwh": max_energy_err,
        "presets": preset_summary,
    }


def _print_phase3_summary(p3: dict, mode: str) -> None:
    is_3b = p3.get("schema_version") == "phase3b"
    for rec in p3.get("profiles", []):
        prof = rec["profile"]
        if mode == "dev" and prof["split"] != "dev":
            continue
        u = rec["uncontrolled"]["eval_result"]
        b1 = rec["b1"]["eval_result"]
        if is_3b:
            b0_s = rec["b0"]["summary"]
            b3_s = rec["b3"]["summary"]
            gs_s = rec["gridsafe"]["summary"]
            oh_txt = (
                f"OH={gs_s['mean_overhead_pct_pass']:.1f}% TP={gs_s['mean_throughput_loss_pct_pass']:.1f}%"
                if gs_s.get("mean_overhead_pct_pass") is not None
                else "no pass"
            )
            print(
                f"  [{prof['split']:8s}] {prof['profile_id']:17s} | "
                f"Noop={'PASS' if u['passed'] else 'FAIL':4s} | "
                f"B1={'PASS' if b1['passed'] else 'FAIL':4s} | "
                f"B0={b0_s['passed_runs']}/{b0_s['runs']} | "
                f"B3={b3_s['passed_runs']}/{b3_s['runs']} | "
                f"GridSafe={gs_s['passed_runs']}/{gs_s['runs']} ({oh_txt})"
            )
        else:
            b0 = rec["b0"]["eval_result"]
            gs = rec["gridsafe"]
            gs_ev = gs.get("best_eval")
            gs_txt = (
                f"{'PASS' if gs['passed'] else 'FAIL'} "
                f"OH={gs_ev['mean_energy_overhead_pct']:.1f}% TP={gs_ev['mean_throughput_loss_pct']:.1f}%"
                if gs_ev
                else f"{gs['outcome']}"
            )
            print(
                f"  [{prof['split']:8s}] {prof['profile_id']:17s} | "
                f"Noop={'PASS' if u['passed'] else 'FAIL'} | "
                f"B1={'PASS' if b1['passed'] else 'FAIL'} | "
                f"B0={'PASS' if b0['passed'] else 'FAIL'} | "
                f"GridSafe={gs_txt}"
            )


def _run_unseen_random_eval(p3: dict, seed: int, n_workloads: int, mode: str) -> None:
    """Evaluate cached controllers on N unseen randomly sampled workloads (chosen by --seed)."""
    suite = sample_random_suite(n=n_workloads, seed=seed, cluster_mw=100.0)
    profiles_by_id = {p.profile_id: p for p in load_utility_profiles(rated_mw=100.0)}
    is_3b = p3.get("schema_version") == "phase3b"

    print(f"\n[eval:{mode}] Unseen Random Workload Suite (N={n_workloads}, seed={seed}):")
    totals = {k: [0, 0] for k in ("uncontrolled", "b1", "b0", "b3", "gridsafe")}

    for rec in p3.get("profiles", []):
        pid = rec["profile"]["profile_id"]
        split = rec["profile"]["split"]
        if mode == "dev" and split != "dev":
            continue
        prof = profiles_by_id.get(pid)
        if prof is None:
            continue

        codes: dict[str, str | None] = {
            "uncontrolled": rec["uncontrolled"]["code"],
            "b1": rec["b1"]["code"],
            "b0": rec["b0"]["summary"]["best_code"] if is_3b else rec["b0"]["code"],
            "b3": rec["b3"]["summary"]["best_code"] if is_3b else None,
            "gridsafe": rec["gridsafe"]["summary"]["best_code"] if is_3b else rec["gridsafe"].get("best_code"),
        }
        parts = []
        for label in ("uncontrolled", "b1", "b0", "b3", "gridsafe"):
            c_str = codes[label]
            if not c_str:
                parts.append(f"{label}=n/a")
                continue
            ev = evaluate_controller_sandboxed(c_str, prof.ruleset, suite, timeout_s=20.0)
            totals[label][1] += 1
            if ev.passed:
                totals[label][0] += 1
            parts.append(f"{label}={'PASS' if ev.passed else 'FAIL'}({ev.workloads_passed}/{ev.workloads_evaluable})")
        print(f"  [{split:8s}] {pid:17s} | " + " | ".join(parts))

    summary_parts = [f"{k}={v[0]}/{v[1]}" for k, v in totals.items() if v[1] > 0]
    print("  Totals (profiles passing all unseen workloads): " + ", ".join(summary_parts))


def main() -> int:
    parser = argparse.ArgumentParser(description="GridSafe evaluation harness")
    parser.add_argument("--mode", choices=["dev", "final"], default="dev")
    parser.add_argument("--seed", type=int, default=20261121, help="Seed for unseen random workload suite")
    parser.add_argument("--random-n", type=int, default=20, help="Number of unseen random workloads (default: 20)")
    args = parser.parse_args()

    p1 = run_phase1_validation()
    mr = p1["mode_report"]
    print(f"[eval:{args.mode}] Phase 1 Physics & Checker Gates (Fast 20s + Slow 30m):")
    print(
        f"  IEEE FBM modes: {'PASS' if mr['passed'] else 'FAIL'} "
        f"(max error = {mr['max_error_pct']:.4f}% <= {mr['threshold_pct']:.1f}%)"
    )
    print(
        f"  Eval speed:     {'PASS' if p1['max_eval_time_s'] < 0.5 else 'FAIL'} "
        f"(mean = {p1['mean_eval_time_s']*1000:.1f} ms, max = {p1['max_eval_time_s']*1000:.1f} ms < 500 ms)"
    )
    print(
        f"  Energy ledger:  {'PASS' if p1['max_energy_balance_error_mwh'] < 1e-9 else 'FAIL'} "
        f"(max residual = {p1['max_energy_balance_error_mwh']:.3e} MWh)"
    )
    for row in p1["presets"]:
        print(
            f"  [{row['family']:4s}] {row['preset']:24s} | {row['eval_time_s']*1000:5.1f} ms "
            f"| raw_torque={row['raw_max_torque_pu']:.4f} pu "
            f"| B1_90%_torque={row['b1_max_torque_pu']:.4f} pu "
            f"| B1_overhead={row['b1_energy_overhead_pct']:.1f}%"
        )

    ext_summary = evaluate_extraction()
    print(f"\n[eval:{args.mode}] Phase 2 Extraction Evaluation:")
    if ext_summary.status == "no gold labels":
        print(f"  Status: {ext_summary.message}")
    else:
        print(
            f"  Gold docs: {ext_summary.gold_files_found} | "
            f"Precision: {ext_summary.precision} | Recall: {ext_summary.recall} | "
            f"Hallucination: {ext_summary.hallucination_rate} | Abstention: {ext_summary.abstention_rate}"
        )

    print(f"\n[eval:{args.mode}] Phase 3b Utility Profiles & Designer vs Baselines:")
    all_profiles = load_utility_profiles(rated_mw=100.0)
    dev_n = sum(1 for p in all_profiles if p.split == "dev")
    held_n = sum(1 for p in all_profiles if p.split == "held_out")
    print(f"  Loaded profiles: {dev_n} dev, {held_n} held_out")
    if PHASE3_CACHE.exists():
        p3 = json.loads(PHASE3_CACHE.read_text(encoding="utf-8"))
        _print_phase3_summary(p3, mode=args.mode)
        if args.mode == "final":
            _run_unseen_random_eval(p3, seed=args.seed, n_workloads=args.random_n, mode=args.mode)
    else:
        print("  No cached Phase 3 benchmark yet (run `python scripts/run_phase3_baselines.py`).")

    return 0 if p1["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
