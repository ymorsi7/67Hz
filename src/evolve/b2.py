"""Baseline B2: stock Antigravity agent, same document, no skills/hooks/functions/checker."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from repo_paths import find_repo_root
import time
from typing import Any

from agents.client import get_genai_client, load_managed_config
from evolve.loop import extract_python_code
from evolve.sandbox import CandidateEval, evaluate_controller_sandboxed
from rules.schema import RuleSet
from sim.plant import PlantConfig
from workload.synth import WorkloadParams

REPO_ROOT = find_repo_root()
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
    prompt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline,
            "model": self.model,
            "code": self.code,
            "eval_result": self.eval_result.to_dict() if self.eval_result else None,
            "elapsed_s": round(self.elapsed_s, 3),
            "notes": self.notes,
            "interaction_id": self.interaction_id,
            "raw_text": (self.raw_text or "")[:4000],
            "prompt": self.prompt,
        }


def _b2_task_prompt(ruleset: RuleSet, *, pdf_path: str | Path | None, utility_hint: str) -> str:
    """Match B0's controller interface description so harness failures are not the comparison.

    Still NO skills/hooks/functions/checker in the agent — only a fair task description.
    """
    b = PlantConfig().battery
    constraint_summary = "\n".join(
        f"- {c.id or c.constraint_type}: {c.constraint_type} value={c.value} {c.unit} page={c.page}"
        + (
            f" band=[{c.freq_min_hz},{c.freq_max_hz}] agg={getattr(c, 'aggregation', 'peak_bin')}"
            if c.constraint_type == "fluctuation_band"
            else ""
        )
        + (f" window_s={c.window_s}" if c.window_s is not None else "")
        for c in ruleset.constraints
        if c.checkable
    )
    prompt = (
        f"Write ONE Python power-smoothing controller "
        f"`def control(obs: dict) -> dict[str, float]:` for a {ruleset.rated_mw:.0f} MW AI training "
        f"cluster complying with {utility_hint}'s interconnection power limits.\n\n"
        "Interface (must match exactly — host sandbox evaluates this):\n"
        "- Input `obs` dict keys: t, dt (varies: 0.002s on fast 20s traces and 0.5s on slow 30-min traces), "
        "step, p_raw_mw, p_prev_grid_mw, idle_mw, rated_mw, soc, battery_energy_mwh, battery_capacity_mwh, "
        "battery_max_power_mw, history_grid_mw, history_raw_mw, state (mutable dict persisted across steps).\n"
        "- Initial condition: every simulation starts at steady idle (`obs['idle_mw']`); jumps at step 0 count "
        "against ramp and step limits.\n"
        f"- Battery: max_power={b.max_power_mw:.1f} MW, capacity={b.capacity_mwh:.1f} MWh, SOC in [0.10, 0.90], "
        "~90% round-trip. Positive battery_setpoint_mw discharges (reduces grid draw); negative charges.\n"
        "- Return ONLY: {'gpu_floor_mw': float, 'gpu_cap_mw': float, 'battery_setpoint_mw': float}.\n"
        "- Use only `math` and `numpy as np`. Keep under 60 lines.\n"
        "- Do NOT use getattr, hasattr, eval, exec, open, or any imports besides math/numpy.\n"
        "- Access obs only with obs['key'] or obs.get('key', default). Use obs['state'] for persistence.\n"
        "- Return ONLY one Python code block (no prose).\n\n"
        f"Utility ruleset ({ruleset.source_name}):\n{constraint_summary}\n"
    )
    if pdf_path is not None:
        prompt += f"\nDocument path (context only; you may not have PDF bytes): {pdf_path}\n"
    return prompt


def run_b2_baseline(
    ruleset: RuleSet,
    workloads: dict[str, WorkloadParams],
    *,
    pdf_path: str | Path | None = None,
    timeout_s: float = 300.0,
    utility_name: str | None = None,
) -> B2Result:
    """Stock antigravity-preview-09-2026, same document, no skills/hooks/functions/checker."""
    cfg = load_managed_config()
    client = get_genai_client()
    t0 = time.perf_counter()

    utility_hint = utility_name or ruleset.source_name or "the utility"
    prompt = _b2_task_prompt(ruleset, pdf_path=pdf_path, utility_hint=utility_hint)

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
        notes = (
            "Stock Antigravity; host scored after the fact with deterministic checker. "
            "Task description aligned with B0 interface + sandbox bans (no skills/hooks/checker)."
        )
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
        prompt=prompt,
    )
