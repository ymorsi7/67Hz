"""Shared Gemini client helpers for managed agents (v1beta)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from google import genai

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_PATH = REPO_ROOT / "config" / "models.yaml"


def _ensure_dotenv() -> None:
    load_dotenv(REPO_ROOT / ".env")


@lru_cache(maxsize=1)
def get_genai_client() -> genai.Client:
    _ensure_dotenv()
    return genai.Client(http_options={"api_version": "v1beta"})


def load_managed_config() -> dict[str, Any]:
    raw = yaml.safe_load(MODELS_PATH.read_text(encoding="utf-8")) or {}
    managed = dict(raw.get("managed_agent") or {})
    hardener = dict(raw.get("hardener") or {})
    designer = dict(raw.get("designer") or {})
    return {
        "engineer_id": managed.get("id", "gridsafe-engineer"),
        "hardener_id": managed.get("hardener_agent_id", "gridsafe-hardener"),
        "base_agent": managed.get("base_agent", "antigravity-preview-09-2026"),
        "engineer_model": managed.get("model") or designer.get("model", "gemini-4-hydrogen"),
        "hardener_model": managed.get("hardener_model") or hardener.get("model", "gemini-4-carbon"),
        "repo_url": managed.get("repo_url", "https://github.com/ymorsi7/67Hz"),
        "enable_agents": bool(managed.get("enable_agents", True)),
    }
