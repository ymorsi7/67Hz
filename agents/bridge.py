"""message_agent bridge: engineer <-> hardener via Interactions API.

Official Managed Agents docs: no subagent nesting. Host handles function_call
`message_agent`, creates/continues an interaction with the target agent, logs
structured JSON to eval/agent_threads/, re-checks controllers/counterexamples,
and returns the reply as function_result. No API keys enter sandboxes.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import time
from typing import Any, Callable

from agents.client import get_genai_client, load_managed_config
from agents.thread_log import AgentThreadLogger
from agents.tools import engineer_tools, hardener_tools
from agents.verify import recheck_controller, recheck_counterexamples, ruleset_from_constraints

EventCallback = Callable[[dict[str, Any]], None]


def _step_type(step: Any) -> str:
    return str(getattr(step, "type", None) or getattr(step, "step_type", None) or "")


def _pending_function_calls(interaction: Any) -> list[Any]:
    steps = list(getattr(interaction, "steps", None) or [])
    answered: set[str] = set()
    pending: list[Any] = []
    for step in steps:
        st = _step_type(step)
        if st in {"function_result", "function_response"}:
            cid = getattr(step, "call_id", None) or getattr(step, "id", None)
            if cid:
                answered.add(str(cid))
        elif st == "function_call":
            pending.append(step)
    out = []
    for fc in pending:
        cid = str(getattr(fc, "id", None) or getattr(fc, "call_id", None) or "")
        if cid and cid in answered:
            continue
        out.append(fc)
    return out


def _fc_name(fc: Any) -> str:
    return str(getattr(fc, "name", None) or getattr(fc, "function_name", None) or "")


def _fc_args(fc: Any) -> dict[str, Any]:
    args = getattr(fc, "args", None) or getattr(fc, "arguments", None) or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {"raw": args}
    return dict(args) if isinstance(args, dict) else {"value": args}


def _fc_id(fc: Any) -> str:
    return str(getattr(fc, "id", None) or getattr(fc, "call_id", None) or "")


class AgentBridge:
    """Stateful host bridge between gridsafe-engineer and gridsafe-hardener."""

    def __init__(
        self,
        thread_id: str,
        *,
        meta: dict[str, Any] | None = None,
        on_event: EventCallback | None = None,
        use_saved_agents: bool = True,
    ) -> None:
        self.cfg = load_managed_config()
        self.client = get_genai_client()
        self.logger = AgentThreadLogger(thread_id, meta=meta)
        self.on_event = on_event
        self.use_saved_agents = use_saved_agents and bool(self.cfg.get("enable_agents", True))
        self.engineer_prev_id: str | None = None
        self.engineer_env: str | None = None
        self.hardener_prev_id: str | None = None
        self.hardener_env: str | None = None
        self.round = 0

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        payload = {"event": event, **data}
        self.logger.log(payload)
        if self.on_event:
            self.on_event(payload)

    def _agent_kwargs(self, role: str) -> dict[str, Any]:
        if self.use_saved_agents:
            aid = self.cfg["engineer_id"] if role == "engineer" else self.cfg["hardener_id"]
            return {"agent": aid}
        model = self.cfg["engineer_model"] if role == "engineer" else self.cfg["hardener_model"]
        tools = engineer_tools() if role == "engineer" else hardener_tools()
        return {
            "agent": self.cfg["base_agent"],
            "agent_config": {"type": "antigravity", "model": model},
            "tools": tools,
            "system_instruction": (
                "You are gridsafe-engineer. Use message_agent to talk to the hardener."
                if role == "engineer"
                else "You are gridsafe-hardener. Attack controllers; reply with structured JSON only."
            ),
        }

    def _create(
        self,
        role: str,
        input_payload: Any,
        *,
        previous_id: str | None,
        environment: str | None,
        tools: list[dict[str, Any]] | None = None,
        timeout_s: float = 300.0,
    ) -> Any:
        kwargs = self._agent_kwargs(role)
        if tools is not None:
            kwargs["tools"] = tools
        if previous_id:
            kwargs["previous_interaction_id"] = previous_id
        if environment:
            kwargs["environment"] = environment
        else:
            kwargs.setdefault("environment", "remote")
        kwargs["input"] = input_payload
        return self.client.interactions.create(timeout=timeout_s, **kwargs)

    def send_to_hardener(self, payload: dict[str, Any], *, timeout_s: float = 240.0) -> dict[str, Any]:
        """Direct host-orchestrated message to hardener (also used by message_agent handler)."""
        self.round += 1
        t0 = time.perf_counter()
        # Strip oversized code from the on-wire prompt; keep a short head for the hardener.
        wire_payload = dict(payload)
        code = str(wire_payload.get("controller_code") or "")
        if len(code) > 1200:
            wire_payload["controller_code"] = code[:1200] + "\n# ... truncated for hardener prompt ..."
            wire_payload["controller_bytes"] = len(code)
        prompt = (
            "You received a structured message from gridsafe-engineer via the host bridge.\n"
            "If type=submit_controller: propose up to 3 adversarial workload parameter dicts that "
            "are likely to break the controller against the given constraints. "
            "Prefer resonant periods (0.02-0.07 s for 15-50 Hz) and abrupt idle->full starts.\n"
            "Do NOT run long multi-minute simulations in code_execution. "
            "At most one short numpy snippet (<2s). Reply JSON only:\n"
            '{"status":"break_found"|"no_break_within_budget","counterexamples":[{"workload":{...},'
            '"failed_constraint_id":"...","notes":"..."}],"notes":"..."}\n\n'
            f"{json.dumps(wire_payload, indent=2)[:8000]}"
        )
        self._emit(
            "message_out",
            {"from": "gridsafe-engineer", "to": "gridsafe-hardener", "round": self.round, "payload_keys": list(payload.keys())},
        )
        try:
            interaction = self._create(
                "hardener",
                prompt,
                previous_id=self.hardener_prev_id,
                environment=self.hardener_env,
                tools=hardener_tools()[:2],
                timeout_s=timeout_s,
            )
            interaction = self._drain_host_tools(interaction, role="hardener", timeout_s=min(60.0, timeout_s))
            self.hardener_prev_id = interaction.id
            self.hardener_env = getattr(interaction, "environment_id", None) or self.hardener_env
            text = (interaction.output_text or "").strip()
            reply = self._parse_json_loose(text)
            latency_s = time.perf_counter() - t0
            result = {
                "ok": True,
                "from": "gridsafe-hardener",
                "to": "gridsafe-engineer",
                "round": self.round,
                "latency_s": round(latency_s, 3),
                "interaction_id": interaction.id,
                "status": getattr(interaction, "status", None),
                "raw_text": text[:4000],
                "reply": reply,
            }
        except Exception as exc:
            latency_s = time.perf_counter() - t0
            reply = {
                "status": "no_break_within_budget",
                "notes": "hardener timed out; host will verify independently",
                "counterexamples": [],
            }
            result = {
                "ok": False,
                "from": "gridsafe-hardener",
                "to": "gridsafe-engineer",
                "round": self.round,
                "latency_s": round(latency_s, 3),
                "error": f"{type(exc).__name__}: {exc}",
                "reply": reply,
            }
        # Backend re-check if a controller + counterexamples are present
        if payload.get("controller_code") and payload.get("constraints") and isinstance(reply, dict):
            ruleset = ruleset_from_constraints(payload["constraints"], source_name="bridge")
            if reply.get("counterexamples"):
                check = recheck_counterexamples(
                    payload["controller_code"],
                    ruleset,
                    list(reply.get("counterexamples") or []),
                )
                result["backend_counterexample_check"] = check
                reply["backend_verified_break"] = check.get("break_found", False)
                result["reply"] = reply
        self._emit("message_in", result)
        return result

    def minimal_exchange(
        self,
        controller_code: str,
        constraints: list[dict[str, Any]],
        *,
        notes: str = "",
        timeout_s: float = 240.0,
    ) -> dict[str, Any]:
        """Smallest working exchange: engineer submits one controller; hardener replies."""
        t0 = time.perf_counter()
        payload = {
            "type": "submit_controller",
            "controller_code": controller_code,
            "constraints": constraints,
            "notes": notes or "Minimal Phase 4 exchange: attack this controller.",
        }
        # Path A: ask engineer to call message_agent (true bridge). Fallback: host-direct.
        engineer_prompt = (
            "Call the custom function message_agent exactly once with:\n"
            '  to="gridsafe-hardener"\n'
            "  payload=<the JSON object provided below>\n"
            "Do not modify the controller. After you receive the function_result, "
            "summarize the hardener reply in one short paragraph.\n\n"
            f"PAYLOAD_JSON:\n{json.dumps(payload)}"
        )
        mode = "engineer_message_agent"
        try:
            interaction = self._create(
                "engineer",
                engineer_prompt,
                previous_id=self.engineer_prev_id,
                environment=self.engineer_env,
                tools=engineer_tools(),
                timeout_s=timeout_s,
            )
            interaction, handled = self._handle_until_complete(
                interaction, role="engineer", timeout_s=timeout_s, fallback_payload=payload
            )
            self.engineer_prev_id = interaction.id
            self.engineer_env = getattr(interaction, "environment_id", None) or self.engineer_env
            hardener_reply = handled.get("hardener_reply")
            if hardener_reply is None:
                mode = "host_direct_fallback"
                hardener_reply = self.send_to_hardener(payload, timeout_s=timeout_s)
        except Exception as exc:
            mode = "host_direct_fallback"
            self._emit("bridge_error", {"error": f"{type(exc).__name__}: {exc}", "fallback": True})
            hardener_reply = self.send_to_hardener(payload, timeout_s=timeout_s)

        total_s = time.perf_counter() - t0
        out = {
            "mode": mode,
            "total_latency_s": round(total_s, 3),
            "hardener_reply": hardener_reply,
            "thread_id": self.logger.thread_id,
            "thread_path": str(self.logger.path),
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        self.logger.update_meta(last_result=out)
        return out

    def _handle_until_complete(
        self,
        interaction: Any,
        *,
        role: str,
        timeout_s: float,
        fallback_payload: dict[str, Any] | None = None,
        max_turns: int = 8,
    ) -> tuple[Any, dict[str, Any]]:
        handled: dict[str, Any] = {}
        deadline = time.perf_counter() + timeout_s
        for _ in range(max_turns):
            status = str(getattr(interaction, "status", "") or "")
            if status == "completed":
                return interaction, handled
            if status != "requires_action":
                # background / in_progress — poll briefly
                if time.perf_counter() > deadline:
                    break
                time.sleep(1.0)
                interaction = self.client.interactions.get(id=interaction.id)
                continue
            pending = _pending_function_calls(interaction)
            if not pending:
                break
            results_input: list[dict[str, Any]] = []
            for fc in pending:
                name = _fc_name(fc)
                args = _fc_args(fc)
                call_id = _fc_id(fc)
                result = self._dispatch_host_function(
                    name, args, role=role, fallback_payload=fallback_payload
                )
                if name == "message_agent" and args.get("to") == self.cfg["hardener_id"]:
                    handled["hardener_reply"] = result.get("bridge_result") or result
                results_input.append(
                    {
                        "type": "function_result",
                        "name": name,
                        "call_id": call_id,
                        "result": result,
                    }
                )
                self._emit(
                    "function_call_handled",
                    {"role": role, "name": name, "call_id": call_id, "args_keys": list(args.keys())},
                )
            interaction = self._create(
                role,
                results_input,
                previous_id=interaction.id,
                environment=getattr(interaction, "environment_id", None),
                tools=engineer_tools() if role == "engineer" else hardener_tools(),
                timeout_s=max(30.0, deadline - time.perf_counter()),
            )
            if role == "engineer":
                self.engineer_prev_id = interaction.id
                self.engineer_env = getattr(interaction, "environment_id", None) or self.engineer_env
            else:
                self.hardener_prev_id = interaction.id
                self.hardener_env = getattr(interaction, "environment_id", None) or self.hardener_env
        return interaction, handled

    def _drain_host_tools(self, interaction: Any, *, role: str, timeout_s: float) -> Any:
        interaction, _ = self._handle_until_complete(interaction, role=role, timeout_s=timeout_s)
        return interaction

    def _dispatch_host_function(
        self,
        name: str,
        args: dict[str, Any],
        *,
        role: str,
        fallback_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if name == "report_progress":
            self._emit("progress", {"role": role, **args})
            return {"ok": True}
        if name == "request_human_review":
            self._emit("human_review", {"role": role, **args})
            return {"ok": True, "queued": True}
        if name == "read_rulebook":
            return {"ok": False, "error": "read_rulebook not wired in this bridge turn; use host job run."}
        if name == "verify_constraint":
            return {"ok": False, "error": "verify_constraint not wired in this bridge turn; use host job run."}
        if name == "message_agent":
            to = str(args.get("to") or "")
            payload = args.get("payload") or fallback_payload or {}
            if not isinstance(payload, dict):
                payload = {"type": "raw", "value": payload}
            if to == self.cfg["hardener_id"] or to == "gridsafe-hardener":
                bridge_result = self.send_to_hardener(payload)
                return {"ok": True, "bridge_result": bridge_result}
            if to == self.cfg["engineer_id"] or to == "gridsafe-engineer":
                self._emit(
                    "message_out",
                    {"from": role, "to": to, "payload": payload, "note": "engineer inbound not auto-run"},
                )
                return {"ok": True, "queued_for_engineer": True, "payload": payload}
            return {"ok": False, "error": f"unknown target agent {to}"}
        return {"ok": False, "error": f"unknown function {name}"}

    @staticmethod
    def _parse_json_loose(text: str) -> Any:
        text = text.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            pass
        # fenced block
        if "```" in text:
            parts = text.split("```")
            for part in parts[1:]:
                body = part.lstrip()
                if body.startswith("json"):
                    body = body[4:]
                body = body.strip()
                try:
                    return json.loads(body)
                except Exception:
                    continue
        # first {...}
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                return {"raw_text": text}
        return {"raw_text": text}


# Re-export for agents.__init__
__all__ = ["AgentBridge", "AgentThreadLogger"]
