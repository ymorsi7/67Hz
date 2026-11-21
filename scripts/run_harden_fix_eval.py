#!/usr/bin/env python3
"""One engineer↔hardener break-and-fix cycle on lipa + lipa_pseg; score SEED=4242 before/after.

Also records SEED=20261121 before scores (the suite the original controllers were exposed to).
Writes eval/results/harden_fix_4242.json for report.py.
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
from agents.verify import recheck_controller
from eval.random_suite import sample_random_suite_for_ruleset
from evolve.loop import evolve_controller
from evolve.sandbox import evaluate_controller_sandboxed
from rules.profiles import load_utility_profiles
from workload.synth import all_preset_workloads

PHASE3 = REPO_ROOT / "eval" / "cache" / "phase3_results.json"
OUT = REPO_ROOT / "eval" / "results" / "harden_fix_4242.json"
SEEN_SEED = 20261121
HOLD_SEED = 4242
N = 20


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
    """Host finds Rand20 break on SEEN_SEED if any; hardener packages; engineer evolves fix; backend rechecks."""
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

    tid = f"harden_fix_{profile_id}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    bridge = AgentBridge(tid, meta={"kind": "harden_fix_eval", "profile_id": profile_id}, use_saved_agents=True)

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
                "do not run long simulations."
            ),
        }
        hardener_reply = bridge.send_to_hardener(payload, timeout_s=90.0)
        hardener_latency = round(time.perf_counter() - t_h0, 3)
        # Prefer host-verified failing workload as the CX
        cx_wl = suite[name].to_dict() if name in suite else {}
        break_found = True
        cx_note = f"host_rand20:{name}"
    else:
        payload = {
            "type": "submit_controller",
            "controller_code": code[:2500],
            "constraints": constraints,
            "notes": f"Attack {profile_id}. Prefer resonant / idle-start / band attacks. Short JSON reply.",
        }
        hardener_reply = bridge.send_to_hardener(payload, timeout_s=90.0)
        hardener_latency = round(time.perf_counter() - t_h0, 3)
        check = hardener_reply.get("backend_counterexample_check") or {}
        break_found = bool(check.get("break_found")) or (not before_seen["passed"])
        cx_wl = None
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
    fixed = evo.best_code if (evo.passed and evo.best_code) else code
    fix_confirmed = bool(evo.passed and evo.best_code and evo.best_code != code)

    after_seen = _score(fixed, prof.ruleset, SEEN_SEED)
    after_hold = _score(fixed, prof.ruleset, HOLD_SEED)

    # Backend confirm: fixed controller should not fail the original failing workload if we had one
    backend_fix_ok = None
    if failing and fixed:
        name = failing[0].get("workload") or failing[0].get("name")
        one = {name: suite[name]} if name in suite else {}
        if one:
            ev1 = evaluate_controller_sandboxed(fixed, prof.ruleset, one, timeout_s=20.0)
            backend_fix_ok = bool(ev1.passed)

    return {
        "profile_id": profile_id,
        "thread_id": tid,
        "before": {"seed_20261121": before_seen, "seed_4242": before_hold},
        "after": {"seed_20261121": after_seen, "seed_4242": after_hold},
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
        "fix_confirmed_by_backend": backend_fix_ok if backend_fix_ok is not None else fix_confirmed,
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
        print(f"[harden-fix] === {pid} ===", flush=True)
        row = _one_cycle(pid, code, profiles[pid])
        rows.append(row)
        b, a = row["before"]["seed_4242"], row["after"]["seed_4242"]
        print(
            f"[harden-fix] {pid} SEED=4242 before={b['workloads_passed']}/{b['workloads_evaluable']} "
            f"after={a['workloads_passed']}/{a['workloads_evaluable']} "
            f"break={row['break_found']} fix_confirmed={row['fix_confirmed_by_backend']}",
            flush=True,
        )

    payload = {
        "schema_version": "harden_fix_eval_v1",
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "seen_seed": SEEN_SEED,
        "holdout_seed": HOLD_SEED,
        "n": N,
        "profiles": rows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"[harden-fix] wrote {OUT.relative_to(REPO_ROOT)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
