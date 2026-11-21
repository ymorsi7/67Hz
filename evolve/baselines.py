"""Deterministic, random-search, and single-shot LLM baselines (Noop, B1, B0, B2, B3).

- Uncontrolled (Noop): zero mitigation.
- B1: fixed 90% GPU power floor, no battery.
- B0: end-to-end single-shot Gemini 4 Neon — FULL raw PDF in, controller out,
  one Neon call, NO Reader page slicing, NO verified constraints, NO checker loop.
- B2: stock Antigravity agent, same document, no skills/hooks/functions/checker
  (implemented in evolve.b2; host scores after the fact).
- B3: low-pass filter + battery policy tuned by seeded random search with the
  exact same number of simulator evaluations GridSafe used. Zero LLM calls.

All baselines are scored by the exact same sandboxed deterministic checker
(`evolve.sandbox.evaluate_controller_sandboxed`) on the exact same profiles and
workloads as GridSafe Designer. Pure Python + numpy (+ optional google-genai for B0/B2).
"""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import math
from pathlib import Path
import re
import time
from typing import Any

import numpy as np

from evolve.loop import _load_dotenv_if_needed, extract_python_code
from evolve.sandbox import (
    CandidateEval,
    aggregate_per_workload_results,
    evaluate_controller_sandboxed,
)
from rules.checker import check_compliance
from rules.schema import RuleSet
from sim.plant import PlantConfig, simulate_plant
from sim.shaft import load_shaft_model
from workload.synth import WorkloadParams, generate_workload

NOOP_CONTROLLER_CODE = '''"""Uncontrolled baseline: no GPU floor, no cap, no battery."""

def control(obs: dict) -> dict[str, float]:
    return {
        "gpu_floor_mw": 0.0,
        "gpu_cap_mw": float(obs["rated_mw"]),
        "battery_setpoint_mw": 0.0,
    }
'''

B1_FIXED_FLOOR_90_CODE = '''"""Baseline B1: fixed 90% GPU power floor, no battery smoothing."""

def control(obs: dict) -> dict[str, float]:
    rated = float(obs["rated_mw"])
    return {
        "gpu_floor_mw": 0.90 * rated,
        "gpu_cap_mw": rated,
        "battery_setpoint_mw": 0.0,
    }
'''


@dataclass
class BaselineResult:
    baseline: str  # "uncontrolled" | "B0" | "B1" | "B3"
    model: str
    code: str
    eval_result: CandidateEval
    elapsed_s: float
    notes: str = ""
    seed: int | None = None
    n_evals: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline,
            "model": self.model,
            "seed": self.seed,
            "n_evals": self.n_evals,
            "code": self.code,
            "eval_result": self.eval_result.to_dict(),
            "elapsed_s": round(self.elapsed_s, 3),
            "notes": self.notes,
        }


def resolve_pdf_path(pdf_path: str | Path | None) -> Path | None:
    """Resolve a PDF path, handling arXiv versioned filenames (e.g. 2601.12686v1.pdf -> 2601.12686.pdf)."""
    if pdf_path is None:
        return None
    p = Path(pdf_path)
    if p.exists():
        return p
    stripped = re.sub(r"v\d+(?=\.pdf$)", "", p.name, flags=re.IGNORECASE)
    alt = p.parent / stripped
    if alt.exists():
        return alt
    return None


def run_uncontrolled_baseline(
    ruleset: RuleSet,
    workloads: dict[str, WorkloadParams],
    *,
    plant_config: PlantConfig | None = None,
    strict_review: bool = True,
) -> BaselineResult:
    """Evaluate uncontrolled cluster (no mitigation) on the exact same checker."""
    t0 = time.perf_counter()
    ev = evaluate_controller_sandboxed(
        NOOP_CONTROLLER_CODE,
        ruleset,
        workloads,
        plant_config=plant_config,
        strict_review=strict_review,
    )
    return BaselineResult(
        baseline="uncontrolled",
        model="none",
        code=NOOP_CONTROLLER_CODE,
        eval_result=ev,
        elapsed_s=time.perf_counter() - t0,
        notes="No mitigation (gpu_floor=0, gpu_cap=rated_mw, battery=0).",
    )


def run_b1_baseline(
    ruleset: RuleSet,
    workloads: dict[str, WorkloadParams],
    *,
    plant_config: PlantConfig | None = None,
    strict_review: bool = True,
) -> BaselineResult:
    """Evaluate Baseline B1 (fixed 90% GPU power floor) on the exact same checker."""
    t0 = time.perf_counter()
    ev = evaluate_controller_sandboxed(
        B1_FIXED_FLOOR_90_CODE,
        ruleset,
        workloads,
        plant_config=plant_config,
        strict_review=strict_review,
    )
    return BaselineResult(
        baseline="B1",
        model="fixed_90pct_floor",
        code=B1_FIXED_FLOOR_90_CODE,
        eval_result=ev,
        elapsed_s=time.perf_counter() - t0,
        notes="Baseline B1: fixed 90% GPU power floor, no battery control.",
    )


def run_b0_baseline(
    ruleset: RuleSet,
    workloads: dict[str, WorkloadParams],
    *,
    utility_name: str,
    pdf_path: str | Path,
    plant_config: PlantConfig | None = None,
    model: str = "gemini-4-neon",
    thinking_level: str = "medium",
    strict_review: bool = True,
    seed: int | None = None,
) -> BaselineResult:
    """Baseline B0: true end-to-end single-shot Gemini 4 Neon — FULL PDF in, controller out.

    Zero Reader page slicing, zero verified constraints, zero checker feedback.
    """
    _load_dotenv_if_needed()
    from google import genai

    t0 = time.perf_counter()
    resolved = resolve_pdf_path(pdf_path)
    if resolved is None:
        raise FileNotFoundError(
            f"B0 requires the raw PDF for end-to-end document-in evaluation, but '{pdf_path}' was not found. "
            "Run `make fetch-specs` first."
        )

    cfg = plant_config if plant_config is not None else PlantConfig()
    b = cfg.battery

    prompt_text = (
        f"You are given a complete utility large-load interconnection document (`{resolved.name}`). "
        f"Read the document and write ONE Python power-smoothing controller "
        f"`def control(obs: dict) -> dict[str, float]:` for a {ruleset.rated_mw:.0f} MW AI training "
        f"cluster complying with {utility_name}'s interconnection power limits stated in this document.\n\n"
        "Interface:\n"
        "- Input `obs` dict keys: t, dt (varies: 0.002s on fast 20s traces and 0.5s on slow 30-min traces), "
        "step, p_raw_mw, p_prev_grid_mw, idle_mw, rated_mw, soc, battery_energy_mwh, battery_capacity_mwh, "
        "battery_max_power_mw, history_grid_mw, history_raw_mw, state (mutable dict persisted across steps).\n"
        "- Initial condition: every simulation starts with the facility at steady idle power (`obs['idle_mw']`), "
        "which is prepended prior to t=0 so any jump at step 0 counts against ramp and step limits.\n"
        f"- Battery specs: max_power={b.max_power_mw:.1f} MW, capacity={b.capacity_mwh:.1f} MWh, "
        "SOC in [0.10, 0.90], ~90% round-trip efficiency. Positive battery_setpoint_mw discharges (reduces grid draw); "
        "negative charges (increases grid draw).\n"
        "- Return dict: {'gpu_floor_mw': float, 'gpu_cap_mw': float, 'battery_setpoint_mw': float}.\n"
        "- Use only `math` and `numpy as np`. Keep under 60 lines. Return ONLY the Python code block."
    )

    pdf_bytes = resolved.read_bytes()
    pdf_b64 = base64.b64encode(pdf_bytes).decode("ascii")
    doc_part: dict[str, Any] = {
        "type": "document",
        "data": pdf_b64,
        "mime_type": "application/pdf",
        "resolution": "medium",
    }
    notes = f"End-to-end full-PDF single-shot {model} on {resolved.name} (no page hints, no rules, no checker)"
    input_parts: list[dict[str, Any]] = [doc_part, {"type": "text", "text": prompt_text}]

    gen_cfg: dict[str, Any] = {"thinking_level": thinking_level}
    if seed is not None:
        gen_cfg["seed"] = int(seed)

    client = genai.Client(http_options={"api_version": "v1beta"})
    code = ""
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            interaction = client.interactions.create(
                model=model,
                input=input_parts,
                generation_config=gen_cfg,
            )
            code = extract_python_code(interaction.output_text or "")
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(2.0)

    if last_exc is not None:
        ev = CandidateEval(
            passed=False,
            workloads_passed=0,
            workloads_total=len(workloads),
            composite_score=1e9,
            mean_energy_overhead_pct=0.0,
            mean_throughput_loss_pct=0.0,
            mean_battery_wear=0.0,
            max_shaft_torque_pu=0.0,
            worst_margin_pct=None,
            worst_constraint_id=None,
            error=f"B0 API error: {type(last_exc).__name__}: {last_exc}",
            elapsed_s=time.perf_counter() - t0,
        )
        return BaselineResult("B0", model, "", ev, time.perf_counter() - t0, notes, seed=seed, n_evals=1)

    ev = evaluate_controller_sandboxed(
        code,
        ruleset,
        workloads,
        plant_config=plant_config,
        strict_review=strict_review,
    )
    return BaselineResult(
        baseline="B0",
        model=model,
        code=code,
        eval_result=ev,
        elapsed_s=time.perf_counter() - t0,
        notes=notes,
        seed=seed,
        n_evals=1,
    )


def _render_b3_code(params: dict[str, Any]) -> str:
    """Render a clean pure-Python low-pass filter + battery controller (< 55 lines)."""
    return f'''"""Baseline B3: low-pass filter + battery policy tuned by random search (no LLM)."""
import math

TAU_S = {params["tau_s"]:.6f}
ORDER = {int(params["filter_order"])}
SLEW_MW_S = {params["slew_mw_s"]:.6f}
SOC_KP = {params["soc_kp"]:.6f}
SOC_TARGET = {params["soc_target"]:.6f}
PRELOAD_MW = {params["idle_preload_mw"]:.6f}
EXACT_ACT = {bool(params["use_exact_actuator"])}

def control(obs: dict) -> dict[str, float]:
    p_raw = float(obs["p_raw_mw"])
    rated = float(obs["rated_mw"])
    dt = float(obs["dt"])
    soc = float(obs["soc"])
    b_max = float(obs["battery_max_power_mw"])
    cap = float(obs["battery_capacity_mwh"])
    e = float(obs["battery_energy_mwh"])
    idle = float(obs.get("idle_mw", obs["p_prev_grid_mw"]))
    st = obs["state"]

    if "y1" not in st:
        st["y1"] = idle
        st["y2"] = idle
        st["pg"] = idle

    desired_in = p_raw + (PRELOAD_MW if (p_raw <= idle + 2.0 and soc < 0.85) else 0.0)
    desired_in = min(rated, max(0.0, desired_in + SOC_KP * (SOC_TARGET - soc) * b_max))
    alpha = dt / (TAU_S + dt)
    st["y1"] = (1.0 - alpha) * st["y1"] + alpha * desired_in
    if ORDER >= 2:
        st["y2"] = (1.0 - alpha) * st["y2"] + alpha * st["y1"]
        filt = st["y2"]
    else:
        filt = st["y1"]

    max_d = SLEW_MW_S * dt
    p_target = st["pg"] + max(-max_d, min(max_d, filt - st["pg"]))
    p_target = min(rated, max(0.0, p_target))
    st["pg"] = p_target

    if not EXACT_ACT:
        batt = max(-b_max, min(b_max, p_raw - p_target))
        return {{"gpu_floor_mw": 0.0, "gpu_cap_mw": rated, "battery_setpoint_mw": batt}}

    dt_h = dt / 3600.0
    eta = math.sqrt(0.90)
    b_dis_max = min(b_max, max(0.0, (e - 0.1 * cap) * eta / dt_h))
    b_chg_max = min(b_max, max(0.0, (0.9 * cap - e) / (eta * dt_h)))
    if p_target < p_raw - b_dis_max:
        return {{"gpu_floor_mw": 0.0, "gpu_cap_mw": p_target + b_dis_max, "battery_setpoint_mw": b_dis_max}}
    if p_target > p_raw + b_chg_max:
        return {{"gpu_floor_mw": p_target - b_chg_max, "gpu_cap_mw": rated, "battery_setpoint_mw": -b_chg_max}}
    return {{"gpu_floor_mw": 0.0, "gpu_cap_mw": rated, "battery_setpoint_mw": p_raw - p_target}}
'''


def _sample_b3_params(rng: np.random.Generator) -> dict[str, Any]:
    use_slew = bool(rng.random() < 0.80)
    use_preload = bool(rng.random() < 0.50)
    return {
        "tau_s": float(math.exp(rng.uniform(math.log(0.2), math.log(600.0)))),
        "filter_order": int(rng.choice([1, 2])),
        "slew_mw_s": float(math.exp(rng.uniform(math.log(0.08), math.log(6.0)))) if use_slew else 1e6,
        "soc_kp": float(rng.uniform(0.0, 1.0)),
        "soc_target": float(rng.uniform(0.45, 0.75)),
        "idle_preload_mw": float(rng.uniform(5.0, 40.0)) if use_preload else 0.0,
        "use_exact_actuator": bool(rng.random() < 0.75),
    }


def _make_b3_callable(params: dict[str, Any]):
    tau_s = float(params["tau_s"])
    order = int(params["filter_order"])
    slew_mw_s = float(params["slew_mw_s"])
    soc_kp = float(params["soc_kp"])
    soc_target = float(params["soc_target"])
    preload_mw = float(params["idle_preload_mw"])
    exact_act = bool(params["use_exact_actuator"])
    eta = math.sqrt(0.90)

    def _control(obs: dict[str, Any]) -> dict[str, float]:
        p_raw = float(obs["p_raw_mw"])
        rated = float(obs["rated_mw"])
        dt = float(obs["dt"])
        soc = float(obs["soc"])
        b_max = float(obs["battery_max_power_mw"])
        cap = float(obs["battery_capacity_mwh"])
        e = float(obs["battery_energy_mwh"])
        idle = float(obs.get("idle_mw", obs["p_prev_grid_mw"]))
        st = obs["state"]

        if "y1" not in st:
            st["y1"] = idle
            st["y2"] = idle
            st["pg"] = idle

        desired_in = p_raw + (preload_mw if (p_raw <= idle + 2.0 and soc < 0.85) else 0.0)
        desired_in = min(rated, max(0.0, desired_in + soc_kp * (soc_target - soc) * b_max))
        alpha = dt / (tau_s + dt)
        st["y1"] = (1.0 - alpha) * st["y1"] + alpha * desired_in
        if order >= 2:
            st["y2"] = (1.0 - alpha) * st["y2"] + alpha * st["y1"]
            filt = st["y2"]
        else:
            filt = st["y1"]

        max_d = slew_mw_s * dt
        p_target = st["pg"] + max(-max_d, min(max_d, filt - st["pg"]))
        p_target = min(rated, max(0.0, p_target))
        st["pg"] = p_target

        if not exact_act:
            batt = max(-b_max, min(b_max, p_raw - p_target))
            return {"gpu_floor_mw": 0.0, "gpu_cap_mw": rated, "battery_setpoint_mw": batt}

        dt_h = dt / 3600.0
        b_dis_max = min(b_max, max(0.0, (e - 0.1 * cap) * eta / dt_h))
        b_chg_max = min(b_max, max(0.0, (0.9 * cap - e) / (eta * dt_h)))
        if p_target < p_raw - b_dis_max:
            return {"gpu_floor_mw": 0.0, "gpu_cap_mw": p_target + b_dis_max, "battery_setpoint_mw": b_dis_max}
        if p_target > p_raw + b_chg_max:
            return {"gpu_floor_mw": p_target - b_chg_max, "gpu_cap_mw": rated, "battery_setpoint_mw": -b_chg_max}
        return {"gpu_floor_mw": 0.0, "gpu_cap_mw": rated, "battery_setpoint_mw": p_raw - p_target}

    return _control


def run_b3_baseline(
    ruleset: RuleSet,
    workloads: dict[str, WorkloadParams],
    *,
    n_evals: int = 19,
    seed: int = 42,
    plant_config: PlantConfig | None = None,
    strict_review: bool = True,
) -> BaselineResult:
    """Baseline B3: low-pass filter + battery policy tuned by seeded random search (no LLM).

    Uses the exact same number of simulator evaluations (`n_evals` candidate suites)
    as GridSafe Designer, then verifies the best candidate in the exact same
    isolated subprocess sandbox.
    """
    t0 = time.perf_counter()
    n_trials = max(1, int(n_evals))
    rng = np.random.default_rng(int(seed))
    shaft = load_shaft_model()
    rules_payload = ruleset.to_dict()

    # Pre-generate deterministic traces once across trials (exact same traces as _runner.py)
    traces = {name: (params, generate_workload(params)) for name, params in workloads.items()}

    best_params: dict[str, Any] | None = None
    best_eval: CandidateEval | None = None

    for _ in range(n_trials):
        params = _sample_b3_params(rng)
        ctrl = _make_b3_callable(params)
        t_cand = time.perf_counter()
        per_wl: list[dict[str, Any]] = []
        for wl_name, (wl_params, trace) in traces.items():
            sim_res = simulate_plant(
                trace,
                controller=ctrl,
                plant_config=plant_config,
                shaft_model=shaft,
                rules_dict=rules_payload,
            )
            report = check_compliance(sim_res, ruleset, strict_review=strict_review)
            failing = [
                {
                    "workload": wl_name,
                    "family": wl_params.family,
                    "constraint_id": r.constraint_id,
                    "constraint_type": r.constraint_type,
                    "status": r.status,
                    "measured": r.measured,
                    "limit": r.limit,
                    "margin": r.margin,
                    "margin_pct": r.margin_pct,
                    "canonical_unit": r.canonical_unit,
                    "page": r.page,
                    "snippet": r.snippet,
                    "entity": r.entity,
                    "details": r.details,
                }
                for r in report.results
                if r.status in {"fail", "error", "needs_review"}
            ]
            per_wl.append(
                {
                    "workload": wl_name,
                    "family": wl_params.family,
                    "status": report.status,
                    "passed": report.passed,
                    "checkable_count": report.checkable_count,
                    "evaluable_count": report.evaluable_count,
                    "not_evaluable_count": report.not_evaluable_count,
                    "passed_count": report.passed_count,
                    "failed_count": report.failed_count,
                    "needs_review_count": report.needs_review_count,
                    "worst_constraint_id": report.worst_constraint_id,
                    "worst_margin_pct": report.worst_margin_pct,
                    "refusal_reason": report.refusal_reason,
                    "assumptions": list(report.assumptions),
                    "failing_constraints": failing,
                    "energy_overhead_pct": sim_res.metrics.energy_overhead_pct,
                    "throughput_loss_pct": sim_res.metrics.throughput_loss_pct,
                    "battery_wear_proxy": sim_res.metrics.battery_wear_proxy,
                    "battery_efc": sim_res.metrics.battery_efc,
                    "max_shaft_torque_pu": sim_res.metrics.max_shaft_torque_pu,
                    "energy_balance_error_mwh": sim_res.metrics.energy_balance_error_mwh,
                    "eval_time_s": sim_res.metrics.eval_time_s,
                    "results": [r.to_dict() for r in report.results],
                }
            )
        ev = aggregate_per_workload_results(
            per_wl,
            ruleset=ruleset,
            elapsed_s=time.perf_counter() - t_cand,
            strict_review=strict_review,
        )
        if best_eval is None or (ev.passed and not best_eval.passed) or (
            ev.passed == best_eval.passed and ev.composite_score < best_eval.composite_score - 1e-6
        ):
            best_eval = ev
            best_params = params

    assert best_params is not None
    best_code = _render_b3_code(best_params)
    # Final sandbox verification of winning code (identical path as B0/B1/GridSafe)
    verified_eval = evaluate_controller_sandboxed(
        best_code,
        ruleset,
        workloads,
        plant_config=plant_config,
        strict_review=strict_review,
    )
    notes = (
        f"B3 random search ({n_trials} evals, seed={seed}, no LLM): "
        f"order={best_params['filter_order']}, tau={best_params['tau_s']:.2f}s, "
        f"slew={best_params['slew_mw_s']:.2f}MW/s, preload={best_params['idle_preload_mw']:.1f}MW, "
        f"exact_act={best_params['use_exact_actuator']}"
    )
    return BaselineResult(
        baseline="B3",
        model="random_search_lpf_battery",
        code=best_code,
        eval_result=verified_eval,
        elapsed_s=time.perf_counter() - t0,
        notes=notes,
        seed=seed,
        n_evals=n_trials,
    )


def run_b2_baseline(
    ruleset: RuleSet,
    workloads: dict[str, WorkloadParams],
    *,
    pdf_path: str | Path | None = None,
    timeout_s: float = 300.0,
) -> BaselineResult:
    """Baseline B2: stock Antigravity, no skills/hooks/functions/checker (see evolve.b2)."""
    from evolve.b2 import run_b2_baseline as _run_b2

    res = _run_b2(ruleset, workloads, pdf_path=pdf_path, timeout_s=timeout_s)
    return BaselineResult(
        baseline="B2",
        model=res.model,
        code=res.code,
        eval_result=res.eval_result
        or CandidateEval(
            passed=False,
            workloads_passed=0,
            workloads_total=len(workloads),
            composite_score=1e9,
            mean_energy_overhead_pct=0.0,
            mean_throughput_loss_pct=0.0,
            mean_battery_wear=0.0,
            max_shaft_torque_pu=0.0,
            worst_margin_pct=None,
            worst_constraint_id=None,
            error=res.notes,
            elapsed_s=res.elapsed_s,
        ),
        elapsed_s=res.elapsed_s,
        notes=res.notes,
    )
