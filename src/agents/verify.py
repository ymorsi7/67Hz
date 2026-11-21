"""Backend re-check of controllers and claimed counterexample workloads."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from repo_paths import find_repo_root
from typing import Any

from evolve.sandbox import evaluate_controller_sandboxed
from rules.schema import Constraint, RuleSet
from workload.synth import WorkloadParams, all_preset_workloads, generate_workload

REPO_ROOT = find_repo_root()
OPEN_ISSUES_PATH = REPO_ROOT / "eval" / "attacks" / "open_issues.jsonl"
# Validation random suite used only by the fix-acceptance gate (never as a holdout score seed).
FIX_VALIDATION_SEED = 9001


def ruleset_from_constraints(
    constraints: list[dict[str, Any]] | list[Constraint],
    *,
    source_name: str = "agent",
    rated_mw: float = 100.0,
) -> RuleSet:
    parsed: list[Constraint] = []
    for c in constraints:
        if isinstance(c, Constraint):
            parsed.append(c)
        else:
            parsed.append(Constraint.from_dict(c))
    return RuleSet(source_name=source_name, rated_mw=float(rated_mw), constraints=parsed)


def workload_from_dict(name: str, raw: dict[str, Any], *, rated_mw: float = 100.0) -> WorkloadParams:
    data = dict(raw)
    data.setdefault("cluster_mw", rated_mw)
    if "family" not in data:
        dt = float(data.get("dt_s", 0.002))
        data["family"] = "slow" if dt >= 0.1 else "fast"
    fam = str(data["family"])
    defaults = {
        "duration_s": 20.0 if fam == "fast" else 1800.0,
        "dt_s": 0.002 if fam == "fast" else 0.5,
        "period_s": 0.05 if fam == "fast" else 30.0,
        "duty_cycle": 0.6,
        "depth": 0.55,
        "jitter": 0.05,
        "edge_s": 0.01 if fam == "fast" else 2.0,
        "idle_fraction": 0.15,
        "job_start_s": 0.5 if fam == "fast" else 120.0,
        "job_start_ramp_s": 0.05 if fam == "fast" else 10.0,
        "seed": 7,
        "family": fam,
    }
    for k, v in defaults.items():
        data.setdefault(k, v)
    fields = set(WorkloadParams.__dataclass_fields__)  # type: ignore[attr-defined]
    return WorkloadParams(**{k: data[k] for k in fields if k in data})


def recheck_controller(
    controller_code: str,
    ruleset: RuleSet,
    workloads: dict[str, WorkloadParams],
    *,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    ev = evaluate_controller_sandboxed(
        controller_code,
        ruleset,
        workloads,
        strict_review=False,
        timeout_s=timeout_s,
    )
    return {
        "verified_by": "backend_checker",
        "passed": bool(ev.passed),
        "eval": ev.to_dict(),
    }


def recheck_counterexamples(
    controller_code: str,
    ruleset: RuleSet,
    counterexamples: list[dict[str, Any]],
    *,
    rated_mw: float = 100.0,
) -> dict[str, Any]:
    """Re-run claimed breaks. A break is only accepted if the checker fails it."""
    verified: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for i, cx in enumerate(counterexamples):
        wl_raw = cx.get("workload") or cx.get("workload_params") or {}
        name = str(cx.get("name") or f"cx_{i:02d}")
        try:
            wp = workload_from_dict(name, wl_raw, rated_mw=rated_mw)
            # Ensure generate works (validates params)
            _ = generate_workload(wp)
            ev = evaluate_controller_sandboxed(
                controller_code,
                ruleset,
                {name: wp},
                strict_review=False,
                timeout_s=60.0,
            )
            row = {
                "name": name,
                "claimed": cx,
                "backend_passed": bool(ev.passed),
                "backend_eval": ev.to_dict(),
                "accepted_break": (not ev.passed),
            }
            if not ev.passed:
                verified.append(row)
            else:
                rejected.append({**row, "reason": "controller still passes backend checker"})
        except Exception as exc:
            rejected.append(
                {
                    "name": name,
                    "claimed": cx,
                    "accepted_break": False,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
    return {
        "verified_by": "backend_checker",
        "verified_breaks": verified,
        "rejected_claims": rejected,
        "break_found": len(verified) > 0,
    }


def _append_open_issue(row: dict[str, Any]) -> None:
    OPEN_ISSUES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OPEN_ISSUES_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def gate_fix(
    current_code: str,
    candidate_code: str,
    ruleset: RuleSet,
    counterexamples: list[dict[str, Any]],
    *,
    rated_mw: float | None = None,
    validation_seed: int = FIX_VALIDATION_SEED,
    n_validation: int = 20,
    profile_id: str | None = None,
    log_open_issue: bool = True,
) -> dict[str, Any]:
    """Accept a hardener/engineer fix only if it (a) clears every counterexample,
    (b) passes every preset, and (c) is at least as good as the current controller
    on the validation random suite (SEED=9001, gate-only).

    A rejected fix keeps ``current_code`` and optionally logs the counterexample
    as an open issue.
    """
    from eval.random_suite import sample_random_suite_for_ruleset

    mw = float(rated_mw if rated_mw is not None else ruleset.rated_mw)
    result: dict[str, Any] = {
        "accepted": False,
        "kept_code": "current",
        "controller_code": current_code,
        "validation_seed": validation_seed,
        "reason": None,
        "message": None,
        "worse_on_n": 0,
        "checks": {},
    }

    # (a) counterexamples must pass under the candidate
    cx_check = (
        recheck_counterexamples(candidate_code, ruleset, counterexamples, rated_mw=mw)
        if counterexamples
        else {"break_found": False, "verified_breaks": [], "rejected_claims": []}
    )
    result["checks"]["counterexamples"] = cx_check
    if cx_check.get("break_found"):
        result["reason"] = "counterexample_still_breaks"
        result["message"] = "fix rejected: counterexample still breaks the candidate"
        if log_open_issue:
            _log_rejected_fix_issue(result, profile_id, counterexamples, current_code, candidate_code)
        return result

    # (b) every preset must pass
    presets = all_preset_workloads(cluster_mw=mw)
    preset_ev = recheck_controller(candidate_code, ruleset, presets, timeout_s=60.0)
    result["checks"]["presets"] = {
        "passed": preset_ev["passed"],
        "workloads_passed": (preset_ev.get("eval") or {}).get("workloads_passed"),
        "workloads_evaluable": (preset_ev.get("eval") or {}).get("workloads_evaluable"),
    }
    if not preset_ev["passed"]:
        result["reason"] = "presets_failed"
        result["message"] = "fix rejected: candidate failed one or more preset workloads"
        if log_open_issue:
            _log_rejected_fix_issue(result, profile_id, counterexamples, current_code, candidate_code)
        return result

    # (c) validation random suite: candidate must pass at least as many as current
    suite = sample_random_suite_for_ruleset(ruleset, n=n_validation, seed=validation_seed, cluster_mw=mw)
    current_ev = recheck_controller(current_code, ruleset, suite, timeout_s=60.0)
    candidate_ev = recheck_controller(candidate_code, ruleset, suite, timeout_s=60.0)
    cur_n = int((current_ev.get("eval") or {}).get("workloads_passed") or 0)
    cand_n = int((candidate_ev.get("eval") or {}).get("workloads_passed") or 0)
    result["checks"]["validation"] = {
        "seed": validation_seed,
        "current_passed": cur_n,
        "candidate_passed": cand_n,
        "n": n_validation,
    }
    if cand_n < cur_n:
        worse_n = cur_n - cand_n
        result["reason"] = "validation_regression"
        result["worse_on_n"] = worse_n
        result["message"] = f"fix rejected: it made things worse on {worse_n} other workloads"
        if log_open_issue:
            _log_rejected_fix_issue(result, profile_id, counterexamples, current_code, candidate_code)
        return result

    result["accepted"] = True
    result["kept_code"] = "candidate"
    result["controller_code"] = candidate_code
    result["reason"] = "accepted"
    result["message"] = (
        f"fix accepted: counterexamples clear, presets pass, "
        f"validation SEED={validation_seed} {cand_n}/{n_validation} >= current {cur_n}/{n_validation}"
    )
    return result


def _log_rejected_fix_issue(
    gate: dict[str, Any],
    profile_id: str | None,
    counterexamples: list[dict[str, Any]],
    current_code: str,
    candidate_code: str,
) -> None:
    _append_open_issue(
        {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "event": "fix_rejected_open_issue",
            "profile_id": profile_id,
            "reason": gate.get("reason"),
            "message": gate.get("message"),
            "worse_on_n": gate.get("worse_on_n"),
            "validation": (gate.get("checks") or {}).get("validation"),
            "counterexamples": counterexamples,
            "current_code_bytes": len(current_code or ""),
            "candidate_code_bytes": len(candidate_code or ""),
            "kept": "current_controller",
        }
    )
