"""Structured JSON agent-to-agent thread logs under eval/agent_threads/."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from repo_paths import find_repo_root
from typing import Any

REPO_ROOT = find_repo_root()
THREADS_DIR = REPO_ROOT / "eval" / "agent_threads"


class AgentThreadLogger:
    def __init__(self, thread_id: str, *, meta: dict[str, Any] | None = None) -> None:
        THREADS_DIR.mkdir(parents=True, exist_ok=True)
        self.thread_id = thread_id
        self.path = THREADS_DIR / f"{thread_id}.jsonl"
        self.meta_path = THREADS_DIR / f"{thread_id}.meta.json"
        self.meta = {
            "thread_id": thread_id,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            **(meta or {}),
        }
        self.meta_path.write_text(json.dumps(self.meta, indent=2) + "\n", encoding="utf-8")

    def log(self, event: dict[str, Any]) -> None:
        row = {
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "thread_id": self.thread_id,
            **event,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

    def update_meta(self, **kwargs: Any) -> None:
        self.meta.update(kwargs)
        self.meta_path.write_text(json.dumps(self.meta, indent=2) + "\n", encoding="utf-8")
