"""Fix-acceptance gate: CX + presets + validation SEED=9001 (non-regressing)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from agents.verify import FIX_VALIDATION_SEED, gate_fix
from rules.schema import Constraint, RuleSet


def _ruleset() -> RuleSet:
    return RuleSet(
        source_name="gate_test",
        rated_mw=100.0,
        constraints=[
            Constraint("ramp_rate", 20.0, "MW/min", page=1, snippet="20 MW/min", window_s=60.0, id="ramp_rate_0"),
        ],
    )


def _ev(*, passed: bool, workloads_passed: int, workloads_evaluable: int = 20) -> dict[str, Any]:
    return {
        "verified_by": "backend_checker",
        "passed": passed,
        "eval": {
            "passed": passed,
            "workloads_passed": workloads_passed,
            "workloads_evaluable": workloads_evaluable,
            "workloads_total": workloads_evaluable,
        },
    }


def _cx_ok() -> dict[str, Any]:
    return {
        "verified_by": "backend_checker",
        "verified_breaks": [],
        "rejected_claims": [],
        "break_found": False,
    }


def _cx_still_breaks() -> dict[str, Any]:
    return {
        "verified_by": "backend_checker",
        "verified_breaks": [{"name": "cx_0", "accepted_break": True}],
        "rejected_claims": [],
        "break_found": True,
    }


def test_gate_rejects_when_validation_suite_regresses() -> None:
    """Ungated lipa-style regression: fix clears CX but loses ground on SEED=9001."""
    ruleset = _ruleset()
    current = "def control(obs): return {'gpu_floor_mw':0.0,'gpu_cap_mw':100.0,'battery_setpoint_mw':0.0}\n"
    candidate = "def control(obs): return {'gpu_floor_mw':0.0,'gpu_cap_mw':90.0,'battery_setpoint_mw':0.0}\n"
    cxs = [{"workload": {"family": "slow", "period_s": 30.0}, "failed_constraint_id": "ramp_rate_0"}]

    calls: list[str] = []

    def fake_recheck_controller(code, ruleset, workloads, **kwargs):  # noqa: ANN001
        # Distinguish validation suite (n=20) from presets (fewer / different keys).
        n = len(workloads)
        if n >= 15:
            calls.append("validation")
            if code is current:
                return _ev(passed=False, workloads_passed=19)
            return _ev(passed=False, workloads_passed=13)
        calls.append("presets")
        return _ev(passed=True, workloads_passed=n or 1, workloads_evaluable=n or 1)

    with (
        patch("agents.verify.recheck_counterexamples", return_value=_cx_ok()),
        patch("agents.verify.recheck_controller", side_effect=fake_recheck_controller),
        patch("agents.verify._append_open_issue") as log_issue,
    ):
        gate = gate_fix(
            current,
            candidate,
            ruleset,
            cxs,
            validation_seed=FIX_VALIDATION_SEED,
            log_open_issue=True,
            profile_id="lipa",
        )

    assert gate["accepted"] is False
    assert gate["reason"] == "validation_regression"
    assert gate["worse_on_n"] == 6
    assert gate["controller_code"] == current
    assert gate["kept_code"] == "current"
    assert "made things worse on 6 other workloads" in (gate["message"] or "")
    assert gate["checks"]["validation"]["seed"] == FIX_VALIDATION_SEED
    assert gate["checks"]["validation"]["current_passed"] == 19
    assert gate["checks"]["validation"]["candidate_passed"] == 13
    log_issue.assert_called_once()
    assert "validation" in calls
    assert "presets" in calls


def test_gate_accepts_non_regressing_fix() -> None:
    ruleset = _ruleset()
    current = "CUR = True\ndef control(obs): return {'gpu_floor_mw':0.0,'gpu_cap_mw':100.0,'battery_setpoint_mw':0.0}\n"
    candidate = "FIX = True\ndef control(obs): return {'gpu_floor_mw':0.0,'gpu_cap_mw':100.0,'battery_setpoint_mw':0.0}\n"
    cxs = [{"workload": {"family": "slow"}, "failed_constraint_id": "ramp_rate_0"}]

    def fake_recheck_controller(code, ruleset, workloads, **kwargs):  # noqa: ANN001
        n = len(workloads)
        if n >= 15:
            return _ev(passed=True, workloads_passed=20 if code is candidate else 18)
        return _ev(passed=True, workloads_passed=n or 1, workloads_evaluable=n or 1)

    with (
        patch("agents.verify.recheck_counterexamples", return_value=_cx_ok()),
        patch("agents.verify.recheck_controller", side_effect=fake_recheck_controller),
        patch("agents.verify._append_open_issue") as log_issue,
    ):
        gate = gate_fix(current, candidate, ruleset, cxs, log_open_issue=True)

    assert gate["accepted"] is True
    assert gate["controller_code"] == candidate
    assert gate["kept_code"] == "candidate"
    log_issue.assert_not_called()


def test_gate_rejects_when_counterexample_still_breaks() -> None:
    ruleset = _ruleset()
    with (
        patch("agents.verify.recheck_counterexamples", return_value=_cx_still_breaks()),
        patch("agents.verify.recheck_controller") as recheck_ctl,
        patch("agents.verify._append_open_issue"),
    ):
        gate = gate_fix("cur", "cand", ruleset, [{"workload": {}}], log_open_issue=True)
    assert gate["accepted"] is False
    assert gate["reason"] == "counterexample_still_breaks"
    recheck_ctl.assert_not_called()
