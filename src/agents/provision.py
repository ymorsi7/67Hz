"""Create or reuse saved managed agents (gridsafe-engineer + gridsafe-hardener)."""

from __future__ import annotations

from typing import Any

from agents.client import get_genai_client, load_managed_config
from agents.sources import base_environment_sources
from agents.tools import engineer_tools, hardener_tools

ENGINEER_SYSTEM = """You are gridsafe-engineer (gemini-4-hydrogen).
Follow .agents/AGENTS.md. Use skills read-rulebook, evolve-controller, certify.
Communicate with gridsafe-hardener ONLY via message_agent. Never claim compliance
without host re-check. Refuse instead of guessing. Never use API keys in the sandbox.
"""

HARDENER_SYSTEM = """You are gridsafe-hardener (gemini-4-carbon).
Follow .agents/AGENTS.md. Use skills red-team and analyze-failure.
When you receive a submit_controller payload, attack it and reply with structured
JSON (break_found + counterexamples, or no_break_within_budget). Keep replies concise.
Never invent a break; only report workloads you actually tested in code_execution.
"""


def _list_agent_ids(client) -> set[str]:
    ids: set[str] = set()
    try:
        page = client.agents.list()
        items = getattr(page, "agents", None)
        if items is None and isinstance(page, dict):
            items = page.get("agents", [])
        if items is None:
            items = []
        for a in items:
            aid = getattr(a, "id", None) or (a.get("id") if isinstance(a, dict) else None)
            if aid:
                ids.add(str(aid))
    except Exception as exc:
        print(f"[provision] agents.list failed: {exc}")
    return ids


def ensure_agents(*, force_recreate: bool = False) -> dict[str, Any]:
    """Idempotently create the two saved managed agents. Returns ids + status."""
    cfg = load_managed_config()
    client = get_genai_client()
    existing = _list_agent_ids(client)
    created: dict[str, str] = {}

    specs = [
        {
            "id": cfg["engineer_id"],
            "model": cfg["engineer_model"],
            "system": ENGINEER_SYSTEM,
            "role": "engineer",
            "tools": engineer_tools(),
        },
        {
            "id": cfg["hardener_id"],
            "model": cfg["hardener_model"],
            "system": HARDENER_SYSTEM,
            "role": "hardener",
            "tools": hardener_tools(),
        },
    ]

    for spec in specs:
        aid = spec["id"]
        if aid in existing and not force_recreate:
            created[aid] = "exists"
            continue
        if aid in existing and force_recreate:
            try:
                client.agents.delete(id=aid)
            except Exception as exc:
                print(f"[provision] delete {aid} failed: {exc}")
        try:
            agent = client.agents.create(
                id=aid,
                base_agent=cfg["base_agent"],
                agent_config={"type": "antigravity", "model": spec["model"]},
                system_instruction=spec["system"],
                tools=spec["tools"],
                base_environment={
                    "type": "remote",
                    "sources": base_environment_sources(repo_url=cfg["repo_url"], role=spec["role"]),
                },
            )
            created[aid] = f"created:{getattr(agent, 'id', aid)}"
            print(f"[provision] created {aid} model={spec['model']}")
        except Exception as exc:
            msg = str(exc)
            if "409" in msg or "already exists" in msg.lower() or type(exc).__name__ == "ConflictError":
                created[aid] = "exists"
                print(f"[provision] {aid} already exists")
            else:
                created[aid] = f"error:{type(exc).__name__}:{exc}"
                print(f"[provision] create {aid} failed: {exc}")

    return {"config": cfg, "agents": created, "existing_before": sorted(existing)}
