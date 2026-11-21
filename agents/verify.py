"""Backend re-check of controllers and claimed counterexample workloads."""

from __future__ import annotations

from typing import Any

from evolve.sandbox import evaluate_controller_sandboxed
from rules.schema import Constraint, RuleSet
from workload.synth import WorkloadParams, generate_workload


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
