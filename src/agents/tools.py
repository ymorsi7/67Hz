"""Custom function schemas exposed to managed agents (host-executed)."""

from __future__ import annotations

from typing import Any


def message_agent_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "message_agent",
        "description": (
            "Send a structured JSON message to another GridSafe managed agent "
            "(engineer <-> hardener) via the host Interactions API bridge. "
            "Use this for adversarial review; do not nest subagents."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "enum": ["gridsafe-engineer", "gridsafe-hardener"],
                    "description": "Target saved agent id.",
                },
                "payload": {
                    "type": "object",
                    "description": "Structured JSON message body.",
                    "properties": {
                        "type": {
                            "type": "string",
                            "description": "Message type, e.g. submit_controller, counterexample, fix, no_break.",
                        },
                        "controller_code": {"type": "string"},
                        "constraints": {"type": "array", "items": {"type": "object"}},
                        "notes": {"type": "string"},
                        "counterexamples": {"type": "array", "items": {"type": "object"}},
                        "status": {"type": "string"},
                    },
                    "required": ["type"],
                },
            },
            "required": ["to", "payload"],
        },
    }


def report_progress_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "report_progress",
        "description": "Report a short progress update to the host UI / job log.",
        "parameters": {
            "type": "object",
            "properties": {
                "step": {"type": "string"},
                "message": {"type": "string"},
                "pct": {"type": "number"},
            },
            "required": ["step", "message"],
        },
    }


def request_human_review_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "request_human_review",
        "description": "Flag an item that needs human review (abstain rather than guess).",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "constraint_id": {"type": "string"},
                "page": {"type": "integer"},
            },
            "required": ["reason"],
        },
    }


def read_rulebook_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "read_rulebook",
        "description": (
            "Backend Neon Reader: extract typed constraints from a PDF path or page images. "
            "Agents must never parse PDFs themselves."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string"},
                "page_images_b64": {"type": "array", "items": {"type": "string"}},
                "max_pages": {"type": "integer"},
            },
        },
    }


def verify_constraint_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "verify_constraint",
        "description": (
            "Backend Decisions Verifier: calibrated yes/no for one constraint against its cited page."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "constraint": {"type": "object"},
                "source_path": {"type": "string"},
                "page": {"type": "integer"},
            },
            "required": ["constraint"],
        },
    }


def engineer_tools() -> list[dict[str, Any]]:
    return [
        {"type": "code_execution"},
        message_agent_tool(),
        report_progress_tool(),
        request_human_review_tool(),
        read_rulebook_tool(),
        verify_constraint_tool(),
    ]


def hardener_tools() -> list[dict[str, Any]]:
    return [
        {"type": "code_execution"},
        message_agent_tool(),
        report_progress_tool(),
    ]
