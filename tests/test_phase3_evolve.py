"""Phase 3 tests: seed controller, static safety bans, subprocess sandbox, evolve loop, profiles, baselines."""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from evolve.baselines import run_b1_baseline, run_uncontrolled_baseline
from evolve.loop import evolve_controller
from evolve.sandbox import (
    _sanitized_env,
    evaluate_controller_sandboxed,
    validate_controller_code,
)
from rules.profiles import load_utility_profiles
from rules.schema import Constraint, RuleSet
from workload.synth import WorkloadParams, preset_workloads

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_sandbox_packages_have_no_scipy_pymupdf_or_pydantic() -> None:
    """Decision #4: sandbox-run packages use pure Python + numpy only."""
    forbidden = {"scipy", "fitz", "pymupdf", "pydantic"}
    for pkg in ("workload", "sim", "rules", "controllers", "evolve"):
        for py_file in (REPO_ROOT / pkg).glob("*.py"):
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
    seed_path = REPO_ROOT / "controllers" / "seed.py"
    code = seed_path.read_text(encoding="utf-8")
    assert len(code.splitlines()) < 60
    res = validate_controller_code(code)
    assert res.ok, f"Seed controller failed static validation: {res.violations}"

    ruleset = RuleSet(
        source_name="test_seed",
        rated_mw=100.0,
        constraints=[Constraint("ramp_rate", 20.0, "MW/min", page=1, snippet="20 MW/min")],
    )
    wl = {"short": WorkloadParams(cluster_mw=100.0, duration_s=1.0, dt_s=0.002, seed=42)}
    ev = evaluate_controller_sandboxed(code, ruleset, wl)
    assert ev.error is None
    assert ev.workloads_total == 1


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
    wl = {"standard_training": preset_workloads(100.0)["standard_training"]}
    ev = evaluate_controller_sandboxed(bad_code, ruleset, wl)
    assert ev.passed is False
    assert ev.workloads_passed == 0
    assert len(ev.failing_constraints) == 2
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


def test_evolve_loop_mocked_converges_streams_and_beats_b1() -> None:
    """Verify evolution loop rejects banned candidates, accepts compliant ones, streams events, beats B1."""
    ruleset = RuleSet(
        source_name="mock_ercot",
        rated_mw=100.0,
        constraints=[
            Constraint("ramp_rate", 20.0, "MW/min", page=14, snippet="ERCOT: 20 MW/min"),
            Constraint("max_step", 10.0, "MW", page=23, snippet="10 MW over 5s", window_s=5.0),
        ],
    )
    wl = {"standard_training": preset_workloads(100.0)["standard_training"]}

    good_battery_code = """
import math

def control(obs: dict) -> dict[str, float]:
    p_raw = float(obs["p_raw_mw"])
    rated = float(obs["rated_mw"])
    dt = float(obs["dt"])
    b_max = float(obs["battery_max_power_mw"])
    cap = float(obs["battery_capacity_mwh"])
    e = float(obs["battery_energy_mwh"])
    state = obs["state"]

    # Hold target steady near mean load (72 MW) with strict slew rate <= 0.25 MW/s
    if "target" not in state:
        state["target"] = 0.72 * rated
    max_delta = 0.25 * dt
    desired = 0.72 * rated
    delta = max(-max_delta, min(max_delta, desired - state["target"]))
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
    )
    assert res.outcome == "evolved_compliant"
    assert res.passed is True
    assert res.total_static_rejected >= 1
    assert res.best_eval is not None and res.best_eval.mean_energy_overhead_pct < 15.0

    event_types = [e["event"] for e in events]
    assert "evolve_start" in event_types
    assert "seed_evaluated" in event_types
    assert "generation_start" in event_types
    assert "candidate_done" in event_types
    assert "new_best" in event_types
    assert event_types[-1] == "evolve_complete"


def test_evolve_loop_guaranteed_fallback_and_explicit_refusal() -> None:
    """Guaranteed outcome: conservative floor fallback if evolution fails; explicit refusal if uncertifiable."""
    ruleset = RuleSet(
        source_name="test_fallback",
        rated_mw=100.0,
        constraints=[Constraint("ramp_rate", 20.0, "MW/min", page=14, snippet="20 MW/min")],
    )
    wl = {"short": WorkloadParams(cluster_mw=100.0, duration_s=1.0, dt_s=0.002)}

    def always_bad(sys_inst: str, prompt: str, gen: int, worker_id: int) -> str:
        return "def control(obs):\n    return {'gpu_floor_mw': 0.0, 'gpu_cap_mw': float(obs['rated_mw']), 'battery_setpoint_mw': 0.0}\n"

    res_fb = evolve_controller(
        ruleset,
        wl,
        population_size=2,
        max_generations=1,
        budget_s=20.0,
        mutator_fn=always_bad,
    )
    assert res_fb.outcome == "fallback_conservative_floor"
    assert res_fb.passed is True
    assert res_fb.fallback_eval is not None and res_fb.fallback_eval.passed is True

    # Unverified constraint needing human review -> explicit refusal
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
        wl,
        population_size=2,
        max_generations=1,
        budget_s=20.0,
        mutator_fn=always_bad,
    )
    assert res_ref.outcome == "refused"
    assert res_ref.passed is False
    assert res_ref.refusal_reason and "human review" in res_ref.refusal_reason.lower()


def test_utility_profiles_and_uncontrolled_binding() -> None:
    """Verify all 6 dev + held_out utility profiles load and fail uncontrolled on default workloads."""
    profiles = load_utility_profiles(rated_mw=100.0)
    dev_utils = {p.utility for p in profiles if p.split == "dev"}
    held_utils = {p.utility for p in profiles if p.split == "held_out"}

    assert {"CAISO", "ERCOT", "ATC", "AESO", "Southern", "LIPA"}.issubset(dev_utils)
    assert "Southern" in held_utils

    workloads = preset_workloads(100.0)
    for prof in profiles:
        noop_res = run_uncontrolled_baseline(prof.ruleset, workloads)
        assert noop_res.eval_result.passed is False, f"Uncontrolled unexpectedly passed {prof.profile_id}"
        assert len(noop_res.eval_result.failing_constraints) >= 1

        b1_res = run_b1_baseline(prof.ruleset, workloads)
        assert b1_res.eval_result.error is None
        assert b1_res.eval_result.workloads_total == len(workloads)
