"""Inline environment sources for managed agents (skills + AGENTS.md + repo mount)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / ".agents"

SKILL_NAMES_ENGINEER = ("read-rulebook", "evolve-controller", "certify")
SKILL_NAMES_HARDENER = ("red-team", "analyze-failure")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def skill_sources(skill_names: tuple[str, ...]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name in skill_names:
        path = AGENTS_DIR / "skills" / name / "SKILL.md"
        out.append(
            {
                "type": "inline",
                "target": f".agents/skills/{name}/SKILL.md",
                "content": _read(path),
            }
        )
    return out


def base_environment_sources(*, repo_url: str, role: str) -> list[dict[str, Any]]:
    skills = SKILL_NAMES_ENGINEER if role == "engineer" else SKILL_NAMES_HARDENER
    sources: list[dict[str, Any]] = [
        {
            "type": "repository",
            "source": repo_url,
            "target": "/workspace/repo",
        },
        {
            "type": "inline",
            "target": ".agents/AGENTS.md",
            "content": _read(AGENTS_DIR / "AGENTS.md"),
        },
    ]
    sources.extend(skill_sources(skills))
    hooks = AGENTS_DIR / "hooks.json"
    if hooks.exists():
        sources.append(
            {
                "type": "inline",
                "target": ".agents/hooks.json",
                "content": _read(hooks),
            }
        )
    return sources
