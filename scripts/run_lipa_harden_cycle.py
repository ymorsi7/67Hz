#!/usr/bin/env python3
"""LIPA F11 demo: hardener finds a break, engineer fixes, backend confirms. Logged to eval/agent_threads/."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agents.client import get_genai_client
from agents.provision import ensure_agents
from agents.thread_log import AgentThreadLogger
from agents.verify import recheck_controller, recheck_counterexamples, workload_from_dict
from eval.random_suite import sample_random_suite_for_ruleset
from evolve.loop import evolve_controller
from rules.profiles import load_utility_profiles
from workload.synth import all_preset_workloads


def _hardener_package_break(failing_wl: dict, suite_params: dict, constraints: list[dict]) -> dict:
    """Fast hardener turn: package a host-found break as structured JSON (Interactions API)."""
    client = get_genai_client()
    name = failing_wl.get("workload") or failing_wl.get("name")
    wp = suite_params[name].to_dict() if name in suite_params else {}
    prompt = (
        "You are gridsafe-hardener. The host verified a break on the submitted LIPA controller.\n"
        "Do NOT run code_execution. Reply JSON only that reports this break:\n"
        + json.dumps(
            {
                "status": "break_found",
                "counterexamples": [
                    {
                        "workload": wp,
                        "failed_constraint_id": failing_wl.get("worst_constraint_id"),
                        "measured": (failing_wl.get("failing_constraints") or [{}])[0].get("measured"),
                        "limit": (failing_wl.get("failing_constraints") or [{}])[0].get("limit"),
                        "margin_pct": failing_wl.get("worst_margin_pct"),
                        "notes": "F11 Rand20 break packaged by hardener",
                    }
                ],
                "notes": "Report the provided break verbatim as structured JSON.",
            },
            indent=2,
        )
        + f"\nConstraints summary: {json.dumps([{'id': c.get('id'), 'type': c.get('constraint_type'), 'value': c.get('value'), 'band': [c.get('freq_min_hz'), c.get('freq_max_hz')]} for c in constraints])}"
    )
    t0 = time.perf_counter()
    ix = client.interactions.create(
        agent="gridsafe-hardener",
        input=prompt,
        environment="remote",
        timeout=90.0,
    )
    text = (ix.output_text or "").strip()
    try:
        reply = json.loads(text) if text.startswith("{") else {"raw_text": text, "status": "break_found"}
    except Exception:
        reply = {"status": "break_found", "raw_text": text, "counterexamples": [{"workload": wp}]}
    return {
        "ok": True,
        "latency_s": round(time.perf_counter() - t0, 3),
        "interaction_id": ix.id,
        "status": ix.status,
        "reply": reply,
        "raw_text": text[:2000],
    }


def main() -> int:
    ensure_agents()
    profiles = {p.profile_id: p for p in load_utility_profiles(rated_mw=100.0)}
    prof = profiles["lipa"]
    code = (REPO_ROOT / "eval" / "cache" / "lipa_seed303_controller.py").read_text(encoding="utf-8")
    constraints = [c.to_dict() for c in prof.ruleset.constraints]
    suite = sample_random_suite_for_ruleset(prof.ruleset, n=20, seed=20261121)

    tid = f"lipa_f11_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    log = AgentThreadLogger(tid, meta={"kind": "lipa_f11_harden_cycle", "profile": "lipa"})

    print("[lipa-f11] probing Rand20 for a host-verified break...")
    t0 = time.perf_counter()
    host_ev = recheck_controller(code, prof.ruleset, suite, timeout_s=60.0)
    failing = [
        w
        for w in (host_ev.get("eval") or {}).get("per_workload") or []
        if w.get("status") == "fail" or w.get("passed") is False
    ]
    print(f"[lipa-f11] host Rand20 passed={host_ev['passed']} failing={len(failing)} in {time.perf_counter()-t0:.1f}s")
    log.log({"event": "host_rand20_probe", "passed": host_ev["passed"], "failing": failing[:5]})
    if not failing:
        print("[lipa-f11] no failing workload; cannot demo break")
        return 1

    print("[lipa-f11] engineer->hardener (package break)...")
    hard = _hardener_package_break(failing[0], suite, constraints)
    log.log({"event": "hardener_reply", "from": "gridsafe-hardener", "reply": hard})
    print(f"[lipa-f11] hardener latency_s={hard.get('latency_s')} reply={json.dumps(hard.get('reply'), default=str)[:400]}")

    name = failing[0].get("workload") or failing[0].get("name")
    cx = {
        "workload": suite[name].to_dict(),
        "failed_constraint_id": failing[0].get("worst_constraint_id"),
        "margin_pct": failing[0].get("worst_margin_pct"),
        "notes": "F11 host-verified Rand20 break",
    }
    # Backend re-check of claimed counterexample
    cx_check = recheck_counterexamples(code, prof.ruleset, [cx])
    log.log({"event": "backend_counterexample_check", "check": cx_check})
    print(f"[lipa-f11] backend accepted break={cx_check.get('break_found')}")

    print("[lipa-f11] engineer fix via evolve (B3-seeded)...")
    workloads = all_preset_workloads(100.0)
    workloads[str(name)] = suite[name]
    evo = evolve_controller(
        prof.ruleset,
        workloads,
        budget_s=75.0,
        seed=404,
        seed_code=code,
        enable_b3_seed=True,
    )
    log.log(
        {
            "event": "engineer_fix",
            "outcome": evo.outcome,
            "passed": evo.passed,
            "elapsed_s": evo.elapsed_s,
            "overhead_pct": None if not evo.best_eval else evo.best_eval.mean_energy_overhead_pct,
        }
    )
    print(f"[lipa-f11] fix outcome={evo.outcome} passed={evo.passed} elapsed={evo.elapsed_s:.1f}s")

    fixed_code = evo.best_code or code
    confirm_preset = recheck_controller(fixed_code, prof.ruleset, all_preset_workloads(100.0))
    confirm_rand = recheck_controller(fixed_code, prof.ruleset, suite)
    confirm_cx = recheck_counterexamples(fixed_code, prof.ruleset, [cx])
    log.log(
        {
            "event": "backend_confirm",
            "preset_passed": confirm_preset["passed"],
            "rand20_passed": confirm_rand["passed"],
            "counterexamples_still_break": confirm_cx.get("break_found"),
        }
    )
    # Optional: send fixed controller back to hardener for ack
    client = get_genai_client()
    t_ack = time.perf_counter()
    ack = client.interactions.create(
        agent="gridsafe-engineer",
        input=(
            "Call message_agent once to gridsafe-hardener with payload="
            + json.dumps(
                {
                    "type": "fix",
                    "status": "fixed" if confirm_rand["passed"] else "partial",
                    "notes": f"Backend confirm preset={confirm_preset['passed']} rand20={confirm_rand['passed']} cx_still_break={confirm_cx.get('break_found')}",
                }
            )
            + ". After function_result, reply FIXED_CONFIRMED."
        ),
        environment="remote",
        timeout=120.0,
    )
    engineer_ack = {"status": ack.status, "output": ack.output_text, "latency_s": round(time.perf_counter() - t_ack, 3)}
    if str(ack.status) == "requires_action":
        # Host bridges to hardener quickly
        steps = list(getattr(ack, "steps", None) or [])
        results = []
        for s in steps:
            if getattr(s, "type", None) != "function_call":
                continue
            hx = client.interactions.create(
                agent="gridsafe-hardener",
                input='ACK JSON only: {"status":"ack","notes":"fix received"}',
                environment="remote",
                timeout=60.0,
            )
            results.append(
                {
                    "type": "function_result",
                    "name": s.name,
                    "call_id": s.id,
                    "result": {"ok": True, "raw_text": hx.output_text},
                }
            )
            log.log({"event": "hardener_ack_fix", "raw_text": hx.output_text, "latency_s": None})
        if results:
            # tools/system_instruction are interaction-scoped and must match the prior turn.
            ack2 = client.interactions.create(
                agent="gridsafe-engineer",
                input=results,
                previous_interaction_id=ack.id,
                environment=ack.environment_id,
                timeout=60.0,
            )
            engineer_ack["continue_status"] = ack2.status
            engineer_ack["continue_output"] = ack2.output_text
    log.log({"event": "engineer_ack", **engineer_ack})

    summary = {
        "thread_id": tid,
        "hardener_latency_s": hard.get("latency_s"),
        "break_found": True,
        "backend_accepted_break": cx_check.get("break_found"),
        "fix_outcome": evo.outcome,
        "fix_passed": evo.passed,
        "backend_preset_passed": confirm_preset["passed"],
        "backend_rand20_passed": confirm_rand["passed"],
        "backend_cx_still_break": confirm_cx.get("break_found"),
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    out = Path(f"eval/agent_threads/{tid}_summary.json")
    out.write_text(json.dumps(summary, indent=2, default=str) + "\n")
    if evo.best_code:
        Path(f"eval/cache/lipa_f11_fixed_controller.py").write_text(evo.best_code, encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"[lipa-f11] wrote {out}")
    return 0 if evo.passed and confirm_preset["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
