"""Deterministic and LLM baselines (Uncontrolled, B0 Neon single-shot, B1 90% floor).

All baselines are scored by the exact same sandboxed deterministic checker
(`evolve.sandbox.evaluate_controller_sandboxed`) on the exact same profiles and
workloads as GridSafe Designer. Pure Python + numpy (+ optional google-genai for B0).
"""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import os
from pathlib import Path
import time
from typing import Any

from evolve.loop import _load_dotenv_if_needed, extract_python_code
from evolve.sandbox import CandidateEval, evaluate_controller_sandboxed
from rules.schema import RuleSet
from sim.plant import PlantConfig
from workload.synth import WorkloadParams

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
    baseline: str  # "uncontrolled" | "B0" | "B1"
    model: str
    code: str
    eval_result: CandidateEval
    elapsed_s: float
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline,
            "model": self.model,
            "code": self.code,
            "eval_result": self.eval_result.to_dict(),
            "elapsed_s": round(self.elapsed_s, 3),
            "notes": self.notes,
        }


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
    pdf_path: str | Path | None = None,
    pages: list[int] | None = None,
    plant_config: PlantConfig | None = None,
    model: str = "gemini-4-neon",
    thinking_level: str = "medium",
    strict_review: bool = True,
) -> BaselineResult:
    """Baseline B0: one single Gemini 4 Neon call — document in, controller out (no checker loop)."""
    _load_dotenv_if_needed()
    from google import genai

    t0 = time.perf_counter()
    cfg = plant_config if plant_config is not None else PlantConfig()
    b = cfg.battery

    prompt_text = (
        f"You are given a utility large-load interconnection document. Write ONE Python power-smoothing "
        f"controller `def control(obs: dict) -> dict[str, float]:` for a {ruleset.rated_mw:.0f} MW AI training "
        f"cluster complying with {utility_name}'s interconnection power limits stated in this document.\n\n"
        "Interface:\n"
        "- Input `obs` dict keys: t, dt (0.002s), step, p_raw_mw, p_prev_grid_mw, rated_mw, soc, "
        "battery_energy_mwh, battery_capacity_mwh, battery_max_power_mw, history_grid_mw, history_raw_mw, state (dict).\n"
        f"- Battery specs: max_power={b.max_power_mw:.1f} MW, capacity={b.capacity_mwh:.1f} MWh, "
        "SOC in [0.10, 0.90], ~90% round-trip efficiency. Positive battery_setpoint_mw discharges (reduces grid draw); "
        "negative charges (increases grid draw).\n"
        "- Return dict: {'gpu_floor_mw': float, 'gpu_cap_mw': float, 'battery_setpoint_mw': float}.\n"
        "- Use only `math` and `numpy as np`. Keep under 60 lines. Return ONLY the Python code block."
    )

    input_parts: list[dict[str, Any]] = []
    notes = f"Single-shot {model} (no checker feedback)"

    if pdf_path is not None and Path(pdf_path).exists():
        pdf_bytes = Path(pdf_path).read_bytes()
        pdf_b64 = base64.b64encode(pdf_bytes).decode("ascii")
        doc_part: dict[str, Any] = {
            "type": "document",
            "data": pdf_b64,
            "mime_type": "application/pdf",
            "resolution": "medium",
        }
        if pages:
            start_p = max(1, min(pages) - 1)
            end_p = max(pages) + 1
            doc_part["processing"] = {
                "type": "static",
                "page_options": {
                    "start_page": start_p,
                    "end_page": end_p,
                    "content_layers": ["page_image", "page_text"],
                },
            }
            notes += f" on {Path(pdf_path).name} pp.{start_p}-{end_p}"
        else:
            notes += f" on {Path(pdf_path).name}"
        input_parts.append(doc_part)
    else:
        # Text-only fallback if PDF not provided
        snippets = "\n".join(f"- p.{c.page}: {c.snippet}" for c in ruleset.constraints)
        prompt_text += f"\n\nDocument excerpts:\n{snippets}"
        notes += " on text excerpts"

    input_parts.append({"type": "text", "text": prompt_text})

    client = genai.Client(http_options={"api_version": "v1beta"})
    try:
        interaction = client.interactions.create(
            model=model,
            input=input_parts,
            generation_config={"thinking_level": thinking_level},
        )
        code = extract_python_code(interaction.output_text or "")
    except Exception as exc:
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
            error=f"B0 API error: {type(exc).__name__}: {exc}",
            elapsed_s=time.perf_counter() - t0,
        )
        return BaselineResult("B0", model, "", ev, time.perf_counter() - t0, notes)

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
    )
