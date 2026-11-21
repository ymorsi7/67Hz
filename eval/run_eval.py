"""GridSafe evaluation entrypoint (Phase 1 physics gates + expanded in Phases 2-7)."""

from __future__ import annotations

import argparse
import sys
import time

import numpy as np

from eval.extraction import evaluate_extraction
from rules.checker import check_compliance
from rules.schema import Constraint, RuleSet
from sim.plant import fixed_floor_controller, noop_controller, simulate_plant
from sim.shaft import load_shaft_model
from workload.synth import generate_workload, preset_workloads


def run_phase1_validation() -> dict:
    """Execute deterministic Phase 1 physics and checker validation gates."""
    shaft = load_shaft_model()
    mode_report = shaft.validate_modes(max_error_pct=2.0)

    presets = preset_workloads(cluster_mw=100.0)
    ruleset = RuleSet(
        source_name="phase1_gate_ruleset",
        rated_mw=100.0,
        constraints=[
            Constraint("ramp_rate", 30.0, "MW/min", page=1, snippet="30 MW/min", window_s=1.0),
            Constraint("max_step", 10.0, "MW", page=1, snippet="10 MW", window_s=1.0),
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

        # Also exercise an active charge/discharge battery controller so energy ledger is non-trivial
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
                "eval_time_s": elapsed,
                "raw_passed": rep_raw.passed,
                "raw_max_torque_pu": sim_raw.shaft.max_torque_pu,
                "b1_passed": rep_b1.passed,
                "b1_max_torque_pu": sim_b1.shaft.max_torque_pu,
                "b1_energy_overhead_pct": sim_b1.metrics.energy_overhead_pct,
            }
        )

    max_eval_s = max(timings_s)
    all_gates_pass = bool(mode_report["passed"] and max_eval_s < 0.5 and max_energy_err < 1e-12)
    return {
        "passed": all_gates_pass,
        "mode_report": mode_report,
        "max_eval_time_s": max_eval_s,
        "mean_eval_time_s": float(np.mean(timings_s)),
        "max_energy_balance_error_mwh": max_energy_err,
        "presets": preset_summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="GridSafe evaluation harness")
    parser.add_argument("--mode", choices=["dev", "final"], default="dev")
    args = parser.parse_args()

    p1 = run_phase1_validation()
    mr = p1["mode_report"]
    print(f"[eval:{args.mode}] Phase 1 Physics & Checker Gates:")
    print(
        f"  IEEE FBM modes: {'PASS' if mr['passed'] else 'FAIL'} "
        f"(max error = {mr['max_error_pct']:.4f}% <= {mr['threshold_pct']:.1f}%)"
    )
    print(
        f"  Eval speed:     {'PASS' if p1['max_eval_time_s'] < 0.5 else 'FAIL'} "
        f"(mean = {p1['mean_eval_time_s']*1000:.1f} ms, max = {p1['max_eval_time_s']*1000:.1f} ms < 500 ms)"
    )
    print(
        f"  Energy ledger:  {'PASS' if p1['max_energy_balance_error_mwh'] < 1e-12 else 'FAIL'} "
        f"(max residual = {p1['max_energy_balance_error_mwh']:.3e} MWh)"
    )
    for row in p1["presets"]:
        print(
            f"  Preset {row['preset']:24s} | {row['eval_time_s']*1000:5.1f} ms "
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

    return 0 if p1["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
