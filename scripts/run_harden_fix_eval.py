#!/usr/bin/env python3
"""Engineer↔hardener break-and-fix cycle on lipa + lipa_pseg under the acceptance gate.

Scores before/after on a never-used holdout seed (default SEED=31337).
The gate itself uses SEED=9001 only for acceptance (never as the holdout score).
Writes eval/results/harden_fix_<holdout>.json for report.py.

Prior ungated run (SEED=4242) is left intact at eval/results/harden_fix_4242.json.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from agents.bridge import AgentBridge
from agents.provision import ensure_agents
from agents.verify import FIX_VALIDATION_SEED, recheck_controller
from eval.random_suite import sample_random_suite_for_ruleset
from evolve.loop import evolve_controller
from evolve.sandbox import evaluate_controller_sandboxed
from rules.profiles import load_utility_profiles
from workload.synth import all_preset_workloads

PHASE3 = REPO_ROOT / "eval" / "cache" / "phase3_results.json"
SEEN_SEED = 20261121
HOLD_SEED = 31337
N = 20
OUT = REPO_ROOT / "eval" / "results" / f"harden_fix_{HOLD_SEED}.json"


def _best_code(rec: dict) -> str:
    return (rec.get("gridsafe") or {}).get("summary", {}).get("best_code") or ""


def _score(code: str, ruleset, seed: int) -> dict[str, Any]:
    suite = sample_random_suite_for_ruleset(ruleset, n=N, seed=seed, cluster_mw=100.0)
    ev = evaluate_controller_sandboxed(code, ruleset, suite, timeout_s=40.0)
    return {
        "seed": seed,
        "passed": ev.passed,
        "workloads_passed": ev.workloads_passed,
        "workloads_evaluable": ev.workloads_evaluable,
        "worst_margin_pct": ev.worst_margin_pct,
        "worst_constraint_id": ev.worst_constraint_id,
        "mean_energy_overhead_pct": round(ev.mean_energy_overhead_pct, 3),
        "mean_throughput_loss_pct": round(ev.mean_throughput_loss_pct, 3),
        "failing_constraints": (ev.failing_constraints or [])[:3],
    }


def _one_cycle(profile_id: str, code: str, prof) -> dict[str, Any]:
    """Host finds Rand20 break on SEEN_SEED if any; evolve a fix; accept only via gate."""
    ensure_agents()
    workloads = all_preset_workloads(float(prof.ruleset.rated_mw))
    constraints = [c.to_dict() for c in prof.ruleset.constraints]
    suite = sample_random_suite_for_ruleset(prof.ruleset, n=N, seed=SEEN_SEED, cluster_mw=100.0)

    before_seen = _score(code, prof.ruleset, SEEN_SEED)
    before_hold = _score(code, prof.ruleset, HOLD_SEED)

    host = recheck_controller(code, prof.ruleset, suite, timeout_s=60.0)
    failing = [
        w
        for w in (host.get("eval") or {}).get("per_workload") or []
        if w.get("status") == "fail" or w.get("passed") is False
    ]

    tid = f"harden_fix_gated_{profile_id}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    bridge = AgentBridge(
        tid,
        meta={"kind": "harden_fix_eval_gated", "profile_id": profile_id, "holdout_seed": HOLD_SEED},
        use_saved_agents=True,
    )

    hardener_reply: dict[str, Any]
    t_h0 = time.perf_counter()
    if failing:
        name = failing[0].get("workload") or failing[0].get("name")
        payload = {
            "type": "submit_controller",
            "controller_code": code[:2500],
            "constraints": constraints,
            "notes": (
                f"Host verified a Rand20 break on {profile_id} seed={SEEN_SEED} workload={name}. "
                "Reply JSON with status=break_found and one short counterexample workload dict; "
                "do not run long simulations. Return best CX within 45 seconds."
            ),
        }
        hardener_reply = bridge.send_to_hardener(payload, timeout_s=90.0)
        hardener_latency = round(time.perf_counter() - t_h0, 3)
        break_found = True
        cx_note = f"host_rand20:{name}"
        cx_list = [
            {
                "workload": suite[name].to_dict() if name in suite else {},
                "failed_constraint_id": failing[0].get("worst_constraint_id"),
                "margin_pct": failing[0].get("worst_margin_pct"),
                "notes": cx_note,
            }
        ]
    else:
        payload = {
            "type": "submit_controller",
            "controller_code": code[:2500],
            "constraints": constraints,
            "notes": f"Attack {profile_id}. Prefer resonant / idle-start / band attacks. Short JSON reply within 45s.",
        }
        hardener_reply = bridge.send_to_hardener(payload, timeout_s=90.0)
        hardener_latency = round(time.perf_counter() - t_h0, 3)
        check = hardener_reply.get("backend_counterexample_check") or {}
        break_found = bool(check.get("break_found")) or (not before_seen["passed"])
        cx_list = []
        for b in check.get("verified_breaks") or []:
            if b.get("claimed"):
                cx_list.append(b["claimed"])
        cx_note = "hardener_or_none"

    # Engineer fix via local evolve (B3-seeded), seeded by broken controller — backend is ground truth
    t_e0 = time.perf_counter()
    evo = evolve_controller(
        prof.ruleset,
        workloads,
        budget_s=75.0,
        seed=HOLD_SEED + hash(profile_id) % 1000,
        seed_code=code,
        enable_b3_seed=True,
    )
    engineer_latency = round(time.perf_counter() - t_e0, 3)
    candidate = evo.best_code if (evo.passed and evo.best_code) else None

    gate: dict[str, Any] | None = None
    if candidate and (break_found or cx_list):
        gate = bridge.accept_fix(
            code,
            candidate,
            prof.ruleset,
            cx_list,
            rated_mw=float(prof.ruleset.rated_mw),
            profile_id=profile_id,
        )
    elif candidate and not break_found:
        # No break to fix — keep current; still record candidate scores for audit if needed
        gate = {
            "accepted": False,
            "reason": "no_break_to_fix",
            "message": "no backend-verified break; current controller kept",
            "controller_code": code,
            "kept_code": "current",
        }

    accepted = bool(gate and gate.get("accepted"))
    fixed = candidate if accepted else code
    fix_confirmed = accepted

    after_seen = _score(fixed, prof.ruleset, SEEN_SEED)
    after_hold = _score(fixed, prof.ruleset, HOLD_SEED)

    # Also score the ungated candidate on holdout when rejected (evidence of what the gate blocked)
    rejected_candidate_hold = None
    if candidate and not accepted and candidate != code:
        rejected_candidate_hold = _score(candidate, prof.ruleset, HOLD_SEED)

    return {
        "profile_id": profile_id,
        "thread_id": tid,
        "gated": True,
        "gate_validation_seed": FIX_VALIDATION_SEED,
        "before": {f"seed_{SEEN_SEED}": before_seen, f"seed_{HOLD_SEED}": before_hold},
        "after": {f"seed_{SEEN_SEED}": after_seen, f"seed_{HOLD_SEED}": after_hold},
        "rejected_candidate_holdout": rejected_candidate_hold,
        "break_found": break_found,
        "failing_on_seen_before": len(failing),
        "hardener_latency_s": hardener_latency,
        "hardener_error": (hardener_reply or {}).get("error"),
        "hardener_reply_status": ((hardener_reply or {}).get("reply") or {}).get("status")
        if isinstance((hardener_reply or {}).get("reply"), dict)
        else None,
        "engineer_latency_s": engineer_latency,
        "evolve_outcome": evo.outcome,
        "evolve_passed": evo.passed,
        "fix_confirmed_by_backend": fix_confirmed,
        "gate": {
            "accepted": accepted,
            "reason": (gate or {}).get("reason"),
            "message": (gate or {}).get("message"),
            "worse_on_n": (gate or {}).get("worse_on_n"),
            "checks": {
                "presets": ((gate or {}).get("checks") or {}).get("presets"),
                "validation": ((gate or {}).get("checks") or {}).get("validation"),
            },
        },
        "fixed_code_changed": fixed != code,
        "cx_note": cx_note,
    }


def main() -> int:
    p3 = json.loads(PHASE3.read_text(encoding="utf-8"))
    profiles = {p.profile_id: p for p in load_utility_profiles(rated_mw=100.0)}
    rows = []
    for pid in ("lipa", "lipa_pseg"):
        rec = next(r for r in p3["profiles"] if r["profile"]["profile_id"] == pid)
        code = _best_code(rec)
        if not code:
            print(f"[harden-fix] no GridSafe code for {pid}", flush=True)
            continue
        print(f"[harden-fix] === {pid} (gated, holdout SEED={HOLD_SEED}) ===", flush=True)
        row = _one_cycle(pid, code, profiles[pid])
        rows.append(row)
        b = row["before"][f"seed_{HOLD_SEED}"]
        a = row["after"][f"seed_{HOLD_SEED}"]
        print(
            f"[harden-fix] {pid} SEED={HOLD_SEED} before={b['workloads_passed']}/{b['workloads_evaluable']} "
            f"after={a['workloads_passed']}/{a['workloads_evaluable']} "
            f"break={row['break_found']} gate_accepted={row['gate']['accepted']} "
            f"reason={row['gate']['reason']}",
            flush=True,
        )
        if row.get("rejected_candidate_holdout"):
            rc = row["rejected_candidate_holdout"]
            print(
                f"[harden-fix] {pid} rejected-candidate SEED={HOLD_SEED} "
                f"would_have_been={rc['workloads_passed']}/{rc['workloads_evaluable']}",
                flush=True,
            )

    payload = {
        "schema_version": "harden_fix_eval_v2_gated",
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "seen_seed": SEEN_SEED,
        "holdout_seed": HOLD_SEED,
        "gate_validation_seed": FIX_VALIDATION_SEED,
        "gated": True,
        "n": N,
        "profiles": rows,
        "prior_ungated_file": "eval/results/harden_fix_4242.json",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"[harden-fix] wrote {OUT.relative_to(REPO_ROOT)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
