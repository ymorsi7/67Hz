"""Phase 3 & 3b tests: seed controller, static safety bans, subprocess sandbox, evolve loop, B3, random suite, profiles."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from eval.random_suite import (
    evaluable_families_for_ruleset,
    sample_random_suite,
    sample_random_suite_for_ruleset,
)
from evolve.baselines import (
    run_b1_baseline,
    run_b3_baseline,
    run_uncontrolled_baseline,
)
from evolve.loop import evolve_controller
from evolve.sandbox import (
    _sanitized_env,
    evaluate_controller_sandboxed,
    validate_controller_code,
)
from rules.profiles import load_utility_profiles
from rules.schema import Constraint, RuleSet
from workload.synth import (
    WorkloadParams,
    all_preset_workloads,
    preset_workloads,
    slow_preset_workloads,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_sandbox_packages_have_no_scipy_pymupdf_or_pydantic() -> None:
    """Decision #4: sandbox-run packages use pure Python + numpy only."""
    forbidden = {"scipy", "fitz", "pymupdf", "pydantic"}
    for pkg in ("workload", "sim", "rules", "controllers", "evolve"):
        for py_file in (REPO_ROOT / "src" / pkg).glob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        assert top not in forbidden, f"{py_file} imports forbidden module {top}"
                elif isinstance(node, ast.ImportFrom) and node.module:
                    top = node.module.split(".")[0]
                    assert top not in forbidden, f"{py_file} imports forbidden module {top}"


def test_seed_controller_under_60_lines_and_valid() -> None:
    seed_path = REPO_ROOT / "src" / "controllers" / "seed.py"
    code = seed_path.read_text(encoding="utf-8")
    assert len(code.splitlines()) < 60
    res = validate_controller_code(code)
    assert res.ok, f"Seed controller failed static validation: {res.violations}"

    ruleset = RuleSet(
        source_name="test_seed",
        rated_mw=100.0,
        constraints=[Constraint("ramp_rate", 20.0, "MW/min", page=1, snippet="20 MW/min", window_s=60.0)],
    )
    wl = {"slow_one": slow_preset_workloads(100.0)["slow_training_run"]}
    ev = evaluate_controller_sandboxed(code, ruleset, wl)
    assert ev.error is None
    assert ev.workloads_total == 1
    assert ev.workloads_evaluable == 1


def test_known_bad_controller_fails_gate() -> None:
    """Gate test: a known-bad controller must fail the deterministic compliance checker."""
    bad_code = """
import numpy as np

def control(obs: dict) -> dict[str, float]:
    # Intentionally amplifies fluctuations by discharging on valleys and charging on peaks
    p_raw = float(obs["p_raw_mw"])
    rated = float(obs["rated_mw"])
    b_max = float(obs["battery_max_power_mw"])
    batt = -b_max if p_raw > 0.6 * rated else b_max
    return {"gpu_floor_mw": 0.0, "gpu_cap_mw": rated, "battery_setpoint_mw": batt}
"""
    ruleset = RuleSet(
        source_name="strict_ercot",
        rated_mw=100.0,
        constraints=[
            Constraint("ramp_rate", 20.0, "MW/min", page=14, snippet="ERCOT: 20 MW/min"),
            Constraint("max_step", 10.0, "MW", page=23, snippet="10 MW over 5s", window_s=5.0),
        ],
    )
    wl = {
        "standard_training": preset_workloads(100.0)["standard_training"],
        "slow_training_run": slow_preset_workloads(100.0)["slow_training_run"],
    }
    ev = evaluate_controller_sandboxed(bad_code, ruleset, wl)
    assert ev.passed is False
    assert ev.workloads_passed == 0
    assert len(ev.failing_constraints) >= 2
    assert ev.worst_margin_pct is not None and ev.worst_margin_pct < 0.0


@pytest.mark.parametrize(
    "malicious_code",
    [
        "import os\ndef control(obs):\n    os.system('echo hi')\n    return {'gpu_floor_mw': 0, 'gpu_cap_mw': 100, 'battery_setpoint_mw': 0}\n",
        "import subprocess\ndef control(obs):\n    return {'gpu_floor_mw': 0, 'gpu_cap_mw': 100, 'battery_setpoint_mw': 0}\n",
        "import socket\ndef control(obs):\n    return {'gpu_floor_mw': 0, 'gpu_cap_mw': 100, 'battery_setpoint_mw': 0}\n",
        "import urllib.request\ndef control(obs):\n    return {'gpu_floor_mw': 0, 'gpu_cap_mw': 100, 'battery_setpoint_mw': 0}\n",
        "import importlib\ndef control(obs):\n    return {'gpu_floor_mw': 0, 'gpu_cap_mw': 100, 'battery_setpoint_mw': 0}\n",
        "def control(obs):\n    open('/etc/passwd')\n    return {'gpu_floor_mw': 0, 'gpu_cap_mw': 100, 'battery_setpoint_mw': 0}\n",
        "def control(obs):\n    eval('1+1')\n    return {'gpu_floor_mw': 0, 'gpu_cap_mw': 100, 'battery_setpoint_mw': 0}\n",
        "def control(obs):\n    __import__('os')\n    return {'gpu_floor_mw': 0, 'gpu_cap_mw': 100, 'battery_setpoint_mw': 0}\n",
        "def control(obs):\n    x = (1).__class__.__bases__[0].__subclasses__()\n    return {'gpu_floor_mw': 0, 'gpu_cap_mw': 100, 'battery_setpoint_mw': 0}\n",
        "import numpy as np\ndef control(obs):\n    np.loadtxt('/etc/passwd')\n    return {'gpu_floor_mw': 0, 'gpu_cap_mw': 100, 'battery_setpoint_mw': 0}\n",
        "def not_control(obs):\n    return {}\n",
    ],
)
def test_banned_code_is_rejected(malicious_code: str) -> None:
    """Gate test: file, network, dynamic exec, dunder, and import tricks are statically rejected."""
    static = validate_controller_code(malicious_code)
    assert static.ok is False
    assert len(static.violations) >= 1

    ruleset = RuleSet(
        source_name="test",
        rated_mw=100.0,
        constraints=[Constraint("ramp_rate", 20.0, "MW/min", page=1, snippet="20 MW/min")],
    )
    wl = {"short": WorkloadParams(cluster_mw=100.0, duration_s=0.2, dt_s=0.01)}
    ev = evaluate_controller_sandboxed(malicious_code, ruleset, wl)
    assert ev.passed is False
    assert len(ev.static_violations) >= 1


def test_sandbox_timeout_kills_infinite_loop() -> None:
    hang_code = """
def control(obs: dict) -> dict[str, float]:
    while True:
        pass
"""
    ruleset = RuleSet(
        source_name="test",
        rated_mw=100.0,
        constraints=[Constraint("ramp_rate", 20.0, "MW/min", page=1, snippet="20 MW/min")],
    )
    wl = {"short": WorkloadParams(cluster_mw=100.0, duration_s=0.2, dt_s=0.01)}
    ev = evaluate_controller_sandboxed(hang_code, ruleset, wl, timeout_s=1.0)
    assert ev.passed is False
    assert ev.timed_out is True


def test_sandbox_strips_api_key_from_child_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "secret-test-key-should-never-leak")
    env = _sanitized_env()
    assert "GEMINI_API_KEY" not in env


def test_t0_jump_controller_rejected_while_idle_start_controller_passes() -> None:
    """Step 3: controller jumping to mean power at t=0 MUST fail; idle-start slew controller passes."""
    ruleset = RuleSet(
        source_name="mock_ercot",
        rated_mw=100.0,
        constraints=[
            Constraint("ramp_rate", 20.0, "MW/min", page=14, snippet="ERCOT: 20 MW/min"),
            Constraint("max_step", 10.0, "MW", page=23, snippet="10 MW over 5s", window_s=5.0),
        ],
    )
    wl = {
        "standard_training": preset_workloads(100.0)["standard_training"],
        "slow_training_run": slow_preset_workloads(100.0)["slow_training_run"],
    }

    cheat_jump_code = """
import math
def control(obs: dict) -> dict[str, float]:
    p_raw = float(obs["p_raw_mw"])
    rated = float(obs["rated_mw"])
    dt = float(obs["dt"])
    b_max = float(obs["battery_max_power_mw"])
    cap = float(obs["battery_capacity_mwh"])
    e = float(obs["battery_energy_mwh"])
    # Cheats by jumping immediately to 72 MW at t=0
    p_target = 0.72 * rated
    dt_h = dt / 3600.0
    eta = math.sqrt(0.90)
    b_dis_max = min(b_max, max(0.0, (e - 0.1 * cap) * eta / dt_h))
    b_chg_max = min(b_max, max(0.0, (0.9 * cap - e) / (eta * dt_h)))
    if p_target < p_raw - b_dis_max:
        return {"gpu_floor_mw": 0.0, "gpu_cap_mw": p_target + b_dis_max, "battery_setpoint_mw": b_dis_max}
    if p_target > p_raw + b_chg_max:
        return {"gpu_floor_mw": p_target - b_chg_max, "gpu_cap_mw": rated, "battery_setpoint_mw": -b_chg_max}
    return {"gpu_floor_mw": 0.0, "gpu_cap_mw": rated, "battery_setpoint_mw": p_raw - p_target}
"""
    ev_cheat = evaluate_controller_sandboxed(cheat_jump_code, ruleset, wl)
    assert ev_cheat.passed is False, "t=0 jump to 72 MW must fail when idle is prepended!"

    honest_idle_code = """
import math
def control(obs: dict) -> dict[str, float]:
    p_raw = float(obs["p_raw_mw"])
    rated = float(obs["rated_mw"])
    dt = float(obs["dt"])
    b_max = float(obs["battery_max_power_mw"])
    cap = float(obs["battery_capacity_mwh"])
    e = float(obs["battery_energy_mwh"])
    idle = float(obs["idle_mw"])
    st = obs["state"]
    if "target" not in st:
        st["target"] = idle
    max_delta = 0.30 * dt  # 18 MW/min <= 20 MW/min AND <= 1.5 MW per 5s <= 10 MW
    delta = max(-max_delta, min(max_delta, p_raw - st["target"]))
    p_target = st["target"] + delta
    st["target"] = p_target
    dt_h = dt / 3600.0
    eta = math.sqrt(0.90)
    b_dis_max = min(b_max, max(0.0, (e - 0.1 * cap) * eta / dt_h))
    b_chg_max = min(b_max, max(0.0, (0.9 * cap - e) / (eta * dt_h)))
    if p_target < p_raw - b_dis_max:
        return {"gpu_floor_mw": 0.0, "gpu_cap_mw": p_target + b_dis_max, "battery_setpoint_mw": b_dis_max}
    if p_target > p_raw + b_chg_max:
        return {"gpu_floor_mw": p_target - b_chg_max, "gpu_cap_mw": rated, "battery_setpoint_mw": -b_chg_max}
    return {"gpu_floor_mw": 0.0, "gpu_cap_mw": rated, "battery_setpoint_mw": p_raw - p_target}
"""
    ev_honest = evaluate_controller_sandboxed(honest_idle_code, ruleset, wl)
    assert ev_honest.passed is True
    assert ev_honest.family_summary["fast"]["status"] == "pass"
    assert ev_honest.family_summary["slow"]["status"] == "pass"


def test_evolve_loop_mocked_converges_streams_and_beats_b1() -> None:
    """Verify evolution loop rejects banned candidates, accepts compliant idle-start ones, and streams events."""
    ruleset = RuleSet(
        source_name="mock_ercot",
        rated_mw=100.0,
        constraints=[
            Constraint("ramp_rate", 20.0, "MW/min", page=14, snippet="ERCOT: 20 MW/min"),
            Constraint("max_step", 10.0, "MW", page=23, snippet="10 MW over 5s", window_s=5.0),
        ],
    )
    wl = {
        "standard_training": preset_workloads(100.0)["standard_training"],
        "slow_training_run": slow_preset_workloads(100.0)["slow_training_run"],
    }

    good_battery_code = """
import math

def control(obs: dict) -> dict[str, float]:
    p_raw = float(obs["p_raw_mw"])
    rated = float(obs["rated_mw"])
    dt = float(obs["dt"])
    b_max = float(obs["battery_max_power_mw"])
    cap = float(obs["battery_capacity_mwh"])
    e = float(obs["battery_energy_mwh"])
    idle = float(obs["idle_mw"])
    state = obs["state"]

    if "target" not in state:
        state["target"] = idle
    max_delta = 0.30 * dt
    delta = max(-max_delta, min(max_delta, p_raw - state["target"]))
    p_target = state["target"] + delta
    state["target"] = p_target

    dt_h = dt / 3600.0
    eta = math.sqrt(0.90)
    b_dis_max = min(b_max, max(0.0, (e - 0.1 * cap) * eta / dt_h))
    b_chg_max = min(b_max, max(0.0, (0.9 * cap - e) / (eta * dt_h)))

    if p_target < p_raw - b_dis_max:
        return {"gpu_floor_mw": 0.0, "gpu_cap_mw": p_target + b_dis_max, "battery_setpoint_mw": b_dis_max}
    if p_target > p_raw + b_chg_max:
        return {"gpu_floor_mw": p_target - b_chg_max, "gpu_cap_mw": rated, "battery_setpoint_mw": -b_chg_max}
    return {"gpu_floor_mw": 0.0, "gpu_cap_mw": rated, "battery_setpoint_mw": p_raw - p_target}
"""
    banned_code = "import os\ndef control(obs):\n    return {'gpu_floor_mw': 0, 'gpu_cap_mw': 100, 'battery_setpoint_mw': 0}\n"
    failing_code = "def control(obs):\n    return {'gpu_floor_mw': 0.0, 'gpu_cap_mw': float(obs['rated_mw']), 'battery_setpoint_mw': 0.0}\n"

    def mock_mutator(sys_inst: str, prompt: str, gen: int, worker_id: int) -> str:
        if worker_id == 0:
            return banned_code
        if worker_id == 1:
            return good_battery_code
        return failing_code

    events: list[dict] = []
    res = evolve_controller(
        ruleset,
        wl,
        population_size=3,
        max_generations=2,
        budget_s=30.0,
        mutator_fn=mock_mutator,
        on_event=events.append,
        seed=101,
    )
    assert res.outcome == "evolved_compliant"
    assert res.passed is True
    assert res.total_static_rejected >= 1
    assert res.best_eval is not None and res.best_eval.mean_energy_overhead_pct < 10.0

    event_types = [e["event"] for e in events]
    assert "evolve_start" in event_types
    assert "seed_evaluated" in event_types
    assert "generation_start" in event_types
    assert "candidate_done" in event_types
    assert "new_best" in event_types
    assert event_types[-1] == "evolve_complete"


def test_evolve_loop_four_rung_ladder_and_never_worse_than_b3() -> None:
    """Verify all 4 Designer rungs: evolved_compliant (<= B3) -> fallback_b3 -> fallback_conservative_floor -> refused."""
    # Rung 1 & 2: when Hydrogen emits bad/worse candidates but B3 passes -> fallback_b3 and NEVER worse than B3!
    ramp_ruleset = RuleSet(
        source_name="test_b3_rung",
        rated_mw=100.0,
        constraints=[Constraint("ramp_rate", 20.0, "MW/min", page=14, snippet="20 MW/min")],
    )
    wl_slow = {"slow": slow_preset_workloads(100.0)["slow_training_run"]}

    def always_bad(sys_inst: str, prompt: str, gen: int, worker_id: int) -> str:
        return "def control(obs):\n    return {'gpu_floor_mw': 0.0, 'gpu_cap_mw': float(obs['rated_mw']), 'battery_setpoint_mw': 0.0}\n"

    b3_direct = run_b3_baseline(ramp_ruleset, wl_slow, n_evals=12, seed=101)
    assert b3_direct.eval_result.passed is True

    res_b3_rung = evolve_controller(
        ramp_ruleset,
        wl_slow,
        population_size=2,
        max_generations=1,
        budget_s=20.0,
        seed=101,
        b3_n_evals=12,
        mutator_fn=always_bad,
    )
    assert res_b3_rung.outcome == "fallback_b3"
    assert res_b3_rung.passed is True
    assert res_b3_rung.b3_eval is not None and res_b3_rung.best_eval is not None
    assert res_b3_rung.best_eval.composite_score <= b3_direct.eval_result.composite_score + 1e-6

    # Even if Hydrogen returns a *passing* candidate that scores WORSE than B3, Designer must keep B3 (never worse than B3)!
    worse_passing_code = """
import math
def control(obs: dict) -> dict[str, float]:
    p_raw = float(obs["p_raw_mw"])
    rated = float(obs["rated_mw"])
    dt = float(obs["dt"])
    idle = float(obs["idle_mw"])
    st = obs["state"]
    # Legal ultra-slow ramp (0.05 MW/s = 3 MW/min << 20 MW/min) with huge curtailment -> passes but terrible score
    if "pg" not in st:
        st["pg"] = idle
    p_target = st["pg"] + max(-0.05 * dt, min(0.05 * dt, p_raw - st["pg"]))
    st["pg"] = p_target
    return {"gpu_floor_mw": p_target, "gpu_cap_mw": p_target, "battery_setpoint_mw": 0.0}
"""
    res_never_worse = evolve_controller(
        ramp_ruleset,
        wl_slow,
        population_size=2,
        max_generations=1,
        budget_s=20.0,
        seed=101,
        b3_n_evals=12,
        mutator_fn=lambda *a: worse_passing_code,
    )
    assert res_never_worse.passed is True
    assert res_never_worse.outcome == "fallback_b3"
    assert res_never_worse.best_eval is not None
    assert res_never_worse.best_eval.composite_score <= b3_direct.eval_result.composite_score + 1e-6

    # Rung 3: conservative floor fallback when neither evolved nor B3 passes (tested cleanly with enable_b3_seed=False)
    freq_ruleset = RuleSet(
        source_name="test_fallback_freq",
        rated_mw=100.0,
        constraints=[
            Constraint("fluctuation_band", 3.5, "MW", page=25, snippet="5-55 Hz < 3.5 MW", freq_min_hz=5.0, freq_max_hz=55.0)
        ],
    )
    wl_fast = {"ssr1": preset_workloads(100.0)["ssr_mode1_attack"]}
    res_fb = evolve_controller(
        freq_ruleset,
        wl_fast,
        seed_code=always_bad("", "", 0, 0),
        population_size=2,
        max_generations=1,
        budget_s=20.0,
        enable_b3_seed=False,
        mutator_fn=always_bad,
    )
    assert res_fb.outcome == "fallback_conservative_floor"
    assert res_fb.passed is True
    assert res_fb.fallback_eval is not None and res_fb.fallback_eval.passed is True

    # Rung 4: refusal when evolved, B3, and conservative floor all fail
    res_ref_ramp = evolve_controller(
        ramp_ruleset,
        wl_slow,
        population_size=2,
        max_generations=1,
        budget_s=20.0,
        enable_b3_seed=False,
        mutator_fn=always_bad,
    )
    assert res_ref_ramp.outcome == "refused"
    assert res_ref_ramp.passed is False

    # Unverified constraint needing human review -> immediate refusal (even with B3 enabled)
    review_ruleset = RuleSet(
        source_name="needs_review_doc",
        rated_mw=100.0,
        constraints=[
            Constraint(
                "ramp_rate",
                20.0,
                "MW/min",
                page=13,
                snippet="20 MW/min",
                needs_human_review=True,
                verified=None,
            )
        ],
    )
    res_ref = evolve_controller(
        review_ruleset,
        wl_slow,
        population_size=2,
        max_generations=1,
        budget_s=20.0,
        mutator_fn=always_bad,
    )
    assert res_ref.outcome == "refused"
    assert res_ref.passed is False
    assert res_ref.refusal_reason and "human review" in res_ref.refusal_reason.lower()


def test_b3_random_search_and_random_suite_determinism() -> None:
    """Steps 4 & 6 + Part A.3: B3 works without LLM; every profile gets 20 evaluable random workloads."""
    suite_a = sample_random_suite(n=20, seed=777)
    suite_b = sample_random_suite(n=20, seed=777)
    assert len(suite_a) == 20
    assert {k: v.to_dict() for k, v in suite_a.items()} == {k: v.to_dict() for k, v in suite_b.items()}
    assert sum(1 for v in suite_a.values() if v.family == "fast") == 10
    assert sum(1 for v in suite_a.values() if v.family == "slow") == 10
    assert all(v.job_start_s > 0.0 for v in suite_a.values())

    # Slow-primary profile (20 MW/min): slow + band_long are evaluable; sampler still prefers slow.
    slow_only_rules = RuleSet(
        source_name="slow_only_test",
        rated_mw=100.0,
        constraints=[Constraint("ramp_rate", 20.0, "MW/min", page=14, snippet="20 MW/min")],
    )
    assert evaluable_families_for_ruleset(slow_only_rules) == ["slow", "band_long"]
    slow_suite = sample_random_suite_for_ruleset(slow_only_rules, n=20, seed=777)
    assert len(slow_suite) == 20
    assert all(v.family == "slow" for v in slow_suite.values())
    ev_slow = evaluate_controller_sandboxed(
        "def control(obs):\n    return {'gpu_floor_mw': 0.0, 'gpu_cap_mw': float(obs['rated_mw']), 'battery_setpoint_mw': 0.0}\n",
        slow_only_rules,
        slow_suite,
    )
    assert ev_slow.workloads_evaluable == 20
    assert ev_slow.workloads_not_evaluable == 0

    # Fast-primary profile (5-55 Hz band): fast + band_long evaluable; sampler still prefers fast.
    fast_only_rules = RuleSet(
        source_name="fast_only_test",
        rated_mw=100.0,
        constraints=[
            Constraint("fluctuation_band", 3.5, "MW", page=11, snippet="5-55 Hz < 3.5 MW", freq_min_hz=5.0, freq_max_hz=55.0)
        ],
    )
    assert evaluable_families_for_ruleset(fast_only_rules) == ["fast", "band_long"]
    fast_suite = sample_random_suite_for_ruleset(fast_only_rules, n=20, seed=777)
    assert len(fast_suite) == 20
    assert all(v.family == "fast" for v in fast_suite.values())

    # Every dev utility profile gets exactly 20 evaluable workloads (held_out reserved for make eval-final)
    for prof in load_utility_profiles(rated_mw=100.0, split="dev"):
        psuite = sample_random_suite_for_ruleset(prof.ruleset, n=20, seed=777)
        assert len(psuite) == 20
        fams = set(evaluable_families_for_ruleset(prof.ruleset))
        assert all(w.family in fams for w in psuite.values()), prof.profile_id

    wl = {"slow": slow_preset_workloads(100.0)["slow_training_run"]}
    b3 = run_b3_baseline(slow_only_rules, wl, n_evals=8, seed=101)
    assert b3.baseline == "B3"
    assert b3.n_evals == 8
    assert validate_controller_code(b3.code).ok
    assert b3.eval_result.error is None


def test_utility_profiles_dual_family_and_entity_coverage() -> None:
    """Verify all 11 utility profiles load with entity attribution and evaluate cleanly on both families."""
    profiles = load_utility_profiles(rated_mw=100.0)
    dev_utils = {p.utility for p in profiles if p.split == "dev"}
    held_utils = {p.utility for p in profiles if p.split == "held_out"}

    assert {"CAISO", "ERCOT", "ATC", "AESO", "Southern", "LIPA"}.issubset(dev_utils)
    assert {"IESO", "MISO", "Southern", "SPP"}.issubset(held_utils)
    for prof in profiles:
        assert all(c.entity for c in prof.ruleset.constraints), f"Missing entity on {prof.profile_id}"

    workloads = all_preset_workloads(100.0)
    # Run simulations on dev profiles only here; held_out profiles run exclusively in `make eval-final`.
    for prof in [p for p in profiles if p.split == "dev"]:
        noop_res = run_uncontrolled_baseline(prof.ruleset, workloads)
        assert len(noop_res.eval_result.unevaluated_constraints) == 0, (
            f"{prof.profile_id} left unevaluated constraints: {noop_res.eval_result.unevaluated_constraints}"
        )
        # Note: CAISO 20 MW/min over 600 s = 200 MW/10 min, which is physically non-binding on a 100 MW cluster
        # (binds on clusters > 235 MW). Every other profile MUST fail uncontrolled at 100 MW.
        if prof.profile_id == "caiso":
            assert noop_res.eval_result.family_summary["fast"]["status"] == "not_evaluable"
            assert noop_res.eval_result.family_summary["slow"]["status"] == "pass"
        else:
            assert noop_res.eval_result.passed is False, f"Uncontrolled unexpectedly passed {prof.profile_id}"
            assert len(noop_res.eval_result.failing_constraints) >= 1

        b1_res = run_b1_baseline(prof.ruleset, workloads)
        assert b1_res.eval_result.error is None
        assert b1_res.eval_result.workloads_total == len(workloads)
