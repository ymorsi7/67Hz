"""GridSafe Designer evolution loop (parallel Gemini 4 Hydrogen mutations).

Pure Python + numpy (+ optional google-genai when calling Hydrogen). No scipy,
no PyMuPDF, no pydantic. Runs 6 to 8 parallel Hydrogen mutations per generation,
evaluates every candidate in an isolated subprocess sandbox against the
deterministic compliance checker (hard gate), scores passing candidates on
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
import re
import time
from typing import Any, Callable

import yaml

from evolve.sandbox import CandidateEval, evaluate_controller_sandboxed
from rules.schema import RuleSet
from rules.units import UnitConversionError, normalize_constraint_limit
from sim.plant import PlantConfig
from workload.synth import WorkloadParams

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_YAML = REPO_ROOT / "config" / "models.yaml"
SEED_CONTROLLER_PATH = REPO_ROOT / "controllers" / "seed.py"

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
        "Maintain a rate-limited grid power target p_target updated by at most 0.90*r_max*dt per step; "
        "initialize p_target near mean cluster power (~0.75*rated_mw) or within battery reach of p_raw[0] so "
        "step 0 starts smoothly; use battery_setpoint_mw = p_raw - p_target first and apply exact GPU floor/cap "
        "only when the battery power or SOC limit would be exceeded.",
    ),
    (
        "min_overhead_center",
        "Minimize energy_overhead_pct and throughput_loss_pct: choose an initial p_target close to the "
        "expected mean workload power (~0.72*rated_mw) so the battery absorbs both peaks (discharge) and dips "
        "(charge) with near-zero dummy GPU burn and zero curtailment, while strictly enforcing every ramp/step limit.",
    ),
    (
        "window_envelope_guard",
        "Track a rolling buffer of recent p_grid targets in obs['state'] so max(window) - min(window) strictly "
        "stays below every max_step and dynamic_range limit (with 10% safety margin), while keeping ramp rate "
        "below the tightest ramp limit using battery-first dispatch.",
    ),
    (
        "spectral_notch_smooth",
        "Suppress frequency-domain ripples in forbidden fluctuation bands (0.1-55 Hz) and IEEE FBM shaft modes "
        "by driving p_grid toward a heavily low-pass filtered / nearly constant target with strict slew clamping "
        "and battery-first actuation.",
    ),
    (
        "soc_balanced_low_wear",
        "Minimize battery wear proxy (avoid unnecessary large C-rate spikes) and prevent SOC exhaustion by "
        "adding a very gentle, rate-limited SOC restoring drift toward 0.50 that NEVER violates ramp_rate, "
        "max_step, or fluctuation_band limits.",
    ),
    (
        "high_margin_robust",
        "Prioritize passing ALL constraints across standard, SSR resonant, and abrupt checkpoint workloads "
        "with generous safety margin (<= 80% of every numeric limit) while still beating B1's ~30% overhead.",
    ),
    (
        "adaptive_deadband",
        "Allow small deadband tracking when limits are loose (e.g. LIPA frequency-only rules) to save battery "
        "wear, and tighten automatically using obs['rules'] whenever ramp_rate or max_step constraints are present.",
    ),
    (
        "two_stage_filter",
        "Cascade a slow EMA target with a strict per-step slew limiter and rolling-window clamp, then solve "
        "exactly for (battery_setpoint_mw, gpu_floor_mw, gpu_cap_mw) accounting for available battery discharge "
        "and charge headroom from obs['soc'] and obs['battery_capacity_mwh'].",
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
    """Guaranteed outcome of GridSafe Designer evolution."""

    outcome: str  # "evolved_compliant" | "fallback_conservative_floor" | "refused"
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
    refusal_reason: str | None = None
    history: list[CandidateRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "passed": self.passed,
            "model": self.model,
            "thinking_level": self.thinking_level,
            "best_code": self.best_code,
            "best_eval": self.best_eval.to_dict() if self.best_eval else None,
            "seed_eval": self.seed_eval.to_dict(),
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
    defaults = {"model": "gemini-4-hydrogen", "thinking_level": "medium"}
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
        # Prefer the block that defines def control(
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
        if c.window_s is not None:
            extra.append(f"window_s={c.window_s:g}")
        if c.freq_min_hz is not None or c.freq_max_hz is not None:
            extra.append(f"band=[{c.freq_min_hz},{c.freq_max_hz}] Hz")
        extra_str = f" ({', '.join(extra)})" if extra else ""
        lines.append(
            f"- [{c.id}] {c.constraint_type}: <= {c.value} {c.unit} ({norm_str}){extra_str} | p.{c.page} | \"{c.snippet}\""
        )
    return "\n".join(lines)


def _format_feedback(parent_code: str, parent_eval: CandidateEval, recent_errors: list[str]) -> str:
    lines = [
        f"Current best candidate status: {'PASS (ALL WORKLOADS)' if parent_eval.passed else 'FAIL'} "
        f"({parent_eval.workloads_passed}/{parent_eval.workloads_total} workloads passed)",
        f"Metrics: energy_overhead={parent_eval.mean_energy_overhead_pct:.2f}%, "
        f"throughput_loss={parent_eval.mean_throughput_loss_pct:.2f}%, "
        f"battery_wear={parent_eval.mean_battery_wear:.6f}, "
        f"max_shaft_torque={parent_eval.max_shaft_torque_pu:.4f} pu, "
        f"worst_margin={parent_eval.worst_margin_pct if parent_eval.worst_margin_pct is not None else 'n/a'}%, "
        f"composite_score={parent_eval.composite_score:.3f}",
    ]
    if parent_eval.error:
        lines.append(f"Error: {parent_eval.error}")
    if parent_eval.failing_constraints:
        lines.append("Failing constraints (MUST FIX ALL TO PASS GATE):")
        # Show up to 8 worst failures sorted by most negative margin
        sorted_fails = sorted(
            parent_eval.failing_constraints,
            key=lambda x: x["margin_pct"] if x.get("margin_pct") is not None else -1e9,
        )[:8]
        for f in sorted_fails:
            lines.append(
                f"  - workload={f['workload']} | {f['constraint_id']} ({f['constraint_type']}): "
                f"measured={f['measured']:.4f} vs limit={f['limit']:.4f} {f['canonical_unit']} "
                f"(margin={f['margin_pct']:.1f}%, p.{f['page']})"
            )
    else:
        lines.append(
            "All constraints currently PASS. Optimize to reduce energy_overhead_pct, throughput_loss_pct, "
            "and battery_wear while staying strictly compliant across all workloads."
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
        "- Keep the entire controller under 60 lines of clean, fast, deterministic Python (called 5,000 times per 10s trace).\n"
        "- Must return {'gpu_floor_mw': float, 'gpu_cap_mw': float, 'battery_setpoint_mw': float} with finite floats.\n"
        "- Exact plant physics at step i:\n"
        "  1. gpu_cap = clip(gpu_cap_mw, 0, rated_mw); gpu_floor = clip(gpu_floor_mw, 0, gpu_cap)\n"
        "  2. p_capped = min(p_raw_mw, gpu_cap); p_gpu = max(p_capped, gpu_floor)\n"
        "     (dummy load = p_gpu - p_capped increases energy_overhead_pct; curtailment = p_raw - p_capped increases throughput_loss_pct)\n"
        "  3. battery_setpoint_mw > 0 DISCHARGES battery (reduces p_grid); < 0 CHARGES battery (increases p_grid).\n"
        "     Clamped to [-battery_max_power_mw, +battery_max_power_mw] and SOC in [soc_min=0.10, soc_max=0.90] "
        f"(max_power={b.max_power_mw:.1f} MW, capacity={b.capacity_mwh:.2f} MWh, eta_chg={b.eta_charge:.4f}, eta_dis={b.eta_discharge:.4f}).\n"
        "  4. p_grid = p_gpu - p_batt.\n"
        "- Available obs keys: t, dt, step, p_raw_mw, p_prev_grid_mw, rated_mw, soc, battery_energy_mwh, "
        "battery_capacity_mwh, battery_max_power_mw, history_grid_mw (numpy 1D), history_raw_mw (numpy 1D), "
        "rules (dict), state (mutable dict persisted across steps of one simulation).\n"
        "- IMPORTANT CONTROL INSIGHT: To guarantee p_grid[i] == p_target exactly (within battery + GPU bounds):\n"
        "  Compute available battery discharge headroom b_dis_max = min(b_max, max(0, (e - 0.1*cap)*eta_d/(dt/3600)))\n"
        "  and charge headroom b_chg_max = min(b_max, max(0, (0.9*cap - e)/(eta_c*(dt/3600)))).\n"
        "  If p_target < p_raw - b_dis_max: set gpu_cap_mw = p_target + b_dis_max, gpu_floor_mw = 0, battery_setpoint_mw = b_dis_max.\n"
        "  Else if p_target > p_raw + b_chg_max: set gpu_floor_mw = p_target - b_chg_max, gpu_cap_mw = rated_mw, battery_setpoint_mw = -b_chg_max.\n"
        "  Else: set gpu_floor_mw = 0, gpu_cap_mw = rated_mw, battery_setpoint_mw = p_raw - p_target.\n"
        "  That achieves p_grid == p_target with ZERO unnecessary curtailment and MINIMUM dummy energy!\n"
        "- Note on ramp_rate with window_s=None: checker measures max(|p_grid[i] - p_grid[i-1]| / dt) across ALL adjacent steps!\n"
        "- Note on ramp_rate with large window_s (e.g. 60s or 600s on a 10s trace): checker measures |p_grid[-1] - p_grid[0]| / 10s!\n"
        "  So initializing p_target near expected mean load (~0.70-0.80 * rated_mw) at step 0 avoids a big drift from idle to active!\n"
    )

    user_prompt = (
        f"Target utility constraints:\n{_summarize_ruleset(ruleset)}\n\n"
        f"{_format_feedback(parent_code, parent_eval, recent_errors)}\n\n"
        f"Mutation strategy ({strategy_name}): {strategy_guidance}\n\n"
        "Write an improved `def control(obs)` controller now. Return ONLY the Python code block."
    )
    return system_instruction, user_prompt


def _make_hydrogen_mutator(model: str, thinking_level: str) -> MutatorFn:
    _load_dotenv_if_needed()
    from google import genai

    def _mutate(system_instruction: str, user_prompt: str, generation: int, worker_id: int) -> str:
        client = genai.Client(http_options={"api_version": "v1beta"})
        interaction = client.interactions.create(
            model=model,
            input=user_prompt,
            system_instruction=system_instruction,
            generation_config={"thinking_level": thinking_level},
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


def evolve_controller(
    ruleset: RuleSet,
    workloads: dict[str, WorkloadParams],
    *,
    seed_code: str | None = None,
    plant_config: PlantConfig | None = None,
    budget_s: float = 75.0,
    population_size: int = 6,
    max_generations: int = 4,
    model: str | None = None,
    thinking_level: str | None = None,
    eval_timeout_s: float = 6.0,
    strict_review: bool = True,
    on_event: EventCallback | None = None,
    mutator_fn: MutatorFn | None = None,
) -> EvolutionResult:
    """Run parallel Hydrogen evolution with sandboxed deterministic gate and guaranteed fallback."""
    t_start = time.perf_counter()
    cfg = load_designer_config()
    eff_model = model or cfg["model"]
    eff_thinking = thinking_level or cfg["thinking_level"]
    pop_size = max(1, min(8, int(population_size)))

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
            "workloads": list(workloads.keys()),
        },
    )

    # Check if ruleset already has blocking human-review items or zero checkable constraints
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

    # Fast refusal if the ruleset itself cannot be certified (e.g., needs_human_review or 0 checkable constraints)
    has_blocking_review = any(
        w.get("needs_review_count", 0) > 0 for w in seed_eval.per_workload
    )
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
            fallback_eval=None,
            generations_completed=0,
            total_candidates_evaluated=1,
            total_candidates_passed=0,
            total_static_rejected=0,
            elapsed_s=time.perf_counter() - t_start,
            refusal_reason=reason,
            history=[],
        )
        emit("evolve_complete", {"outcome": res.outcome, "passed": False, "refusal_reason": reason})
        return res

    best_code: str = base_code
    best_eval: CandidateEval = seed_eval
    history: list[CandidateRecord] = []
    total_evals = 1
    total_passed = 1 if seed_eval.passed else 0
    total_static_rejected = 0
    recent_errors: list[str] = []
    gens_completed = 0
    stagnant_gens = 0

    for gen in range(1, max_generations + 1):
        elapsed = time.perf_counter() - t_start
        remaining = budget_s - elapsed
        if remaining < 6.0:
            break

        # Adaptive thinking level so later generations stay comfortably inside 75s budget
        gen_thinking = "low" if (remaining < 35.0 or eff_thinking == "low") else eff_thinking
        active_mutator = mutator_fn if mutator_fn is not None else _make_hydrogen_mutator(eff_model, gen_thinking)

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

        strategies = [MUTATION_STRATEGIES[(gen - 1 + w) % len(MUTATION_STRATEGIES)] for w in range(pop_size)]
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

        # Select strict best across this generation (passing always beats failing)
        for rec in gen_records:
            ev = rec.eval_result
            is_better = (ev.passed and not best_eval.passed) or (
                ev.passed == best_eval.passed and ev.composite_score < best_eval.composite_score - 1e-6
            )
            if is_better:
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

        # Early exit when we already have a strong low-overhead compliant controller and stopped improving
        if best_eval.passed and (
            (best_eval.composite_score < 2.0 and gen >= 2) or stagnant_gens >= 1 and gen >= 2
        ):
            break

    # Guaranteed outcome resolution
    fallback_eval: CandidateEval | None = None
    if best_eval.passed:
        res = EvolutionResult(
            outcome="evolved_compliant",
            passed=True,
            model=eff_model,
            thinking_level=eff_thinking,
            best_code=best_code,
            best_eval=best_eval,
            seed_eval=seed_eval,
            fallback_eval=None,
            generations_completed=gens_completed,
            total_candidates_evaluated=total_evals,
            total_candidates_passed=total_passed,
            total_static_rejected=total_static_rejected,
            elapsed_s=time.perf_counter() - t_start,
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
                fallback_eval=fallback_eval,
                generations_completed=gens_completed,
                total_candidates_evaluated=total_evals + 1,
                total_candidates_passed=total_passed + 1,
                total_static_rejected=total_static_rejected,
                elapsed_s=time.perf_counter() - t_start,
                refusal_reason=None,
                history=history,
            )
        else:
            fail_summary = "; ".join(
                f"{f['workload']}:{f['constraint_id']}({f['measured']:.2f}>{f['limit']:.2f}{f['canonical_unit']})"
                for f in fallback_eval.failing_constraints[:4]
            )
            reason = (
                f"Refused: no evolved controller passed within {budget_s:.0f}s budget and conservative "
                f"fixed-floor fallback also failed ({fail_summary or fallback_eval.error})."
            )
            res = EvolutionResult(
                outcome="refused",
                passed=False,
                model=eff_model,
                thinking_level=eff_thinking,
                best_code=None,
                best_eval=best_eval,
                seed_eval=seed_eval,
                fallback_eval=fallback_eval,
                generations_completed=gens_completed,
                total_candidates_evaluated=total_evals + 1,
                total_candidates_passed=total_passed,
                total_static_rejected=total_static_rejected,
                elapsed_s=time.perf_counter() - t_start,
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
