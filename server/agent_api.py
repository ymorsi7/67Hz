"""Managed-agent API for the UI (harden SSE, tally, thread logs).

Mount with: `app.include_router(agent_router)` from `server.app`.
Does not touch frontend/ or server/ui_*.py.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parent.parent
THREADS_DIR = REPO_ROOT / "eval" / "agent_threads"
ATTACKS_DIR = REPO_ROOT / "eval" / "attacks"
TALLY_PATH = ATTACKS_DIR / "hardening_tally.jsonl"

agent_router = APIRouter(prefix="/api/agents", tags=["agents"])


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _plain_summary(payload: dict[str, Any] | None, *, role: str) -> str:
    if not isinstance(payload, dict):
        return f"{role} sent a non-JSON payload"
    typ = str(payload.get("type") or payload.get("status") or "message")
    if typ == "submit_controller":
        return f"{role} submitted a controller for attack ({payload.get('controller_bytes') or len(str(payload.get('controller_code') or ''))} bytes)"
    if typ in {"break_found", "ack"} or payload.get("status") == "break_found":
        n = len(payload.get("counterexamples") or [])
        return f"{role} reported break_found with {n} counterexample(s)"
    if payload.get("status") == "no_break_within_budget" or typ == "no_break_within_budget":
        return f"{role} reported no break within budget"
    if typ == "fix":
        return f"{role} submitted a fix ({payload.get('status') or 'status unknown'})"
    notes = str(payload.get("notes") or "")[:120]
    return f"{role} sent {typ}" + (f": {notes}" if notes else "")


class HardenRequest(BaseModel):
    """POST /api/agents/harden body."""

    controller_code: str = Field(..., min_length=1, description="Candidate controller source")
    constraints: list[dict[str, Any]] = Field(..., min_length=1, description="Verified constraints")
    rated_mw: float = Field(100.0, gt=0, description="Cluster rated MW")
    notes: str = ""
    profile_id: str | None = None


def _load_tally() -> dict[str, Any]:
    """Aggregate hardening_tally.jsonl into UI-facing totals. Never invent counts."""
    if not TALLY_PATH.exists():
        return {
            "running": False,
            "running_since_utc": None,
            "run_id": None,
            "rounds": 0,
            "attacks_tried": 0,
            "breaks_found": 0,
            "fixes_confirmed": 0,
            "last_event_utc": None,
            "targets": [],
            "message": "No hardening tally file yet (eval/attacks/hardening_tally.jsonl).",
        }

    rows: list[dict[str, Any]] = []
    for line in TALLY_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    starts = [r for r in rows if r.get("event") == "hardening_start"]
    stops = [r for r in rows if r.get("event") == "hardening_stop"]
    cycles = [r for r in rows if r.get("event") == "attack_cycle"]
    latest_start = starts[-1] if starts else None
    run_id = (latest_start or {}).get("run_id")
    run_cycles = [r for r in cycles if (not run_id) or r.get("run_id") == run_id]
    breaks = sum(1 for r in run_cycles if r.get("break_found"))
    fixes = 0
    for r in run_cycles:
        fix = r.get("fix") or {}
        if fix.get("passed") and not (fix.get("recheck_breaks_still_fail") is True):
            # Count only when a fix row claims pass; prefer backend-confirmed when present
            if fix.get("recheck_breaks_still_fail") is False or fix.get("passed"):
                fixes += 1 if fix.get("passed") else 0
    # Prefer explicit tally counters from latest cycle when present
    if run_cycles:
        last_t = (run_cycles[-1].get("tally") or {})
        breaks = int(last_t.get("breaks_verified", breaks))
        fixes = int(last_t.get("fixes_applied", fixes))

    stopped_for_run = any(r.get("run_id") == run_id for r in stops) if run_id else False
    running = bool(latest_start) and not stopped_for_run
    return {
        "running": running,
        "running_since_utc": (latest_start or {}).get("ts_utc"),
        "run_id": run_id,
        "rounds": len(run_cycles),
        "attacks_tried": len(run_cycles),
        "breaks_found": breaks,
        "fixes_confirmed": fixes,
        "last_event_utc": (rows[-1].get("ts_utc") if rows else None),
        "targets": (latest_start or {}).get("targets") or [],
        "message": None,
    }


def _list_threads() -> list[dict[str, Any]]:
    THREADS_DIR.mkdir(parents=True, exist_ok=True)
    out: list[dict[str, Any]] = []
    for meta_path in sorted(THREADS_DIR.glob("*.meta.json"), reverse=True):
        thread_id = meta_path.name.replace(".meta.json", "")
        meta: dict[str, Any] = {}
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {"thread_id": thread_id}
        jsonl = THREADS_DIR / f"{thread_id}.jsonl"
        n_events = 0
        if jsonl.exists():
            n_events = sum(1 for line in jsonl.read_text(encoding="utf-8").splitlines() if line.strip())
        out.append(
            {
                "thread_id": thread_id,
                "created_at_utc": meta.get("created_at_utc"),
                "kind": meta.get("kind"),
                "n_events": n_events,
                "meta": meta,
            }
        )
    return out


def _read_thread(thread_id: str) -> dict[str, Any]:
    # Prevent path traversal
    if "/" in thread_id or "\\" in thread_id or ".." in thread_id:
        raise HTTPException(status_code=400, detail="Invalid thread id")
    meta_path = THREADS_DIR / f"{thread_id}.meta.json"
    jsonl_path = THREADS_DIR / f"{thread_id}.jsonl"
    summary_path = THREADS_DIR / f"{thread_id}_summary.json"
    if not jsonl_path.exists() and not meta_path.exists():
        raise HTTPException(status_code=404, detail=f"Unknown thread {thread_id}")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {"thread_id": thread_id}
    events: list[dict[str, Any]] = []
    if jsonl_path.exists():
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                events.append({"raw": line})
    summary = None
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            summary = None
    return {"thread_id": thread_id, "meta": meta, "events": events, "summary": summary}


def _run_harden_round(
    req: HardenRequest,
    event_q: queue.Queue,
    thread_id: str,
) -> None:
    """Background worker: one engineer→hardener round with backend re-checks."""
    try:
        from agents.bridge import AgentBridge
        from agents.client import load_managed_config
        from agents.provision import ensure_agents
        from agents.verify import recheck_counterexamples, ruleset_from_constraints
        from eval.random_suite import sample_random_suite_for_ruleset
        from evolve.loop import evolve_controller
        from workload.synth import all_preset_workloads
    except Exception as exc:
        event_q.put(
            (
                "status",
                {
                    "ok": False,
                    "message": f"Agent modules failed to import: {type(exc).__name__}: {exc}",
                },
            )
        )
        event_q.put(("verdict", {"verdict": "unavailable", "message": "Agent path unavailable."}))
        event_q.put(None)
        return

    cfg = load_managed_config()
    if not cfg.get("enable_agents", True):
        event_q.put(
            (
                "status",
                {
                    "ok": False,
                    "message": "Managed agents are turned off (config/models.yaml managed_agent.enable_agents=false).",
                },
            )
        )
        event_q.put(
            (
                "verdict",
                {
                    "verdict": "agents_disabled",
                    "message": "No agent messages were sent because enable_agents is false.",
                },
            )
        )
        event_q.put(None)
        return

    try:
        ensure_agents()
    except Exception as exc:
        event_q.put(
            (
                "status",
                {
                    "ok": False,
                    "message": f"Could not ensure saved agents: {type(exc).__name__}: {exc}",
                },
            )
        )
        event_q.put(("verdict", {"verdict": "unavailable", "message": "Agent provisioning failed."}))
        event_q.put(None)
        return

    bridge = AgentBridge(
        thread_id,
        meta={
            "kind": "ui_harden_round",
            "profile_id": req.profile_id,
            "rated_mw": req.rated_mw,
        },
        use_saved_agents=True,
    )

    payload = {
        "type": "submit_controller",
        "controller_code": req.controller_code,
        "constraints": req.constraints,
        "rated_mw": req.rated_mw,
        "notes": req.notes or "UI harden round: attack this controller.",
        "controller_bytes": len(req.controller_code),
    }
    event_q.put(
        (
            "agent_message",
            {
                "sender": "gridsafe-engineer",
                "receiver": "gridsafe-hardener",
                "summary": _plain_summary(payload, role="Engineer"),
                "payload": {
                    "type": payload["type"],
                    "notes": payload["notes"],
                    "rated_mw": payload["rated_mw"],
                    "controller_bytes": payload["controller_bytes"],
                    "n_constraints": len(req.constraints),
                    # Full code kept for audit; UI may collapse it
                    "controller_code": req.controller_code,
                    "constraints": req.constraints,
                },
            },
        )
    )

    try:
        hard = bridge.send_to_hardener(payload, timeout_s=90.0)
    except Exception as exc:
        event_q.put(
            (
                "status",
                {
                    "ok": False,
                    "message": f"Hardener call failed: {type(exc).__name__}: {exc}",
                },
            )
        )
        event_q.put(
            (
                "verdict",
                {
                    "verdict": "unavailable",
                    "message": "The hardener did not reply; no fake messages were invented.",
                },
            )
        )
        event_q.put(None)
        return

    reply = hard.get("reply") if isinstance(hard.get("reply"), dict) else {"raw": hard.get("raw_text")}
    event_q.put(
        (
            "agent_message",
            {
                "sender": "gridsafe-hardener",
                "receiver": "gridsafe-engineer",
                "summary": _plain_summary(reply if isinstance(reply, dict) else None, role="Hardener")
                + (f" (latency {hard.get('latency_s')}s)" if hard.get("latency_s") is not None else ""),
                "payload": reply if isinstance(reply, dict) else {"raw_text": hard.get("raw_text")},
                "latency_s": hard.get("latency_s"),
                "ok": hard.get("ok"),
                "error": hard.get("error"),
            },
        )
    )

    if hard.get("error"):
        event_q.put(
            (
                "status",
                {
                    "ok": False,
                    "message": f"Hardener error (real): {hard.get('error')}",
                },
            )
        )

    ruleset = ruleset_from_constraints(req.constraints, source_name="ui_harden", rated_mw=req.rated_mw)
    check = hard.get("backend_counterexample_check")
    cxs = list((reply or {}).get("counterexamples") or []) if isinstance(reply, dict) else []

    # Always host-verify claimed breaks; also probe Rand20 if hardener found nothing
    if not check and cxs:
        check = recheck_counterexamples(req.controller_code, ruleset, cxs, rated_mw=req.rated_mw)
    if not check or not check.get("break_found"):
        suite = sample_random_suite_for_ruleset(ruleset, n=20, seed=int(time.time()) % 1_000_000)
        from agents.verify import recheck_controller

        host_ev = recheck_controller(req.controller_code, ruleset, suite, timeout_s=60.0)
        event_q.put(
            (
                "backend_check",
                {
                    "kind": "host_rand20_probe",
                    "passed": host_ev["passed"],
                    "summary": (
                        "Backend Rand20 probe: controller still passes all 20 workloads."
                        if host_ev["passed"]
                        else "Backend Rand20 probe: found at least one failing workload."
                    ),
                    "detail": {
                        "workloads_passed": (host_ev.get("eval") or {}).get("workloads_passed"),
                        "workloads_evaluable": (host_ev.get("eval") or {}).get("workloads_evaluable"),
                        "worst_margin_pct": (host_ev.get("eval") or {}).get("worst_margin_pct"),
                        "worst_constraint_id": (host_ev.get("eval") or {}).get("worst_constraint_id"),
                    },
                },
            )
        )
        if not host_ev["passed"]:
            # Build CX from first failing workload
            failing = [
                w
                for w in (host_ev.get("eval") or {}).get("per_workload") or []
                if w.get("status") == "fail" or w.get("passed") is False
            ]
            if failing:
                name = failing[0].get("workload") or failing[0].get("name")
                if name in suite:
                    cxs = [
                        {
                            "workload": suite[name].to_dict(),
                            "failed_constraint_id": failing[0].get("worst_constraint_id"),
                            "margin_pct": failing[0].get("worst_margin_pct"),
                            "notes": "host-verified Rand20 break",
                        }
                    ]
                    check = recheck_counterexamples(req.controller_code, ruleset, cxs, rated_mw=req.rated_mw)

    if check:
        event_q.put(
            (
                "backend_check",
                {
                    "kind": "counterexample",
                    "break_found": bool(check.get("break_found")),
                    "summary": (
                        f"Backend accepted {len(check.get('verified_breaks') or [])} claimed break(s)."
                        if check.get("break_found")
                        else "Backend rejected all claimed breaks (controller still passes)."
                    ),
                    "detail": check,
                },
            )
        )

    break_found = bool(check and check.get("break_found"))
    if not break_found:
        event_q.put(
            (
                "verdict",
                {
                    "verdict": "no_break_within_budget",
                    "message": "No backend-verified break was found within this round's budget.",
                    "thread_id": thread_id,
                },
            )
        )
        event_q.put(None)
        return

    # Engineer fix via local B3-seeded evolve (backend), then re-check
    event_q.put(
        (
            "status",
            {"ok": True, "message": "Break confirmed. Engineer fix starting (local B3-seeded evolve on backend)."},
        )
    )
    workloads = all_preset_workloads(cluster_mw=float(req.rated_mw))
    for b in check.get("verified_breaks") or []:
        claimed = b.get("claimed") or {}
        wl = claimed.get("workload")
        if isinstance(wl, dict):
            from agents.verify import workload_from_dict

            name = str(b.get("name") or f"break_{len(workloads)}")
            try:
                workloads[name] = workload_from_dict(name, wl, rated_mw=req.rated_mw)
            except Exception:
                pass

    try:
        evo = evolve_controller(
            ruleset,
            workloads,
            budget_s=75.0,
            seed=404,
            seed_code=req.controller_code,
            enable_b3_seed=True,
        )
    except Exception as exc:
        event_q.put(
            (
                "backend_check",
                {
                    "kind": "fix",
                    "passed": False,
                    "summary": f"Engineer fix failed to run: {type(exc).__name__}: {exc}",
                },
            )
        )
        event_q.put(
            (
                "verdict",
                {
                    "verdict": "break_found_not_fixed",
                    "message": "A backend-verified break was found, but the fix step failed.",
                    "thread_id": thread_id,
                },
            )
        )
        event_q.put(None)
        return

    event_q.put(
        (
            "agent_message",
            {
                "sender": "gridsafe-engineer",
                "receiver": "gridsafe-hardener",
                "summary": f"Engineer produced a fix candidate (outcome={evo.outcome}, passed={evo.passed})",
                "payload": {
                    "type": "fix",
                    "outcome": evo.outcome,
                    "passed": evo.passed,
                    "elapsed_s": evo.elapsed_s,
                    "controller_code": evo.best_code,
                },
            },
        )
    )

    fixed = evo.best_code or req.controller_code
    cx_list = []
    for b in check.get("verified_breaks") or []:
        if b.get("claimed"):
            cx_list.append(b["claimed"])
    if not cx_list and cxs:
        cx_list = cxs
    recheck = recheck_counterexamples(fixed, ruleset, cx_list, rated_mw=req.rated_mw) if cx_list else {"break_found": False}
    still_breaks = bool(recheck.get("break_found"))
    event_q.put(
        (
            "backend_check",
            {
                "kind": "fix",
                "passed": bool(evo.passed) and not still_breaks,
                "summary": (
                    "Backend confirms the fix: prior break workloads now pass."
                    if (evo.passed and not still_breaks)
                    else "Backend could not confirm a fix: break still fails or evolve did not pass."
                ),
                "detail": {
                    "evolve_outcome": evo.outcome,
                    "evolve_passed": evo.passed,
                    "counterexamples_still_break": still_breaks,
                    "recheck": recheck,
                },
            },
        )
    )

    if evo.passed and not still_breaks:
        verdict = "break_found_and_fixed"
        message = "Break found and fixed; backend confirmed the prior counterexamples now pass."
    else:
        verdict = "break_found_not_fixed"
        message = "Break found, but the fix was not confirmed by the backend checker."
    event_q.put(
        (
            "verdict",
            {
                "verdict": verdict,
                "message": message,
                "thread_id": thread_id,
                "fixed_controller_code": fixed if verdict == "break_found_and_fixed" else None,
                "outcome": evo.outcome,
            },
        )
    )
    event_q.put(None)


@agent_router.post("/harden")
async def harden_stream(req: HardenRequest) -> StreamingResponse:
    """One engineer→hardener round; streams agent_message, backend_check, verdict SSE events."""
    thread_id = f"ui_harden_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    event_q: queue.Queue = queue.Queue()

    def worker() -> None:
        _run_harden_round(req, event_q, thread_id)

    threading.Thread(target=worker, daemon=True).start()

    async def gen() -> AsyncIterator[str]:
        yield _sse("status", {"ok": True, "message": "Harden round started.", "thread_id": thread_id})
        while True:
            try:
                item = await asyncio.to_thread(event_q.get, True, 0.5)
            except queue.Empty:
                yield _sse("ping", {"t": time.time()})
                continue
            if item is None:
                break
            event, data = item
            yield _sse(event, data)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@agent_router.get("/tally")
async def get_tally() -> dict[str, Any]:
    return _load_tally()


@agent_router.get("/threads")
async def get_threads() -> dict[str, Any]:
    threads = _list_threads()
    return {"threads": threads, "count": len(threads)}


@agent_router.get("/threads/{thread_id}")
async def get_thread(thread_id: str) -> dict[str, Any]:
    return _read_thread(thread_id)
