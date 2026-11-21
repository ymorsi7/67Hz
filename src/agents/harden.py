"""Long-running background hardening loop on gridsafe-hardener (gemini-4-carbon)."""

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
from agents.verify import recheck_controller, recheck_counterexamples
from eval.random_suite import sample_random_suite_for_ruleset
from evolve.loop import evolve_controller
from rules.profiles import load_utility_profiles
from workload.synth import all_preset_workloads

REPO_ROOT = find_repo_root()
ATTACKS_DIR = REPO_ROOT / "eval" / "attacks"
TALLY_PATH = ATTACKS_DIR / "hardening_tally.jsonl"
STATE_PATH = ATTACKS_DIR / "hardening_state.json"
PAUSE_FLAG = ATTACKS_DIR / "HARDEN_PAUSE"


def _append_tally(row: dict[str, Any]) -> None:
    ATTACKS_DIR.mkdir(parents=True, exist_ok=True)
    with TALLY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def _save_state(state: dict[str, Any]) -> None:
    ATTACKS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str) + "\n", encoding="utf-8")


def _load_state() -> dict[str, Any] | None:
    if not STATE_PATH.exists():
        return None
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def _wait_if_paused(run_id: str) -> None:
    """Block while eval/attacks/HARDEN_PAUSE exists (touch to pause, rm to resume)."""
    announced = False
    while PAUSE_FLAG.exists():
        if not announced:
            print(f"[harden] PAUSED ({PAUSE_FLAG.name} present). Remove the file to resume.", flush=True)
            _append_tally(
                {
                    "ts_utc": datetime.now(timezone.utc).isoformat(),
                    "event": "hardening_paused",
                    "run_id": run_id,
                    "reason": "HARDEN_PAUSE flag present",
                }
            )
            announced = True
        time.sleep(2.0)
    if announced:
        print("[harden] RESUMED", flush=True)
        _append_tally(
            {
                "ts_utc": datetime.now(timezone.utc).isoformat(),
                "event": "hardening_resumed",
                "run_id": run_id,
            }
        )


def run_hardening_loop(
    *,
    profile_ids: list[str] | None = None,
    max_cycles: int = 50,
    sleep_s: float = 2.0,
    seed: int = 20261121,
    resume: bool = False,
) -> None:
    """Attack -> log verified breaks -> coordinate local fix -> timestamped tally.

    Leaves running until max_cycles or enable_agents=false / KeyboardInterrupt.
    Pause: `touch eval/attacks/HARDEN_PAUSE`. Resume: remove that file, or
    `make harden-resume` to continue from eval/attacks/hardening_state.json after a stop.
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

    cycle = 0
    breaks_verified = 0
    fixes_applied = 0
    prev_state = _load_state() if resume else None
    if resume and prev_state:
        run_id = str(prev_state.get("run_id") or f"harden_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
        cycle = int(prev_state.get("cycle") or 0)
        breaks_verified = int(prev_state.get("breaks_verified") or 0)
        fixes_applied = int(prev_state.get("fixes_applied") or 0)
        targets = list(prev_state.get("targets") or targets)
        print(
            f"[harden] RESUME {run_id} from cycle={cycle} "
            f"breaks={breaks_verified} fixes={fixes_applied} targets={targets}",
            flush=True,
        )
        _append_tally(
            {
                "ts_utc": datetime.now(timezone.utc).isoformat(),
                "event": "hardening_resume_start",
                "run_id": run_id,
                "cycle": cycle,
                "breaks_verified": breaks_verified,
                "fixes_applied": fixes_applied,
                "targets": targets,
            }
        )
    else:
        run_id = f"harden_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        _append_tally(
            {
                "ts_utc": datetime.now(timezone.utc).isoformat(),
                "event": "hardening_start",
                "run_id": run_id,
                "targets": targets,
            }
        )
        print(f"[harden] start {run_id} targets={targets}")

    bridge = AgentBridge(
        f"{run_id}_thread",
        meta={"kind": "hardening_run", "targets": targets, "resumed": bool(resume and prev_state)},
        use_saved_agents=True,
    )
    _save_state(
        {
            "run_id": run_id,
            "cycle": cycle,
            "breaks_verified": breaks_verified,
            "fixes_applied": fixes_applied,
            "targets": targets,
            "seed": seed,
            "max_cycles": max_cycles,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
    )

    while cycle < max_cycles:
        _wait_if_paused(run_id)
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
            cxs = []
            for b in check.get("verified_breaks") or []:
                claimed = b.get("claimed") or {}
                if claimed.get("workload"):
                    cxs.append(claimed)
            if evo2.passed and evo2.best_code:
                # Acceptance gate: CX + presets + validation SEED=9001 (>= current).
                # Rejected fixes keep the current controller and log an open issue.
                gate = bridge.accept_fix(
                    code,
                    evo2.best_code,
                    prof.ruleset,
                    cxs,
                    rated_mw=float(prof.ruleset.rated_mw),
                    profile_id=pid,
                )
                if gate.get("accepted"):
                    fixes_applied += 1
                    row["fix"] = {
                        "outcome": evo2.outcome,
                        "passed": True,
                        "accepted": True,
                        "recheck_breaks_still_fail": False,
                        "gate": {
                            "reason": gate.get("reason"),
                            "message": gate.get("message"),
                            "validation": (gate.get("checks") or {}).get("validation"),
                        },
                    }
                else:
                    row["fix"] = {
                        "outcome": evo2.outcome,
                        "passed": False,
                        "accepted": False,
                        "kept_current_controller": True,
                        "recheck_breaks_still_fail": (gate.get("checks") or {})
                        .get("counterexamples", {})
                        .get("break_found"),
                        "gate": {
                            "reason": gate.get("reason"),
                            "message": gate.get("message"),
                            "worse_on_n": gate.get("worse_on_n"),
                            "validation": (gate.get("checks") or {}).get("validation"),
                        },
                    }
            else:
                row["fix"] = {"outcome": evo2.outcome, "passed": False, "accepted": False}

        row["tally"] = {"cycles": cycle, "breaks_verified": breaks_verified, "fixes_applied": fixes_applied}
        # Auto-pause after timeouts / rate limits so the operator can resume later.
        err = str((reply or {}).get("error") or "")
        if "APITimeoutError" in err or "ReadTimeout" in err or "429" in err or "RESOURCE_EXHAUSTED" in err:
            row["auto_pause"] = True
            PAUSE_FLAG.write_text(
                f"auto-pause after cycle {cycle}: {err[:240]}\nRemove this file (or make harden-resume) to continue.\n",
                encoding="utf-8",
            )
            print(f"[harden] auto-paused after timeout/rate-limit (cycle={cycle})", flush=True)
        _append_tally(row)
        _save_state(
            {
                "run_id": run_id,
                "cycle": cycle,
                "breaks_verified": breaks_verified,
                "fixes_applied": fixes_applied,
                "targets": targets,
                "seed": seed,
                "max_cycles": max_cycles,
                "last_profile_id": pid,
                "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            }
        )
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
    _save_state(
        {
            "run_id": run_id,
            "cycle": cycle,
            "breaks_verified": breaks_verified,
            "fixes_applied": fixes_applied,
            "targets": targets,
            "seed": seed,
            "max_cycles": max_cycles,
            "status": "completed",
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
    )
    print(f"[harden] done {run_id}")
