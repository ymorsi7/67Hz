"""Generate docs/VALIDATION.md directly from deterministic Phase 1 runs."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

import numpy as np

from eval.run_eval import run_phase1_validation
from sim.shaft import DEFAULT_MODAL_DAMPING_RATIO, load_shaft_model

OUT_PATH = REPO_ROOT / "docs" / "VALIDATION.md"


def main() -> None:
    shaft = load_shaft_model()
    p1 = run_phase1_validation()
    mr = p1["mode_report"]

    lines: list[str] = [
        "# GridSafe Physics & Checker Validation",
        "",
        f"Generated from `scripts/generate_validation_doc.py` on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.",
        "All numbers below come directly from deterministic runs of `sim/shaft.py`, `sim/plant.py`, `workload/synth.py`, and `rules/checker.py` (pure Python + `numpy`, no `scipy`).",
        "",
        "## 1. IEEE First Benchmark Model Torsional Mode Validation",
        "",
        "Source parameters: `config/fbm.yaml` (IEEE SSR Task Force, *First benchmark model for computer simulation of subsynchronous resonance*, IEEE Trans. PAS-96, no. 5, pp. 1565–1572, 1977).",
        "",
        f"- Base MVA: `{shaft.base_mva}` MVA",
        f"- Base frequency: `{shaft.base_frequency_hz}` Hz (`w0 = 2 * pi * 60` rad/s)",
        f"- Masses (`6`): `{', '.join(shaft.masses)}`",
        f"- Shaft sections (`5`): `{', '.join(shaft.section_names)}`",
        "- Generalized eigenproblem: `M = diag(2 H / w0)`, symmetric transform `A = M^{-1/2} K M^{-1/2}`, solved with `numpy.linalg.eigh`.",
        f"- Rigid-body mode frequency: `{shaft.freqs_all_hz[0]:.6f}` Hz",
        "",
        "| Mode | Computed (Hz) | Published (Hz) | Error (Hz) | Error (%) | Dominant Section | `phi_GEN` | Gate (<= 2.0%) |",
        "| --- | ---: | ---: | ---: | ---: | --- | ---: | --- |",
    ]

    dom_sections = shaft.modal_dominant_sections()
    for idx, row in enumerate(mr["modes"]):
        dom_sec, dom_share_pct = dom_sections[idx]
        phi_gen = float(shaft.phi_gen[idx])
        gate_str = "PASS" if row["within_gate"] else "FAIL"
        lines.append(
            f"| Mode {row['mode']} | {row['computed_hz']:.4f} | {row['published_hz']:.2f} "
            f"| {row['error_hz']:+.4f} | {row['error_pct']:.4f}% | `{dom_sec}` ({dom_share_pct:.1f}% strain E) | {phi_gen:+.4f} | **{gate_str}** |"
        )

    lines.extend(
        [
            "",
            f"**Max relative error:** `{mr['max_error_pct']:.4f}%` (gate: `<= {mr['threshold_pct']:.1f}%`) -> **{'PASS' if mr['passed'] else 'FAIL'}**.",
            "",
            "## 2. Plant Simulation, Energy Conservation & Latency Gates",
            "",
            f"- Evaluation latency gate (`< 500 ms` per full synth + time-stepped plant + frequency-domain shaft + checker evaluation):",
            f"  - Mean: `{p1['mean_eval_time_s'] * 1000:.1f} ms`",
            f"  - Max: `{p1['max_eval_time_s'] * 1000:.1f} ms` -> **{'PASS' if p1['max_eval_time_s'] < 0.5 else 'FAIL'}**",
            f"- Energy conservation ledger relative residual (`|E_grid - (E_final - E_init) - (E_gpu + E_batt_loss)| / E_throughput`):",
            f"  - Max relative residual across all fast (20 s) + slow (30 min) presets: `{p1['max_energy_balance_rel_error']:.3e}` (gate `< {p1['energy_ledger_rel_tol']:.0e}`; abs max `{p1['max_energy_balance_error_mwh']:.3e} MWh`) -> **{'PASS' if p1['max_energy_balance_rel_error'] < p1['energy_ledger_rel_tol'] else 'FAIL'}**",
            "",
            "### Preset Workload Benchmarks (100 MW cluster, fast 20 s @ 2 ms & slow 30 min @ 0.5 s)",
            "",
            "| Preset | Eval Time (ms) | Uncontrolled Peak Torque (pu) | B1 (90% Floor) Peak Torque (pu) | B1 Energy Overhead (%) |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )

    for row in p1["presets"]:
        lines.append(
            f"| `{row['preset']}` | {row['eval_time_s'] * 1000:.1f} | {row['raw_max_torque_pu']:.4f} "
            f"| {row['b1_max_torque_pu']:.4f} | {row['b1_energy_overhead_pct']:.1f}% |"
        )

    lines.extend(
        [
            "",
            "## 3. Modeling Simplifications & Assumptions (Disclosed)",
            "",
            f"- **Modal damping**: Linear modal damping ratio `zeta = {DEFAULT_MODAL_DAMPING_RATIO}` is assumed uniformly across torsional modes in the frequency-domain transfer function `H_m(w)`. Real turbine-generator units exhibit mode-dependent steam-damping and electrical network negative damping near series-compensated lines.",
            "- **Electrical coupling**: Net cluster load perturbation `dP_grid(t) / S_base` is applied directly as an electrical torque perturbation at the generator bus (`coupling_factor = 1.0` default) without full AC network impedance or sub-transient flux dynamics.",
            "- **Battery model**: Idealized power-limited, SOC-limited energy storage with symmetric charge/discharge efficiency (`sqrt(0.90)` each way for 90% round-trip) and a C-rate-weighted Equivalent Full Cycles (EFC) wear proxy rather than an electrochemical degradation model.",
            "- **Pure Python + `numpy` sandbox compatibility**: `workload/`, `sim/`, and `rules/` have zero `scipy` or `PyMuPDF` imports (verified by automated AST unit test) so the managed agent sandbox runs the exact same physics and checker code as the backend.",
            "",
        ]
    )

    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
