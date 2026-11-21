"""Static AST validator and isolated subprocess sandbox for candidate controllers.

Pure Python + numpy only (sandbox-safe). Enforces static bans on filesystem,
network, subprocess, dynamic code execution, and dunder/import escape tricks,
then executes candidate controllers in an isolated subprocess with a sanitized
environment (zero secrets/API keys) and a hard wall-clock timeout.
"""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from rules.schema import RuleSet
from sim.plant import BatteryConfig, PlantConfig
from workload.synth import WorkloadParams

RUNNER_PATH = Path(__file__).resolve().parent / "_runner.py"

ALLOWED_IMPORTS = frozenset({"math", "numpy", "__future__"})

BANNED_CALL_NAMES = frozenset(
    {
        "__import__",
        "breakpoint",
        "classmethod",
        "compile",
        "delattr",
        "dir",
        "eval",
        "exec",
        "getattr",
        "globals",
        "help",
        "input",
        "locals",
        "memoryview",
        "object",
        "open",
        "setattr",
        "staticmethod",
        "super",
        "type",
        "vars",
    }
)

BANNED_NAMES = frozenset(
    {
        "__builtins__",
        "__import__",
        "__loader__",
        "__spec__",
        "__file__",
        "__cached__",
        "__package__",
        "__debug__",
    }
)

MAX_CODE_BYTES = 12_000
MAX_CODE_LINES = 160


@dataclass
class StaticValidationResult:
    ok: bool
    violations: list[str] = field(default_factory=list)


@dataclass
class CandidateEval:
    """Sandboxed deterministic evaluation of a candidate controller across scenario families."""

    passed: bool
    workloads_passed: int
    workloads_total: int
    composite_score: float
    mean_energy_overhead_pct: float
    mean_throughput_loss_pct: float
    mean_battery_wear: float
    max_shaft_torque_pu: float
    worst_margin_pct: float | None
    worst_constraint_id: str | None
    failing_constraints: list[dict[str, Any]] = field(default_factory=list)
    per_workload: list[dict[str, Any]] = field(default_factory=list)
    static_violations: list[str] = field(default_factory=list)
    timed_out: bool = False
    error: str | None = None
    elapsed_s: float = 0.0
    workloads_evaluable: int = 0
    workloads_not_evaluable: int = 0
    unevaluated_constraints: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    family_summary: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_controller_code(code: str, *, max_lines: int = MAX_CODE_LINES) -> StaticValidationResult:
    """Statically inspect candidate controller AST to ban file, network, and import tricks."""
    violations: list[str] = []
    if not code or not code.strip():
        return StaticValidationResult(ok=False, violations=["Empty controller code."])

    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        violations.append(f"Controller exceeds max size ({MAX_CODE_BYTES} bytes).")

    line_count = len(code.splitlines())
    if line_count > max_lines:
        violations.append(f"Controller exceeds max line limit ({line_count} > {max_lines} lines).")

    try:
        tree = ast.parse(code, filename="<candidate>")
    except SyntaxError as exc:
        return StaticValidationResult(ok=False, violations=[f"SyntaxError at line {exc.lineno}: {exc.msg}"])

    has_control_def = False

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "control":
            total_args = len(node.args.posonlyargs) + len(node.args.args)
            if total_args >= 1 or node.args.vararg is not None:
                has_control_def = True

        elif isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in ALLOWED_IMPORTS:
                    violations.append(f"Banned import: '{alias.name}' (only math and numpy allowed).")

        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                violations.append("Relative imports are prohibited.")
            else:
                mod = (node.module or "").split(".")[0]
                if mod not in ALLOWED_IMPORTS:
                    violations.append(f"Banned import-from: '{node.module}' (only math and numpy allowed).")
                if mod == "__future__":
                    for alias in node.names:
                        if alias.name != "annotations":
                            violations.append(f"Banned __future__ import: '{alias.name}'.")

        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BANNED_CALL_NAMES:
                violations.append(f"Banned builtin call: '{func.id}()'.")
            elif isinstance(func, ast.Attribute) and func.attr in {
                "system",
                "popen",
                "exec",
                "eval",
                "open",
                "load",
                "loads",
                "save",
                "savetxt",
                "loadtxt",
                "fromfile",
                "tofile",
                "memmap",
                "genfromtxt",
            }:
                violations.append(f"Banned I/O or execution method call: '.{func.attr}()'.")

        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                violations.append(f"Banned dunder attribute access: '.{node.attr}'.")
            elif node.attr in {"f_globals", "f_locals", "f_builtins", "gi_frame", "cr_frame", "tb_frame"}:
                violations.append(f"Banned frame introspection attribute: '.{node.attr}'.")

        elif isinstance(node, ast.Name):
            if node.id in BANNED_NAMES:
                violations.append(f"Banned identifier reference: '{node.id}'.")

    if not has_control_def:
        violations.append("Controller must define top-level function 'def control(obs):'.")

    return StaticValidationResult(ok=len(violations) == 0, violations=violations)


def compute_composite_score(
    *,
    passed: bool,
    mean_energy_overhead_pct: float,
    mean_throughput_loss_pct: float,
    mean_battery_wear: float,
    max_shaft_torque_pu: float,
    worst_margin_pct: float | None,
    workloads_passed: int,
    workloads_total: int,
    unevaluated_count: int = 0,
) -> float:
    """Compute scalar objective (lower is better); passing always beats failing."""
    if passed:
        return (
            1.0 * mean_energy_overhead_pct
            + 3.0 * mean_throughput_loss_pct
            + 50.0 * mean_battery_wear
            + 0.5 * max_shaft_torque_pu
        )
    fail_frac = 1.0 - (workloads_passed / max(1, workloads_total))
    shortfall = max(0.0, -(worst_margin_pct if worst_margin_pct is not None else -1000.0))
    return 1_000_000.0 + 100_000.0 * fail_frac + 50_000.0 * unevaluated_count + min(shortfall, 900_000.0)


def aggregate_per_workload_results(
    per_wl: list[dict[str, Any]],
    ruleset: RuleSet,
    elapsed_s: float,
    *,
    strict_review: bool = True,
) -> CandidateEval:
    """Aggregate per-workload checker outputs into suite-level and per-family CandidateEval.

    Guarantees:
    - A trace where all rules are `not_evaluable` is NEVER counted as pass or fail.
    - Suite-level `passed` requires zero failing rules, zero blocking human-review rules,
      at least one evaluable trace, AND every checkable rule evaluated on >= 1 trace.
    """
    wl_evaluable = sum(1 for w in per_wl if w.get("status", "fail") != "not_evaluable")
    wl_not_eval = sum(1 for w in per_wl if w.get("status") == "not_evaluable")
    wl_passed = sum(1 for w in per_wl if w.get("status") == "pass" or (w.get("status") is None and w.get("passed")))

    failing: list[dict[str, Any]] = []
    worst_margin: float | None = None
    worst_cid: str | None = None
    evaluated_cids: set[str] = set()
    all_assumptions: list[str] = list(ruleset.collect_assumptions())

    for w in per_wl:
        failing.extend(w.get("failing_constraints", []))
        wm = w.get("worst_margin_pct")
        if wm is not None and (worst_margin is None or float(wm) < worst_margin):
            worst_margin = float(wm)
            worst_cid = w.get("worst_constraint_id")
        for r in w.get("results", []):
            if r.get("status") in {"pass", "fail", "error"}:
                evaluated_cids.add(str(r.get("constraint_id", "")))
            for a in r.get("assumptions") or []:
                if a not in all_assumptions:
                    all_assumptions.append(a)
        for a in w.get("assumptions") or []:
            if a not in all_assumptions:
                all_assumptions.append(a)

    required_cids = [
        c.id
        for c in ruleset.constraints
        if c.checkable and c.value is not None and not (c.needs_human_review and strict_review)
    ]
    unevaluated_cids = [cid for cid in required_cids if cid not in evaluated_cids]
    has_blocking_review = any(w.get("needs_review_count", 0) > 0 for w in per_wl)

    all_passed = (
        len(per_wl) > 0
        and wl_evaluable > 0
        and wl_passed == wl_evaluable
        and len(failing) == 0
        and len(unevaluated_cids) == 0
        and not (has_blocking_review and strict_review)
    )

    overheads = [float(w["energy_overhead_pct"]) for w in per_wl]
    tp_losses = [float(w["throughput_loss_pct"]) for w in per_wl]
    wears = [float(w["battery_wear_proxy"]) for w in per_wl]
    torques = [float(w["max_shaft_torque_pu"]) for w in per_wl]

    mean_oh = sum(overheads) / max(1, len(overheads))
    mean_tp = sum(tp_losses) / max(1, len(tp_losses))
    mean_wear = sum(wears) / max(1, len(wears))
    max_torque = max(torques) if torques else 0.0

    # Per-family breakdown ("fast", "slow", etc.)
    by_family: dict[str, list[dict[str, Any]]] = {}
    for w in per_wl:
        fam = str(w.get("family", "fast"))
        by_family.setdefault(fam, []).append(w)

    family_summary: dict[str, dict[str, Any]] = {}
    for fam, items in by_family.items():
        f_eval = sum(1 for w in items if w.get("status") != "not_evaluable")
        f_ne = sum(1 for w in items if w.get("status") == "not_evaluable")
        f_pass = sum(1 for w in items if w.get("status") == "pass")
        f_fails = sum(len(w.get("failing_constraints", [])) for w in items)
        f_rev = sum(int(w.get("needs_review_count", 0)) for w in items)
        if f_rev > 0 and strict_review:
            f_status = "refused"
        elif f_eval == 0:
            f_status = "not_evaluable"
        elif f_fails == 0 and f_pass == f_eval:
            f_status = "pass"
        else:
            f_status = "fail"

        f_wm: float | None = None
        f_wcid: str | None = None
        for w in items:
            wm = w.get("worst_margin_pct")
            if wm is not None and (f_wm is None or float(wm) < f_wm):
                f_wm = float(wm)
                f_wcid = w.get("worst_constraint_id")

        f_times_ms = [float(w.get("eval_time_s", 0.0)) * 1000.0 for w in items]
        family_summary[fam] = {
            "status": f_status,
            "passed": f_status == "pass",
            "workloads_total": len(items),
            "workloads_evaluable": f_eval,
            "workloads_passed": f_pass,
            "workloads_not_evaluable": f_ne,
            "mean_energy_overhead_pct": round(sum(float(w["energy_overhead_pct"]) for w in items) / len(items), 3),
            "mean_throughput_loss_pct": round(sum(float(w["throughput_loss_pct"]) for w in items) / len(items), 3),
            "mean_battery_wear": round(sum(float(w["battery_wear_proxy"]) for w in items) / len(items), 6),
            "max_shaft_torque_pu": round(max(float(w["max_shaft_torque_pu"]) for w in items), 5),
            "worst_margin_pct": round(f_wm, 3) if f_wm is not None else None,
            "worst_constraint_id": f_wcid,
            "eval_time_mean_ms": round(sum(f_times_ms) / max(1, len(f_times_ms)), 2),
            "eval_time_max_ms": round(max(f_times_ms), 2) if f_times_ms else 0.0,
        }

    score = compute_composite_score(
        passed=all_passed,
        mean_energy_overhead_pct=mean_oh,
        mean_throughput_loss_pct=mean_tp,
        mean_battery_wear=mean_wear,
        max_shaft_torque_pu=max_torque,
        worst_margin_pct=worst_margin,
        workloads_passed=wl_passed,
        workloads_total=max(1, wl_evaluable),
        unevaluated_count=len(unevaluated_cids),
    )

    err_msg: str | None = None
    if unevaluated_cids and not failing and not (has_blocking_review and strict_review):
        err_msg = f"Not evaluable across suite: constraint(s) {unevaluated_cids} had no trace >= 2x window."

    return CandidateEval(
        passed=all_passed,
        workloads_passed=wl_passed,
        workloads_total=len(per_wl),
        workloads_evaluable=wl_evaluable,
        workloads_not_evaluable=wl_not_eval,
        composite_score=score,
        mean_energy_overhead_pct=mean_oh,
        mean_throughput_loss_pct=mean_tp,
        mean_battery_wear=mean_wear,
        max_shaft_torque_pu=max_torque,
        worst_margin_pct=worst_margin,
        worst_constraint_id=worst_cid,
        failing_constraints=failing,
        per_workload=per_wl,
        unevaluated_constraints=unevaluated_cids,
        assumptions=all_assumptions,
        family_summary=family_summary,
        error=err_msg,
        elapsed_s=elapsed_s,
    )


def _sanitized_env() -> dict[str, str]:
    """Minimal environment for child sandbox subprocess — strips GEMINI_API_KEY and all secrets."""
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONNOUSERSITE": "1",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    }
    if "VIRTUAL_ENV" in os.environ:
        env["VIRTUAL_ENV"] = os.environ["VIRTUAL_ENV"]
    return env


def _serialize_plant_config(cfg: PlantConfig | None) -> dict[str, Any]:
    c = cfg if cfg is not None else PlantConfig()
    b: BatteryConfig = c.battery
    return {
        "coupling_factor": c.coupling_factor,
        "history_window": c.history_window,
        "battery": {
            "max_power_mw": b.max_power_mw,
            "capacity_mwh": b.capacity_mwh,
            "initial_soc": b.initial_soc,
            "soc_min": b.soc_min,
            "soc_max": b.soc_max,
            "eta_charge": b.eta_charge,
            "eta_discharge": b.eta_discharge,
        },
    }


def evaluate_controller_sandboxed(
    code: str,
    ruleset: RuleSet,
    workloads: dict[str, WorkloadParams],
    *,
    plant_config: PlantConfig | None = None,
    strict_review: bool = True,
    timeout_s: float = 10.0,
    max_lines: int = MAX_CODE_LINES,
) -> CandidateEval:
    """Validate and evaluate a candidate controller in an isolated subprocess."""
    t0 = time.perf_counter()
    n_wl = len(workloads)

    static = validate_controller_code(code, max_lines=max_lines)
    if not static.ok:
        return CandidateEval(
            passed=False,
            workloads_passed=0,
            workloads_total=n_wl,
            composite_score=1e9,
            mean_energy_overhead_pct=0.0,
            mean_throughput_loss_pct=0.0,
            mean_battery_wear=0.0,
            max_shaft_torque_pu=0.0,
            worst_margin_pct=None,
            worst_constraint_id=None,
            static_violations=static.violations,
            error="Static safety check failed: " + "; ".join(static.violations),
            elapsed_s=time.perf_counter() - t0,
        )

    payload = {
        "code": code,
        "ruleset": ruleset.to_dict(),
        "strict_review": strict_review,
        "plant_config": _serialize_plant_config(plant_config),
        "cpu_limit_s": max(2, int(timeout_s) + 2),
        "workloads": [{"name": name, "params": params.to_dict()} for name, params in workloads.items()],
    }

    try:
        proc = subprocess.run(
            [sys.executable, str(RUNNER_PATH)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=timeout_s,
            env=_sanitized_env(),
            cwd=str(RUNNER_PATH.parent.parent),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CandidateEval(
            passed=False,
            workloads_passed=0,
            workloads_total=n_wl,
            composite_score=1e9,
            mean_energy_overhead_pct=0.0,
            mean_throughput_loss_pct=0.0,
            mean_battery_wear=0.0,
            max_shaft_torque_pu=0.0,
            worst_margin_pct=None,
            worst_constraint_id=None,
            timed_out=True,
            error=f"Sandboxed evaluation timed out after {timeout_s:.1f}s.",
            elapsed_s=time.perf_counter() - t0,
        )

    elapsed = time.perf_counter() - t0
    if proc.returncode != 0 or not proc.stdout.strip():
        err_msg = (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()
        return CandidateEval(
            passed=False,
            workloads_passed=0,
            workloads_total=n_wl,
            composite_score=1e9,
            mean_energy_overhead_pct=0.0,
            mean_throughput_loss_pct=0.0,
            mean_battery_wear=0.0,
            max_shaft_torque_pu=0.0,
            worst_margin_pct=None,
            worst_constraint_id=None,
            error=f"Sandbox subprocess error: {err_msg[:400]}",
            elapsed_s=elapsed,
        )

    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except json.JSONDecodeError as exc:
        return CandidateEval(
            passed=False,
            workloads_passed=0,
            workloads_total=n_wl,
            composite_score=1e9,
            mean_energy_overhead_pct=0.0,
            mean_throughput_loss_pct=0.0,
            mean_battery_wear=0.0,
            max_shaft_torque_pu=0.0,
            worst_margin_pct=None,
            worst_constraint_id=None,
            error=f"Invalid sandbox JSON output: {exc}",
            elapsed_s=elapsed,
        )

    if not out.get("ok"):
        return CandidateEval(
            passed=False,
            workloads_passed=0,
            workloads_total=n_wl,
            composite_score=1e9,
            mean_energy_overhead_pct=0.0,
            mean_throughput_loss_pct=0.0,
            mean_battery_wear=0.0,
            max_shaft_torque_pu=0.0,
            worst_margin_pct=None,
            worst_constraint_id=None,
            error=str(out.get("error", "Unknown sandbox error.")),
            elapsed_s=elapsed,
        )

    return aggregate_per_workload_results(
        out["per_workload"],
        ruleset=ruleset,
        elapsed_s=elapsed,
        strict_review=strict_review,
    )
