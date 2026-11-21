"""Isolated subprocess runner for evaluating candidate controllers safely.

Invoked by evolve.sandbox.evaluate_controller_sandboxed() with a sanitized
environment (no API keys or secrets). Reads a JSON payload from stdin, compiles
the candidate controller in a restricted builtins namespace (math + numpy only),
runs sim.plant.simulate_plant + rules.checker.check_compliance on every workload,
and prints a JSON result to stdout.
"""

from __future__ import annotations

import builtins
import json
import math
import sys
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np

from rules.checker import check_compliance
from rules.schema import RuleSet
from sim.plant import BatteryConfig, PlantConfig, simulate_plant
from sim.shaft import load_shaft_model
from workload.synth import WorkloadParams, generate_workload

_ALLOWED_IMPORTS = {"math", "numpy", "__future__"}


def _safe_import(name: str, globals_: Any = None, locals_: Any = None, fromlist: Any = (), level: int = 0) -> Any:
    top = name.split(".")[0]
    if level != 0 or top not in _ALLOWED_IMPORTS:
        raise ImportError(f"Import of '{name}' is prohibited in GridSafe controller sandbox.")
    return builtins.__import__(name, globals_, locals_, fromlist, level)


def _restricted_builtins() -> dict[str, Any]:
    allowed_names = (
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "divmod",
        "enumerate",
        "filter",
        "float",
        "frozenset",
        "int",
        "isinstance",
        "len",
        "list",
        "map",
        "max",
        "min",
        "pow",
        "range",
        "reversed",
        "round",
        "set",
        "slice",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
        "Exception",
        "ValueError",
        "KeyError",
        "TypeError",
        "IndexError",
        "ZeroDivisionError",
        "RuntimeError",
        "True",
        "False",
        "None",
    )
    safe = {k: getattr(builtins, k) for k in allowed_names if hasattr(builtins, k)}
    safe["__import__"] = _safe_import
    return safe


def _apply_cpu_limit(cpu_seconds: int) -> None:
    try:
        import resource  # POSIX only

        soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
        limit = max(1, int(cpu_seconds))
        if hard == resource.RLIM_INFINITY or limit <= hard:
            resource.setrlimit(resource.RLIMIT_CPU, (limit, hard))
    except Exception:
        pass


def main() -> None:
    raw = sys.stdin.read()
    payload = json.loads(raw)

    cpu_limit = int(payload.get("cpu_limit_s", 10))
    _apply_cpu_limit(cpu_limit)

    code: str = payload["code"]
    ruleset = RuleSet.from_dict(payload["ruleset"])
    strict_review = bool(payload.get("strict_review", True))

    plant_dict = payload.get("plant_config") or {}
    batt_dict = plant_dict.get("battery") or {}
    plant_cfg = PlantConfig(
        battery=BatteryConfig(**batt_dict) if batt_dict else BatteryConfig(),
        coupling_factor=float(plant_dict.get("coupling_factor", 1.0)),
        history_window=int(plant_dict.get("history_window", 250)),
    )

    # Compile and execute candidate controller in a strictly restricted namespace
    ns: dict[str, Any] = {
        "__builtins__": _restricted_builtins(),
        "np": np,
        "numpy": np,
        "math": math,
    }
    try:
        compiled = compile(code, "<candidate_controller>", "exec")
        exec(compiled, ns, ns)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"Compile/load error: {type(exc).__name__}: {exc}"}))
        return

    control_fn = ns.get("control")
    if not callable(control_fn):
        print(json.dumps({"ok": False, "error": "Candidate code does not define callable control(obs)."}))
        return

    shaft = load_shaft_model()
    rules_payload = ruleset.to_dict()
    per_workload: list[dict[str, Any]] = []

    try:
        for wl in payload["workloads"]:
            wl_name = str(wl["name"])
            wl_params = WorkloadParams(**wl["params"])
            trace = generate_workload(wl_params)
            sim_res = simulate_plant(
                trace,
                controller=control_fn,
                plant_config=plant_cfg,
                shaft_model=shaft,
                rules_dict=rules_payload,
            )
            report = check_compliance(sim_res, ruleset, strict_review=strict_review)
            failing = [
                {
                    "workload": wl_name,
                    "constraint_id": r.constraint_id,
                    "constraint_type": r.constraint_type,
                    "status": r.status,
                    "measured": r.measured,
                    "limit": r.limit,
                    "margin": r.margin,
                    "margin_pct": r.margin_pct,
                    "canonical_unit": r.canonical_unit,
                    "page": r.page,
                    "snippet": r.snippet,
                    "details": r.details,
                }
                for r in report.results
                if not r.passed and r.status != "uncheckable"
            ]
            per_workload.append(
                {
                    "workload": wl_name,
                    "passed": report.passed,
                    "checkable_count": report.checkable_count,
                    "passed_count": report.passed_count,
                    "failed_count": report.failed_count,
                    "needs_review_count": report.needs_review_count,
                    "worst_constraint_id": report.worst_constraint_id,
                    "worst_margin_pct": report.worst_margin_pct,
                    "refusal_reason": report.refusal_reason,
                    "failing_constraints": failing,
                    "energy_overhead_pct": sim_res.metrics.energy_overhead_pct,
                    "throughput_loss_pct": sim_res.metrics.throughput_loss_pct,
                    "battery_wear_proxy": sim_res.metrics.battery_wear_proxy,
                    "battery_efc": sim_res.metrics.battery_efc,
                    "max_shaft_torque_pu": sim_res.metrics.max_shaft_torque_pu,
                    "energy_balance_error_mwh": sim_res.metrics.energy_balance_error_mwh,
                    "eval_time_s": sim_res.metrics.eval_time_s,
                    "results": [r.to_dict() for r in report.results],
                }
            )
    except Exception as exc:
        tb_line = traceback.format_exc().splitlines()[-1]
        print(json.dumps({"ok": False, "error": f"Runtime error: {tb_line}"}))
        return

    print(json.dumps({"ok": True, "per_workload": per_workload}))


if __name__ == "__main__":
    main()
