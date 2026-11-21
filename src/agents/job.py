"""One forked managed-agent job run per rulebook (Phase 4)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from repo_paths import find_repo_root
import time
from typing import Any

from agents.bridge import AgentBridge
from agents.client import load_managed_config
from agents.provision import ensure_agents
from agents.verify import recheck_controller
from evolve.loop import evolve_controller
from rules.profiles import UtilityProfile, load_utility_profiles
from workload.synth import all_preset_workloads

REPO_ROOT = find_repo_root()
JOBS_DIR = REPO_ROOT / "eval" / "jobs"


def _workloads_for_profile(profile: UtilityProfile) -> dict:
    return all_preset_workloads(cluster_mw=float(profile.ruleset.rated_mw))


def run_job(
    profile_id: str = "lipa",
    *,
    seed: int = 303,
    budget_s: float = 75.0,
    attack_rounds: int = 2,
    use_agents: bool | None = None,
) -> dict[str, Any]:
    """Run one job: local evolve (B3-seeded) + optional agent hardener rounds + certify."""
    cfg = load_managed_config()
    agents_on = cfg.get("enable_agents", True) if use_agents is None else bool(use_agents)
    profiles = {p.profile_id: p for p in load_utility_profiles(rated_mw=100.0)}
    if profile_id not in profiles:
        raise KeyError(f"unknown profile {profile_id}")
    profile = profiles[profile_id]
    workloads = _workloads_for_profile(profile)

    job_id = f"job_{profile_id}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()

    # Local Designer path (guaranteed within 75s) — B3 seed + ladder already in evolve_controller
    evo = evolve_controller(
        profile.ruleset,
        workloads,
        budget_s=budget_s,
        seed=seed,
        enable_b3_seed=True,
    )
    certificate: dict[str, Any] = {
        "job_id": job_id,
        "profile_id": profile_id,
        "utility": profile.utility,
        "outcome": evo.outcome,
        "passed": evo.passed,
        "model_jobs": {
            "reader": "gemini-4-neon (backend)",
            "verifier": "gemini-decision-1 (backend)",
            "designer": "gemini-4-hydrogen (local evolve)",
            "hardener": "gemini-4-carbon (managed)" if agents_on else "skipped",
        },
        "assumptions": list(getattr(profile, "assumptions", []) or []),
        "evolve_elapsed_s": round(evo.elapsed_s, 3),
        "attacks": [],
        "plan_checklist": {
            "read": True,
            "verify": True,
            "evolve": bool(evo.passed),
            "attack": False,
            "certify": False,
        },
    }

    thread_id = f"{job_id}_thread"
    bridge = None
    if agents_on and evo.passed and evo.best_code:
        try:
            ensure_agents()
            bridge = AgentBridge(
                thread_id,
                meta={"kind": "job_run", "profile_id": profile_id, "job_id": job_id},
                use_saved_agents=True,
            )
            constraints = [c.to_dict() for c in profile.ruleset.constraints]
            code = evo.best_code
            clean_rounds = 0
            for r in range(1, attack_rounds + 1):
                reply = bridge.send_to_hardener(
                    {
                        "type": "submit_controller",
                        "controller_code": code,
                        "constraints": constraints,
                        "notes": f"Job {job_id} attack round {r}/{attack_rounds}",
                    },
                    timeout_s=240.0,
                )
                attack_row = {
                    "round": r,
                    "hardener_latency_s": reply.get("latency_s"),
                    "reply": reply.get("reply"),
                    "backend_counterexample_check": reply.get("backend_counterexample_check"),
                }
                certificate["attacks"].append(attack_row)
                check = reply.get("backend_counterexample_check") or {}
                if check.get("break_found"):
                    clean_rounds = 0
                    # Local fix path: re-evolve with feedback (host-side, no sandbox keys)
                    fail_hint = json.dumps(check.get("verified_breaks", [])[:1], default=str)[:1500]
                    evo2 = evolve_controller(
                        profile.ruleset,
                        workloads,
                        budget_s=min(60.0, budget_s),
                        seed=seed + r,
                        seed_code=code,
                        enable_b3_seed=True,
                    )
                    if evo2.passed and evo2.best_code:
                        code = evo2.best_code
                        evo = evo2
                        certificate["outcome"] = evo2.outcome
                        attack_row["fix_applied"] = True
                        attack_row["fix_outcome"] = evo2.outcome
                    else:
                        attack_row["fix_applied"] = False
                        attack_row["fix_note"] = fail_hint[:400]
                else:
                    clean_rounds += 1
                    if clean_rounds >= 2:
                        break
            certificate["plan_checklist"]["attack"] = True
            # Final backend re-check
            final = recheck_controller(code, profile.ruleset, workloads)
            certificate["backend_final_check"] = {
                "passed": final["passed"],
                "worst_margin_pct": (final.get("eval") or {}).get("worst_margin_pct"),
            }
            certificate["passed"] = bool(final["passed"])
            certificate["controller_code"] = code
            certificate["plan_checklist"]["certify"] = bool(final["passed"])
            certificate["agent_thread"] = str(bridge.logger.path)
        except Exception as exc:
            certificate["agent_error"] = f"{type(exc).__name__}: {exc}"
            certificate["controller_code"] = evo.best_code
            certificate["agents_fallback"] = "local_evolve_only"
    else:
        certificate["controller_code"] = evo.best_code
        certificate["agents_skipped"] = not agents_on or not evo.passed
        if evo.passed and evo.best_code:
            final = recheck_controller(evo.best_code, profile.ruleset, workloads)
            certificate["backend_final_check"] = {"passed": final["passed"]}
            certificate["plan_checklist"]["certify"] = bool(final["passed"])

    certificate["elapsed_s"] = round(time.perf_counter() - t0, 3)
    certificate["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    out = JOBS_DIR / f"{job_id}.json"
    out.write_text(json.dumps(certificate, indent=2, default=str) + "\n", encoding="utf-8")
    certificate["certificate_path"] = str(out)
    return certificate
