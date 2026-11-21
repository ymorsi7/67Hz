"""Baseline B2: stock Antigravity agent, same document, no skills/hooks/functions/checker."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any

from agents.client import get_genai_client, load_managed_config
from evolve.loop import extract_python_code
from evolve.sandbox import CandidateEval, evaluate_controller_sandboxed
from rules.schema import RuleSet
from workload.synth import WorkloadParams

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class B2Result:
    baseline: str
    model: str
    code: str
    eval_result: CandidateEval | None
    elapsed_s: float
    notes: str
    interaction_id: str | None = None
    raw_text: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline,
            "model": self.model,
            "code": self.code,
            "eval_result": self.eval_result.to_dict() if self.eval_result else None,
            "elapsed_s": round(self.elapsed_s, 3),
            "notes": self.notes,
            "interaction_id": self.interaction_id,
            "raw_text": (self.raw_text or "")[:2000],
        }


def run_b2_baseline(
    ruleset: RuleSet,
    workloads: dict[str, WorkloadParams],
    *,
    pdf_path: str | Path | None = None,
    timeout_s: float = 300.0,
) -> B2Result:
    """Stock antigravity-preview-09-2026, same document, no skills/hooks/functions/checker."""
    cfg = load_managed_config()
    client = get_genai_client()
    t0 = time.perf_counter()

    constraint_summary = "\n".join(
        f"- {c.constraint_id}: {c.type} value={c.value} {c.unit} page={c.page}"
        for c in ruleset.constraints
        if c.checkable
    )
    prompt = (
        "Write a Python controller for an AI training cluster battery+GPU plant.\n"
        "Return ONLY a python code block with: def control(obs: dict) -> dict[str, float]\n"
        "Actions: gpu_floor_mw, gpu_cap_mw, battery_setpoint_mw. Pure numpy/math only.\n"
        f"Utility ruleset ({ruleset.source_name}):\n{constraint_summary}\n"
    )
    if pdf_path is not None:
        prompt += f"\nDocument path (for context only; you may not have the PDF bytes): {pdf_path}\n"

    interaction = client.interactions.create(
        agent=cfg["base_agent"],
        agent_config={"type": "antigravity", "model": cfg["engineer_model"]},
        input=prompt,
        # Intentionally NO skills, hooks, custom functions, or checker.
        tools=[{"type": "code_execution"}],
        environment="remote",
        timeout=timeout_s,
    )
    text = interaction.output_text or ""
    code = extract_python_code(text) or text
    elapsed = time.perf_counter() - t0
    # Host scores with checker AFTER the fact (baseline itself has no checker).
    try:
        ev = evaluate_controller_sandboxed(code, ruleset, workloads, timeout_s=30.0)
        notes = "Stock Antigravity; host scored after the fact with deterministic checker."
    except Exception as exc:
        ev = None
        notes = f"Stock Antigravity; host scoring failed: {type(exc).__name__}: {exc}"

    return B2Result(
        baseline="B2",
        model=f"{cfg['base_agent']}/{cfg['engineer_model']}",
        code=code,
        eval_result=ev,
        elapsed_s=elapsed,
        notes=notes,
        interaction_id=getattr(interaction, "id", None),
        raw_text=text,
    )
