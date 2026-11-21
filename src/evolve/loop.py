"""GridSafe Designer evolution loop (parallel Gemini 4 Hydrogen mutations).

Pure Python + numpy (+ optional google-genai when calling Hydrogen). No scipy,
no PyMuPDF, no pydantic. Runs 6 to 8 parallel Hydrogen mutations per generation,
evaluates every candidate in an isolated subprocess sandbox against the
deterministic compliance checker (hard gate across fast and slow scenario
families with idle initial condition prepended), scores passing candidates on
energy overhead, throughput loss, battery wear, and shaft torque, feeds back
exact failing constraints and margins, streams structured progress events, and
guarantees a safe outcome: compliant evolved controller -> conservative fixed
floor (if it passes) -> explicit refusal with reason.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from dataclasses import asdict, dataclass, field
import difflib
import os
from pathlib import Path

from repo_paths import find_repo_root, resolve_engine_file
import random
import re
import time
from typing import Any, Callable

import yaml

from evolve.sandbox import CandidateEval, evaluate_controller_sandboxed
from rules.schema import RuleSet
from rules.units import UnitConversionError, normalize_constraint_limit
from sim.plant import PlantConfig
from workload.synth import WorkloadParams

REPO_ROOT = find_repo_root()
MODELS_YAML = REPO_ROOT / "config" / "models.yaml"
SEED_CONTROLLER_PATH = resolve_engine_file("controllers", "seed.py")

CONSERVATIVE_FLOOR_CODE = '''"""Conservative fixed-floor fallback controller (100% rated power floor)."""

def control(obs: dict) -> dict[str, float]:
    rated = float(obs["rated_mw"])
    return {
        "gpu_floor_mw": rated,
        "gpu_cap_mw": rated,
        "battery_setpoint_mw": 0.0,
    }
'''

MUTATION_STRATEGIES: list[tuple[str, str]] = [
    (
        "slew_battery_first",
        "Start p_target strictly at obs['idle_mw'] at step 0 (NEVER jump at t=0!). Update p_target "
        "using dt-aware rate limiting (<= 0.95*r_max*dt per step) toward p_raw; use exact battery headroom "
        "discharge/charge first, falling back to GPU cap (delay/curtailment) or GPU floor only when battery "
        "power or SOC headroom is exhausted.",
    ),
    (
        "preload_battery_charge",
        "Start p_target strictly at obs['idle_mw'] at step 0. During the idle prefix (when p_raw <= idle_mw + 2 MW "
        "and soc < 0.85), smoothly ramp p_target upward at the legal ramp rate by CHARGING the battery (avoiding "
        "wasted dummy GPU burn!) so grid power is pre-positioned higher when the job starts, minimizing throughput loss.",
    ),
    (
        "window_envelope_guard",
        "Start p_target strictly at obs['idle_mw'] at step 0. Enforce BOTH rolling window max_step limits "
        "(using a lightweight O(1) slew cap <= 0.95*step_limit/window_s OR time-indexed ring buffer) AND "
        "60s/600s ramp limits across both fast (dt=0.002s) and slow (dt=0.5s) traces with exact battery headroom.",
    ),
    (
        "spectral_notch_smooth",
        "Start p_target strictly at obs['idle_mw'] at step 0. Cascade a 2nd-order low-pass filter (tau >= 3s) "
        "with strict per-step slew clamping so frequency-domain ripples in forbidden bands (0.1-55 Hz) and IEEE FBM "
        "torsional modes stay far below limits while smoothly tracking slow load changes on 30-min traces.",
    ),
    (
        "soc_balanced_low_wear",
        "Start p_target strictly at obs['idle_mw'] at step 0. Track a heavily filtered demand target with strict "
        "ramp/step limits and add a gentle, rate-limited SOC restoring term toward 0.55 on 30-minute traces so "
        "battery stays ready for both job startup/peaks and checkpoint/shutdown drops with minimal wear.",
    ),
    (
        "high_margin_robust",
        "Start p_target strictly at obs['idle_mw'] at step 0. Prioritize passing ALL constraints across both "
        "fast (20s @ 0.002s) and slow (30min @ 0.5s) workloads with >= 15% safety margin (<= 85% of every limit) "
        "using exact battery-first headroom allocation.",
    ),
    (
        "fast_startup_step_budget",
        "Start p_target strictly at obs['idle_mw'] at step 0. When a profile has a 5s/6s max_step limit but no "
        "strict per-second ramp limit, use an exact rolling-window budget (tracking min/max of recent p_grid "
        "subsampled every ~0.1s) to ramp up faster within the legal step window and minimize throughput loss.",
    ),
    (
        "two_stage_adaptive",
        "Start p_target strictly at obs['idle_mw'] at step 0. Combine a 2nd-order EMA (dt-aware) with exact "
        "per-step slew limiting derived from the tightest ramp_rate and max_step/window_s rules, then solve "
        "exactly for (battery_setpoint_mw, gpu_floor_mw, gpu_cap_mw) using exact battery SOC headroom.",
    ),
]

EventCallback = Callable[[dict[str, Any]], None]
MutatorFn = Callable[[str, str, int, int], str]


@dataclass
class CandidateRecord:
    generation: int
    worker_id: int
    strategy: str
    code: str
    eval_result: CandidateEval
    diff_from_parent: str = ""
    api_time_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "worker_id": self.worker_id,
            "strategy": self.strategy,
            "code": self.code,
            "eval_result": self.eval_result.to_dict(),
            "diff_from_parent": self.diff_from_parent,
            "api_time_s": round(self.api_time_s, 3),
        }


@dataclass
class EvolutionResult:
    """Guaranteed outcome of GridSafe Designer evolution.

    Fallback ladder:
      1. evolved_compliant (LLM candidate passes and scores <= B3)
      2. fallback_b3 (tuned low-pass + battery policy passes)
      3. fallback_conservative_floor (100% fixed floor passes)
      4. refused (explicit refusal with reason)
    """

    outcome: str  # "evolved_compliant" | "fallback_b3" | "fallback_conservative_floor" | "refused"
    passed: bool
    model: str
    thinking_level: str
    best_code: str | None
    best_eval: CandidateEval | None
    seed_eval: CandidateEval
    fallback_eval: CandidateEval | None
    generations_completed: int
    total_candidates_evaluated: int
    total_candidates_passed: int
    total_static_rejected: int
    elapsed_s: float
    seed: int | None = None
    b3_eval: CandidateEval | None = None
    refusal_reason: str | None = None
    history: list[CandidateRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "passed": self.passed,
            "model": self.model,
            "thinking_level": self.thinking_level,
            "seed": self.seed,
            "best_code": self.best_code,
            "best_eval": self.best_eval.to_dict() if self.best_eval else None,
            "seed_eval": self.seed_eval.to_dict(),
            "b3_eval": self.b3_eval.to_dict() if self.b3_eval else None,
            "fallback_eval": self.fallback_eval.to_dict() if self.fallback_eval else None,
            "generations_completed": self.generations_completed,
            "total_candidates_evaluated": self.total_candidates_evaluated,
            "total_candidates_passed": self.total_candidates_passed,
            "total_static_rejected": self.total_static_rejected,
            "elapsed_s": round(self.elapsed_s, 3),
            "refusal_reason": self.refusal_reason,
            "history": [h.to_dict() for h in self.history],
        }


def load_designer_config(models_yaml: Path = MODELS_YAML) -> dict[str, str]:
    defaults = {"model": "gemini-4-hydrogen", "thinking_level": "low"}
    if not models_yaml.exists():
        return defaults
    data = yaml.safe_load(models_yaml.read_text(encoding="utf-8")) or {}
    cfg = data.get("designer") or {}
    return {
        "model": str(cfg.get("model", defaults["model"])),
        "thinking_level": str(cfg.get("thinking_level", defaults["thinking_level"])),
    }


def _load_dotenv_if_needed() -> None:
    if os.environ.get("GEMINI_API_KEY"):
        return
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def extract_python_code(text: str) -> str:
    """Extract Python source code from a model response (fenced or raw)."""
    if not text:
        return ""
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if blocks:
        for b in blocks:
            if "def control(" in b:
                return b.strip() + "\n"
        return blocks[0].strip() + "\n"
    stripped = text.strip()
    if "def control(" in stripped:
        return stripped + "\n"
    return stripped


def _summarize_ruleset(ruleset: RuleSet) -> str:
    lines = [f"Rulebook: {ruleset.source_name} (cluster rated_mw={ruleset.rated_mw:.1f} MW)"]
    for c in ruleset.constraints:
        if not c.checkable or c.value is None:
            continue
        try:
            lim, unit = normalize_constraint_limit(c, rated_mw=ruleset.rated_mw)
            norm_str = f"normalized <= {lim:.6g} {unit}"
        except UnitConversionError:
            norm_str = f"raw {c.value} {c.unit}"
        extra = []
        if c.entity:
            extra.append(f"entity={c.entity}")
        if c.window_s is not None:
            extra.append(f"window_s={c.window_s:g}")
        if c.freq_min_hz is not None or c.freq_max_hz is not None:
            extra.append(f"band=[{c.freq_min_hz},{c.freq_max_hz}] Hz")
        extra_str = f" ({', '.join(extra)})" if extra else ""
        lines.append(
            f"- [{c.id}] {c.constraint_type}: <= {c.value} {c.unit} ({norm_str}){extra_str} | p.{c.page} | \"{c.snippet}\""
        )
    assumptions = ruleset.collect_assumptions()
    if assumptions:
        lines.append("Documented assumptions applied by checker:")
        for a in assumptions:
            lines.append(f"  * {a}")
    return "\n".join(lines)


def _format_feedback(parent_code: str, parent_eval: CandidateEval, recent_errors: list[str]) -> str:
    lines = [
        f"Current best candidate status: {'PASS (ALL EVALUABLE WORKLOADS)' if parent_eval.passed else 'FAIL'} "
        f"({parent_eval.workloads_passed}/{parent_eval.workloads_evaluable} evaluable workloads passed; "
        f"{parent_eval.workloads_not_evaluable} not_evaluable)",
        f"Overall metrics: energy_overhead={parent_eval.mean_energy_overhead_pct:.2f}%, "
        f"throughput_loss={parent_eval.mean_throughput_loss_pct:.2f}%, "
        f"battery_wear={parent_eval.mean_battery_wear:.6f}, "
        f"max_shaft_torque={parent_eval.max_shaft_torque_pu:.4f} pu, "
        f"worst_margin={parent_eval.worst_margin_pct if parent_eval.worst_margin_pct is not None else 'n/a'}%, "
        f"composite_score={parent_eval.composite_score:.3f}",
    ]
    if parent_eval.family_summary:
        for fam, fs in sorted(parent_eval.family_summary.items()):
            lines.append(
                f"  Family [{fam}]: status={fs['status']} ({fs['workloads_passed']}/{fs['workloads_evaluable']} eval), "
                f"OH={fs['mean_energy_overhead_pct']:.2f}%, TP={fs['mean_throughput_loss_pct']:.2f}%, "
                f"worst_margin={fs['worst_margin_pct']}%"
            )
    if parent_eval.error:
        lines.append(f"Error: {parent_eval.error}")
    if parent_eval.failing_constraints:
        lines.append("Failing constraints (MUST FIX ALL TO PASS GATE):")
        sorted_fails = sorted(
            parent_eval.failing_constraints,
            key=lambda x: x["margin_pct"] if x.get("margin_pct") is not None else -1e9,
        )[:8]
        for f in sorted_fails:
            lines.append(
                f"  - [{f.get('family', 'fast')}] {f['workload']} | {f['constraint_id']} ({f['constraint_type']}): "
                f"measured={f['measured']:.4f} vs limit={f['limit']:.4f} {f['canonical_unit']} "
                f"(margin={f['margin_pct']:.1f}%, p.{f['page']})"
            )
    else:
        lines.append(
            "All evaluable constraints currently PASS. Optimize to reduce energy_overhead_pct, "
            "throughput_loss_pct, and battery_wear while staying strictly compliant across BOTH fast and slow traces."
        )
    if recent_errors:
        lines.append("Avoid these errors seen in prior candidates:")
        for e in recent_errors[:4]:
            lines.append(f"  - {e}")
    lines.append("\nCurrent parent controller code:\n```python\n" + parent_code.strip() + "\n```")
    return "\n".join(lines)


def _build_mutation_prompt(
    ruleset: RuleSet,
    parent_code: str,
    parent_eval: CandidateEval,
    strategy_name: str,
    strategy_guidance: str,
    plant_config: PlantConfig | None,
    recent_errors: list[str],
) -> tuple[str, str]:
    cfg = plant_config if plant_config is not None else PlantConfig()
    b = cfg.battery

    system_instruction = (
        "You are GridSafe Designer, a power-systems control engineer writing deterministic Python controllers "
        "for an AI training data center with a co-located battery.\n"
        "Output ONLY a single Python code block defining `def control(obs: dict) -> dict[str, float]:`.\n"
        "HARD RULES:\n"
        "- Only `import math` and `import numpy as np` are allowed. NO other imports, NO file/network/eval/exec/dunder access.\n"
        "- Keep the entire controller under 60 lines of clean, fast, deterministic Python.\n"
        "- Must work cleanly on BOTH fast traces (20 s, dt=0.002 s, 10,000 steps) AND slow traces (30 min = 1800 s, dt=0.5 s, 3,600 steps)!\n"
        "  ALWAYS scale time constants and slew limits by `obs['dt']` — NEVER assume dt == 0.002!\n"
        "- Must return {'gpu_floor_mw': float, 'gpu_cap_mw': float, 'battery_setpoint_mw': float} with finite floats.\n"
        "- INITIAL CONDITION (CRITICAL — NO CHEATING AT t=0):\n"
        "  Every trace starts with the facility at steady idle power `idle_mw = obs['idle_mw']` (also `obs['p_prev_grid_mw']` at step 0).\n"
        "  The deterministic checker prepends steady `idle_mw` for ALL t < 0, so ANY instantaneous jump in p_grid[0] away from `idle_mw` "
        "immediately counts against ramp_rate, max_step, and dynamic_range rules!\n"
        "  Do NOT initialize p_target at mean training power (e.g., 70 MW). Always start p_target at `idle_mw` and ramp legally.\n"
        "  During idle (p_raw == idle_mw), you MAY pre-load by smoothly ramping p_target upward using battery charging or dummy load (overhead counted), "
        "or you may delay/curtail job startup with gpu_cap_mw (throughput loss counted).\n"
        "- Exact plant physics at step i:\n"
        "  1. gpu_cap = clip(gpu_cap_mw, 0, rated_mw); gpu_floor = clip(gpu_floor_mw, 0, gpu_cap)\n"
        "  2. p_capped = min(p_raw_mw, gpu_cap); p_gpu = max(p_capped, gpu_floor)\n"
        "     (dummy load = p_gpu - p_capped -> energy_overhead_pct; curtailment = p_raw - p_capped -> throughput_loss_pct)\n"
        "  3. battery_setpoint_mw > 0 DISCHARGES battery (reduces p_grid); < 0 CHARGES battery (increases p_grid).\n"
        "     Clamped to [-battery_max_power_mw, +battery_max_power_mw] and SOC in [soc_min=0.10, soc_max=0.90] "
        f"(max_power={b.max_power_mw:.1f} MW, capacity={b.capacity_mwh:.2f} MWh, eta_chg={b.eta_charge:.4f}, eta_dis={b.eta_discharge:.4f}).\n"
        "  4. p_grid = p_gpu - p_batt.\n"
        "- Available obs keys: t, dt, step, p_raw_mw, p_prev_grid_mw, idle_mw, rated_mw, soc, battery_energy_mwh, "
        "battery_capacity_mwh, battery_max_power_mw, history_grid_mw, history_raw_mw, rules, state (dict persisted per run).\n"
        "- EXACT ACTUATOR INSIGHT (prevents sudden grid spikes even when SOC hits 0.10 or 0.90 on 30-min traces):\n"
        "  dt_h = dt / 3600.0; eta = math.sqrt(0.90)\n"
        "  b_dis_max = min(b_max, max(0.0, (e - 0.1 * cap) * eta / dt_h))\n"
        "  b_chg_max = min(b_max, max(0.0, (0.9 * cap - e) / (eta * dt_h)))\n"
        "  If p_target < p_raw - b_dis_max: gpu_cap = p_target + b_dis_max; gpu_floor = 0.0; batt = b_dis_max\n"
        "  Elif p_target > p_raw + b_chg_max: gpu_floor = p_target - b_chg_max; gpu_cap = rated; batt = -b_chg_max\n"
        "  Else: gpu_floor = 0.0; gpu_cap = rated; batt = p_raw - p_target\n"
        "  This guarantees p_grid == p_target on EVERY step with ZERO unnecessary curtailment and MINIMUM dummy energy!\n"
        "- WINDOW MATH INSIGHT:\n"
        "  A per-step slew limit |p_target[i] - p_target[i-1]| <= r_max * dt guarantees EVERY rolling ramp window (60s, 600s) "
        "has rate <= r_max AND every max_step window W has swing <= r_max * W in O(1) time!\n"
        "  For fluctuation_band limits (0.1-55 Hz or 5-55 Hz), a 2nd-order low-pass filter (cascaded EMA) + slew limit suppresses "
        "high-frequency harmonics strongly without requiring huge overhead.\n"
    )

    user_prompt = (
        f"Target utility constraints:\n{_summarize_ruleset(ruleset)}\n\n"
        f"{_format_feedback(parent_code, parent_eval, recent_errors)}\n\n"
        f"Mutation strategy ({strategy_name}): {strategy_guidance}\n\n"
        "Write an improved `def control(obs)` controller now. Return ONLY the Python code block."
    )
    return system_instruction, user_prompt


def _make_hydrogen_mutator(model: str, thinking_level: str, seed: int | None = None) -> MutatorFn:
    _load_dotenv_if_needed()
    from google import genai

    def _mutate(system_instruction: str, user_prompt: str, generation: int, worker_id: int) -> str:
        client = genai.Client(http_options={"api_version": "v1beta"})
        gen_cfg: dict[str, Any] = {"thinking_level": thinking_level}
        if seed is not None:
            gen_cfg["seed"] = int(seed) + generation * 100 + worker_id
        interaction = client.interactions.create(
            model=model,
            input=user_prompt,
            system_instruction=system_instruction,
            generation_config=gen_cfg,
        )
        return extract_python_code(interaction.output_text or "")

    return _mutate


def _unified_diff(old_code: str, new_code: str) -> str:
    diff_lines = list(
        difflib.unified_diff(
            old_code.splitlines(),
            new_code.splitlines(),
            fromfile="parent.py",
            tofile="candidate.py",
            lineterm="",
            n=2,
        )
    )
    return "\n".join(diff_lines[:80])


def _is_better_eval(cand: CandidateEval, incumbent: CandidateEval | None) -> bool:
    """Return True if `cand` strictly beats `incumbent` (passing beats failing; lower score wins)."""
    if incumbent is None:
        return True
    if cand.passed and not incumbent.passed:
        return True
    if cand.passed == incumbent.passed and cand.composite_score < incumbent.composite_score - 1e-6:
        return True
    return False


def evolve_controller(
    ruleset: RuleSet,
    workloads: dict[str, WorkloadParams],
    *,
    seed_code: str | None = None,
    plant_config: PlantConfig | None = None,
    budget_s: float = 75.0,
    population_size: int = 6,
    max_generations: int = 3,
    model: str | None = None,
    thinking_level: str | None = None,
    eval_timeout_s: float = 10.0,
    strict_review: bool = True,
    seed: int | None = None,
    enable_b3_seed: bool = True,
    b3_n_evals: int = 19,
    on_event: EventCallback | None = None,
    mutator_fn: MutatorFn | None = None,
) -> EvolutionResult:
    """Run parallel Hydrogen evolution seeded by B3 with a 4-rung safety ladder:
    evolved controller (scoring <= B3) -> B3 -> conservative floor -> explicit refusal.
    """
    t_start = time.perf_counter()
    cfg = load_designer_config()
    eff_model = model or cfg["model"]
    eff_thinking = thinking_level or cfg["thinking_level"]
    pop_size = max(1, min(8, int(population_size)))

    strategy_pool = list(MUTATION_STRATEGIES)
    if seed is not None:
        random.Random(seed).shuffle(strategy_pool)

    def emit(event_type: str, data: dict[str, Any]) -> None:
        if on_event is not None:
            payload = {"event": event_type, "elapsed_s": round(time.perf_counter() - t_start, 3), **data}
            on_event(payload)

    base_code = seed_code if seed_code is not None else SEED_CONTROLLER_PATH.read_text(encoding="utf-8")

    emit(
        "evolve_start",
        {
            "source_name": ruleset.source_name,
            "model": eff_model,
            "thinking_level": eff_thinking,
            "population_size": pop_size,
            "budget_s": budget_s,
            "seed": seed,
            "enable_b3_seed": enable_b3_seed,
            "workloads": list(workloads.keys()),
        },
    )

    seed_eval = evaluate_controller_sandboxed(
        base_code,
        ruleset,
        workloads,
        plant_config=plant_config,
        strict_review=strict_review,
        timeout_s=eval_timeout_s,
    )
    emit(
        "seed_evaluated",
        {
            "passed": seed_eval.passed,
            "score": round(seed_eval.composite_score, 4),
            "overhead_pct": round(seed_eval.mean_energy_overhead_pct, 2),
            "throughput_loss_pct": round(seed_eval.mean_throughput_loss_pct, 2),
            "worst_margin_pct": seed_eval.worst_margin_pct,
            "failing_count": len(seed_eval.failing_constraints),
        },
    )

    has_blocking_review = any(w.get("needs_review_count", 0) > 0 for w in seed_eval.per_workload)
    has_zero_checkable = bool(seed_eval.per_workload) and all(
        w.get("checkable_count", 0) == 0 for w in seed_eval.per_workload
    )
    if (has_blocking_review and strict_review) or has_zero_checkable:
        reason = (
            seed_eval.per_workload[0].get("refusal_reason")
            if seed_eval.per_workload
            else "No checkable numeric constraints present."
        )
        res = EvolutionResult(
            outcome="refused",
            passed=False,
            model=eff_model,
            thinking_level=eff_thinking,
            best_code=None,
            best_eval=None,
            seed_eval=seed_eval,
            b3_eval=None,
            fallback_eval=None,
            generations_completed=0,
            total_candidates_evaluated=1,
            total_candidates_passed=0,
            total_static_rejected=0,
            elapsed_s=time.perf_counter() - t_start,
            seed=seed,
            refusal_reason=reason,
            history=[],
        )
        emit("evolve_complete", {"outcome": res.outcome, "passed": False, "refusal_reason": reason})
        return res

    # Tune B3 (low-pass + battery random search, no LLM) as Designer starting point and second fallback rung.
    b3_code: str | None = None
    b3_eval: CandidateEval | None = None
    if enable_b3_seed and b3_n_evals > 0:
        from evolve.baselines import run_b3_baseline

        b3_res = run_b3_baseline(
            ruleset,
            workloads,
            n_evals=b3_n_evals,
            seed=seed if seed is not None else 42,
            plant_config=plant_config,
            strict_review=strict_review,
        )
        b3_code = b3_res.code
        b3_eval = b3_res.eval_result
        emit(
            "b3_seed_evaluated",
            {
                "passed": b3_eval.passed,
                "score": round(b3_eval.composite_score, 4),
                "overhead_pct": round(b3_eval.mean_energy_overhead_pct, 2),
                "throughput_loss_pct": round(b3_eval.mean_throughput_loss_pct, 2),
                "worst_margin_pct": b3_eval.worst_margin_pct,
                "n_evals": b3_res.n_evals,
            },
        )

    # Start Designer evolution from B3 whenever B3 is at least as good as (or beats) the default seed.
    if b3_code is not None and b3_eval is not None and (seed_code is None or _is_better_eval(b3_eval, seed_eval)):
        best_code: str = b3_code
        best_eval: CandidateEval = b3_eval
    else:
        best_code = base_code
        best_eval = seed_eval

    best_evolved_code: str | None = None
    best_evolved_eval: CandidateEval | None = None
    history: list[CandidateRecord] = []
    total_evals = 1 + (1 if b3_eval is not None else 0)
    total_passed = (1 if seed_eval.passed else 0) + (1 if (b3_eval and b3_eval.passed) else 0)
    total_static_rejected = 0
    recent_errors: list[str] = []
    gens_completed = 0
    stagnant_gens = 0

    for gen in range(1, max_generations + 1):
        elapsed = time.perf_counter() - t_start
        remaining = budget_s - elapsed
        if remaining < 6.0:
            break

        gen_thinking = "low" if (remaining < 35.0 or eff_thinking == "low") else eff_thinking
        active_mutator = (
            mutator_fn if mutator_fn is not None else _make_hydrogen_mutator(eff_model, gen_thinking, seed=seed)
        )

        emit(
            "generation_start",
            {
                "generation": gen,
                "remaining_s": round(remaining, 2),
                "thinking_level": gen_thinking,
                "parent_passed": best_eval.passed,
                "parent_score": round(best_eval.composite_score, 4),
            },
        )

        strategies = [strategy_pool[(gen - 1 + w) % len(strategy_pool)] for w in range(pop_size)]
        parent_snapshot_code = best_code
        parent_snapshot_eval = best_eval
        gen_errors: list[str] = []
        gen_improved = False

        def _run_worker(worker_idx: int) -> CandidateRecord:
            strat_name, strat_guide = strategies[worker_idx]
            sys_inst, prompt = _build_mutation_prompt(
                ruleset=ruleset,
                parent_code=parent_snapshot_code,
                parent_eval=parent_snapshot_eval,
                strategy_name=strat_name,
                strategy_guidance=strat_guide,
                plant_config=plant_config,
                recent_errors=recent_errors,
            )
            t_api0 = time.perf_counter()
            try:
                cand_code = active_mutator(sys_inst, prompt, gen, worker_idx)
            except Exception as exc:
                cand_code = ""
                api_t = time.perf_counter() - t_api0
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
                    error=f"Designer API error: {type(exc).__name__}: {exc}",
                    elapsed_s=api_t,
                )
                return CandidateRecord(gen, worker_idx, strat_name, cand_code, ev, "", api_t)
            api_t = time.perf_counter() - t_api0

            ev = evaluate_controller_sandboxed(
                cand_code,
                ruleset,
                workloads,
                plant_config=plant_config,
                strict_review=strict_review,
                timeout_s=eval_timeout_s,
            )
            diff_txt = _unified_diff(parent_snapshot_code, cand_code) if cand_code else ""
            return CandidateRecord(gen, worker_idx, strat_name, cand_code, ev, diff_txt, api_t)

        gen_records: list[CandidateRecord] = []
        pool = ThreadPoolExecutor(max_workers=pop_size)
        try:
            futs = [pool.submit(_run_worker, idx) for idx in range(pop_size)]
            wait_budget = max(5.0, budget_s - (time.perf_counter() - t_start))
            try:
                for fut in as_completed(futs, timeout=wait_budget):
                    rec = fut.result()
                    gen_records.append(rec)
                    total_evals += 1
                    if rec.eval_result.static_violations:
                        total_static_rejected += 1
                    if rec.eval_result.passed:
                        total_passed += 1
                    elif rec.eval_result.error:
                        gen_errors.append(rec.eval_result.error)

                    emit(
                        "candidate_done",
                        {
                            "generation": gen,
                            "worker_id": rec.worker_id,
                            "strategy": rec.strategy,
                            "passed": rec.eval_result.passed,
                            "score": round(rec.eval_result.composite_score, 4),
                            "overhead_pct": round(rec.eval_result.mean_energy_overhead_pct, 2),
                            "throughput_loss_pct": round(rec.eval_result.mean_throughput_loss_pct, 2),
                            "battery_wear": round(rec.eval_result.mean_battery_wear, 6),
                            "worst_margin_pct": rec.eval_result.worst_margin_pct,
                            "static_violations": rec.eval_result.static_violations,
                            "error": rec.eval_result.error,
                            "api_time_s": round(rec.api_time_s, 2),
                            "diff": rec.diff_from_parent,
                        },
                    )
            except FuturesTimeoutError:
                emit("generation_timeout", {"generation": gen, "completed_workers": len(gen_records)})
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        gen_records.sort(key=lambda r: r.worker_id)
        history.extend(gen_records)
        gens_completed = gen

        for rec in gen_records:
            ev = rec.eval_result
            if _is_better_eval(ev, best_evolved_eval):
                best_evolved_code = rec.code
                best_evolved_eval = ev
            if _is_better_eval(ev, best_eval):
                best_code = rec.code
                best_eval = ev
                gen_improved = True
                emit(
                    "new_best",
                    {
                        "generation": gen,
                        "worker_id": rec.worker_id,
                        "strategy": rec.strategy,
                        "passed": ev.passed,
                        "score": round(ev.composite_score, 4),
                        "overhead_pct": round(ev.mean_energy_overhead_pct, 2),
                        "throughput_loss_pct": round(ev.mean_throughput_loss_pct, 2),
                        "battery_wear": round(ev.mean_battery_wear, 6),
                        "worst_margin_pct": ev.worst_margin_pct,
                    },
                )

        recent_errors = gen_errors[:4]
        if best_eval.passed and not gen_improved:
            stagnant_gens += 1
        else:
            stagnant_gens = 0

        if best_eval.passed and (
            (best_eval.composite_score < 1.0 and gen >= 2) or (stagnant_gens >= 1 and gen >= 2)
        ):
            break

    # 4-rung safety ladder:
    #   Rung 1: evolved controller (must pass AND score <= B3 if B3 passed)
    #   Rung 2: B3 (tuned low-pass + battery) if it passed
    #   Rung 3: conservative fixed floor if it passes
    #   Rung 4: explicit refusal with reason
    evolved_wins = (
        best_evolved_code is not None
        and best_evolved_eval is not None
        and best_evolved_eval.passed
        and (
            b3_eval is None
            or not b3_eval.passed
            or best_evolved_eval.composite_score <= b3_eval.composite_score + 1e-9
        )
    )
    fallback_eval: CandidateEval | None = None
    if evolved_wins and best_evolved_code is not None and best_evolved_eval is not None:
        res = EvolutionResult(
            outcome="evolved_compliant",
            passed=True,
            model=eff_model,
            thinking_level=eff_thinking,
            best_code=best_evolved_code,
            best_eval=best_evolved_eval,
            seed_eval=seed_eval,
            b3_eval=b3_eval,
            fallback_eval=None,
            generations_completed=gens_completed,
            total_candidates_evaluated=total_evals,
            total_candidates_passed=total_passed,
            total_static_rejected=total_static_rejected,
            elapsed_s=time.perf_counter() - t_start,
            seed=seed,
            refusal_reason=None,
            history=history,
        )
    elif b3_code is not None and b3_eval is not None and b3_eval.passed:
        res = EvolutionResult(
            outcome="fallback_b3",
            passed=True,
            model=eff_model,
            thinking_level=eff_thinking,
            best_code=b3_code,
            best_eval=b3_eval,
            seed_eval=seed_eval,
            b3_eval=b3_eval,
            fallback_eval=b3_eval,
            generations_completed=gens_completed,
            total_candidates_evaluated=total_evals,
            total_candidates_passed=total_passed,
            total_static_rejected=total_static_rejected,
            elapsed_s=time.perf_counter() - t_start,
            seed=seed,
            refusal_reason=None,
            history=history,
        )
    elif seed_eval.passed:
        res = EvolutionResult(
            outcome="evolved_compliant",
            passed=True,
            model=eff_model,
            thinking_level=eff_thinking,
            best_code=base_code,
            best_eval=seed_eval,
            seed_eval=seed_eval,
            b3_eval=b3_eval,
            fallback_eval=None,
            generations_completed=gens_completed,
            total_candidates_evaluated=total_evals,
            total_candidates_passed=total_passed,
            total_static_rejected=total_static_rejected,
            elapsed_s=time.perf_counter() - t_start,
            seed=seed,
            refusal_reason=None,
            history=history,
        )
    else:
        fallback_eval = evaluate_controller_sandboxed(
            CONSERVATIVE_FLOOR_CODE,
            ruleset,
            workloads,
            plant_config=plant_config,
            strict_review=strict_review,
            timeout_s=eval_timeout_s,
        )
        emit(
            "fallback_checked",
            {
                "passed": fallback_eval.passed,
                "overhead_pct": round(fallback_eval.mean_energy_overhead_pct, 2),
                "worst_margin_pct": fallback_eval.worst_margin_pct,
            },
        )
        if fallback_eval.passed:
            res = EvolutionResult(
                outcome="fallback_conservative_floor",
                passed=True,
                model=eff_model,
                thinking_level=eff_thinking,
                best_code=CONSERVATIVE_FLOOR_CODE,
                best_eval=fallback_eval,
                seed_eval=seed_eval,
                b3_eval=b3_eval,
                fallback_eval=fallback_eval,
                generations_completed=gens_completed,
                total_candidates_evaluated=total_evals + 1,
                total_candidates_passed=total_passed + 1,
                total_static_rejected=total_static_rejected,
                elapsed_s=time.perf_counter() - t_start,
                seed=seed,
                refusal_reason=None,
                history=history,
            )
        else:
            fail_summary = "; ".join(
                f"{f['workload']}:{f['constraint_id']}({f['measured']:.2f}>{f['limit']:.2f}{f['canonical_unit']})"
                for f in fallback_eval.failing_constraints[:4]
                if f.get("measured") is not None and f.get("limit") is not None
            )
            reason = (
                f"Refused: no evolved controller passed within {budget_s:.0f}s budget, B3 failed, "
                f"and conservative fixed-floor fallback also failed ({fail_summary or fallback_eval.error})."
            )
            res = EvolutionResult(
                outcome="refused",
                passed=False,
                model=eff_model,
                thinking_level=eff_thinking,
                best_code=None,
                best_eval=best_eval,
                seed_eval=seed_eval,
                b3_eval=b3_eval,
                fallback_eval=fallback_eval,
                generations_completed=gens_completed,
                total_candidates_evaluated=total_evals + 1,
                total_candidates_passed=total_passed,
                total_static_rejected=total_static_rejected,
                elapsed_s=time.perf_counter() - t_start,
                seed=seed,
                refusal_reason=reason,
                history=history,
            )

    emit(
        "evolve_complete",
        {
            "outcome": res.outcome,
            "passed": res.passed,
            "generations_completed": res.generations_completed,
            "total_candidates_evaluated": res.total_candidates_evaluated,
            "score": round(res.best_eval.composite_score, 4) if res.best_eval else None,
            "overhead_pct": round(res.best_eval.mean_energy_overhead_pct, 2) if res.best_eval else None,
            "refusal_reason": res.refusal_reason,
        },
    )
    return res
