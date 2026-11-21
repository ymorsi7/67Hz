"""Long-running background hardening loop on gridsafe-hardener (gemini-4-carbon)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any

from agents.bridge import AgentBridge
from agents.client import load_managed_config
from agents.provision import ensure_agents
from agents.verify import recheck_controller, recheck_counterexamples
from eval.random_suite import sample_random_suite_for_ruleset
from evolve.loop import evolve_controller
from rules.profiles import load_utility_profiles
from workload.synth import all_preset_workloads

REPO_ROOT = Path(__file__).resolve().parent.parent
ATTACKS_DIR = REPO_ROOT / "eval" / "attacks"
TALLY_PATH = ATTACKS_DIR / "hardening_tally.jsonl"


def _append_tally(row: dict[str, Any]) -> None:
    ATTACKS_DIR.mkdir(parents=True, exist_ok=True)
    with TALLY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def run_hardening_loop(
    *,
    profile_ids: list[str] | None = None,
    max_cycles: int = 50,
    sleep_s: float = 2.0,
    seed: int = 20261121,
) -> None:
    """Attack -> log verified breaks -> coordinate local fix -> timestamped tally.

    Leaves running until max_cycles or enable_agents=false / KeyboardInterrupt.
    """
    cfg = load_managed_config()
    if not cfg.get("enable_agents", True):
        print("[harden] enable_agents=false; exiting")
        return

    ensure_agents()
    profiles = {p.profile_id: p for p in load_utility_profiles(rated_mw=100.0) if p.split == "dev"}
    targets = profile_ids or ["lipa", "southern_dev"]
    targets = [t for t in targets if t in profiles]
    if not targets:
        raise SystemExit("no hardening targets available")

    run_id = f"harden_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    bridge = AgentBridge(
        f"{run_id}_thread",
        meta={"kind": "hardening_run", "targets": targets},
        use_saved_agents=True,
    )
    print(f"[harden] start {run_id} targets={targets}")
    _append_tally(
        {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "event": "hardening_start",
            "run_id": run_id,
            "targets": targets,
        }
    )

    cycle = 0
    breaks_verified = 0
    fixes_applied = 0
    while cycle < max_cycles:
        cycle += 1
        pid = targets[(cycle - 1) % len(targets)]
        prof = profiles[pid]
        workloads = all_preset_workloads(cluster_mw=float(prof.ruleset.rated_mw))
        # Start from a fresh local evolve (B3-seeded)
        evo = evolve_controller(prof.ruleset, workloads, budget_s=75.0, seed=seed + cycle, enable_b3_seed=True)
        if not evo.passed or not evo.best_code:
            _append_tally(
                {
                    "ts_utc": datetime.now(timezone.utc).isoformat(),
                    "event": "evolve_refused",
                    "run_id": run_id,
                    "cycle": cycle,
                    "profile_id": pid,
                    "outcome": evo.outcome,
                }
            )
            time.sleep(sleep_s)
            continue

        code = evo.best_code
        constraints = [c.to_dict() for c in prof.ruleset.constraints]
        reply = bridge.send_to_hardener(
            {
                "type": "submit_controller",
                "controller_code": code,
                "constraints": constraints,
                "notes": f"Hardening cycle {cycle} profile={pid}. Prefer resonant / idle-start attacks.",
            },
            timeout_s=90.0,
        )
        check = reply.get("backend_counterexample_check")
        # If hardener didn't return structured CXs, also probe random suite on host
        if not check:
            suite = sample_random_suite_for_ruleset(prof.ruleset, n=20, seed=seed + cycle)
            host_ev = recheck_controller(code, prof.ruleset, suite)
            check = {
                "break_found": not host_ev["passed"],
                "verified_breaks": [] if host_ev["passed"] else [{"name": "rand20_host", "backend_eval": host_ev["eval"]}],
                "source": "host_random_suite",
            }

        row = {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "event": "attack_cycle",
            "run_id": run_id,
            "cycle": cycle,
            "profile_id": pid,
            "hardener_latency_s": reply.get("latency_s"),
            "break_found": bool(check.get("break_found")),
            "reply_status": (reply.get("reply") or {}).get("status") if isinstance(reply.get("reply"), dict) else None,
        }
        attack_path = ATTACKS_DIR / f"{run_id}_c{cycle:03d}_{pid}.json"
        attack_path.write_text(
            json.dumps({"reply": reply, "check": check, "controller_code": code}, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        row["attack_path"] = str(attack_path)

        if check.get("break_found"):
            breaks_verified += 1
            # Coordinate fix via local evolve seeded by broken controller
            evo2 = evolve_controller(
                prof.ruleset,
                workloads,
                budget_s=75.0,
                seed=seed + 1000 + cycle,
                seed_code=code,
                enable_b3_seed=True,
            )
            if evo2.passed and evo2.best_code:
                fixes_applied += 1
                # Re-eval on the claimed breaks if present
                cxs = []
                for b in check.get("verified_breaks") or []:
                    claimed = b.get("claimed") or {}
                    if claimed.get("workload"):
                        cxs.append(claimed)
                recheck = None
                if cxs:
                    recheck = recheck_counterexamples(evo2.best_code, prof.ruleset, cxs)
                row["fix"] = {
                    "outcome": evo2.outcome,
                    "passed": evo2.passed,
                    "recheck_breaks_still_fail": (recheck or {}).get("break_found"),
                }
            else:
                row["fix"] = {"outcome": evo2.outcome, "passed": False}

        row["tally"] = {"cycles": cycle, "breaks_verified": breaks_verified, "fixes_applied": fixes_applied}
        _append_tally(row)
        print(
            f"[harden] cycle={cycle} profile={pid} break={row['break_found']} "
            f"breaks_verified={breaks_verified} fixes={fixes_applied}"
        )
        time.sleep(sleep_s)

    _append_tally(
        {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "event": "hardening_stop",
            "run_id": run_id,
            "cycles": cycle,
            "breaks_verified": breaks_verified,
            "fixes_applied": fixes_applied,
        }
    )
    print(f"[harden] done {run_id}")
