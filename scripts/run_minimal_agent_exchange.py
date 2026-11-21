#!/usr/bin/env python3
"""Smallest Phase 4 exchange: engineer -> hardener with one controller. Times latency."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from agents.bridge import AgentBridge
from agents.provision import ensure_agents
from rules.profiles import load_utility_profiles


def _lipa_constraints() -> list[dict]:
    for p in load_utility_profiles(rated_mw=100.0):
        if p.profile_id == "lipa":
            return [c.to_dict() for c in p.ruleset.constraints]
    raise SystemExit("lipa profile not found")


def _lipa_controller() -> str:
    cached = REPO_ROOT / "eval" / "cache" / "lipa_seed303_controller.py"
    if cached.exists():
        return cached.read_text(encoding="utf-8")
    phase3 = json.loads((REPO_ROOT / "eval" / "cache" / "phase3_results.json").read_text())
    for rec in phase3["profiles"]:
        if rec["profile"]["profile_id"] != "lipa":
            continue
        for run in rec["gridsafe"]["runs"]:
            if run.get("outcome") == "evolved_compliant" and run.get("code"):
                return str(run["code"])
    raise SystemExit("no lipa evolved controller in cache")


def main() -> int:
    print(f"[minimal-exchange] start {datetime.now(timezone.utc).isoformat()}")
    t_prov0 = time.perf_counter()
    prov = ensure_agents()
    print(f"[minimal-exchange] provision {json.dumps(prov['agents'])} in {time.perf_counter()-t_prov0:.1f}s")

    constraints = _lipa_constraints()
    code = _lipa_controller()
    print(f"[minimal-exchange] lipa constraints={len(constraints)} controller_bytes={len(code)}")

    thread_id = f"minimal_exchange_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    bridge = AgentBridge(
        thread_id,
        meta={"kind": "minimal_exchange", "profile": "lipa", "n_constraints": len(constraints)},
        use_saved_agents=True,
    )

    payload = {
        "type": "submit_controller",
        "controller_code": code,
        "constraints": constraints,
        "notes": "Minimal exchange: reply JSON {status, counterexamples|notes}. Budget: one short attack.",
    }

    # Step 1: host-direct hardener call (clean latency for engineer->hardener hop)
    print("[minimal-exchange] sending controller to hardener (host-bridged Interactions API)...")
    t0 = time.perf_counter()
    hardener_reply = bridge.send_to_hardener(payload, timeout_s=300.0)
    hop_s = time.perf_counter() - t0
    print(f"[minimal-exchange] HARDENER_HOP_LATENCY_S={hop_s:.3f}")
    print(f"[minimal-exchange] hardener_status={hardener_reply.get('status')}")
    print(f"[minimal-exchange] reply_preview={json.dumps(hardener_reply.get('reply'), default=str)[:900]}")

    # Step 2: engineer path calling message_agent (full bridge)
    print("[minimal-exchange] engineer message_agent path...")
    t1 = time.perf_counter()
    full = bridge.minimal_exchange(
        code,
        constraints,
        notes="Call message_agent once; do not rewrite the controller.",
        timeout_s=300.0,
    )
    full_s = time.perf_counter() - t1
    print(f"[minimal-exchange] ENGINEER_BRIDGE_LATENCY_S={full_s:.3f} mode={full.get('mode')}")
    print(f"[minimal-exchange] thread={bridge.logger.path}")

    summary = {
        "hardener_hop_latency_s": round(hop_s, 3),
        "engineer_bridge_latency_s": round(full_s, 3),
        "hardener_reply": hardener_reply,
        "engineer_bridge": full,
        "thread_id": thread_id,
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    out = REPO_ROOT / "eval" / "agent_threads" / f"{thread_id}_summary.json"
    out.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"[minimal-exchange] TOTAL_REPORTED_HOP_S={hop_s:.3f}")
    print(f"[minimal-exchange] wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
