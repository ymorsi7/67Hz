"""GridSafe FastAPI server with SSE streaming and static React frontend serving."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any, AsyncIterator

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"
MODELS_CONFIG_PATH = REPO_ROOT / "config" / "models.yaml"

app = FastAPI(title="GridSafe", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def load_models_config() -> dict[str, Any]:
    if not MODELS_CONFIG_PATH.exists():
        return {}
    with MODELS_CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_git_footer_meta() -> dict[str, Any]:
    try:
        count = subprocess.check_output(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=REPO_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        first_commit_time = subprocess.check_output(
            ["git", "log", "--reverse", "--format=%cI", "-n", "1"],
            cwd=REPO_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return {
            "commit_count": int(count),
            "first_commit_time": first_commit_time,
        }
    except Exception:
        return {"commit_count": 0, "first_commit_time": None}


def sse_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "mode": os.getenv("GRIDSAFE_MODE", "live"),
        "api_key_configured": bool(os.getenv("GEMINI_API_KEY")),
        "models": load_models_config(),
        "git": get_git_footer_meta(),
    }


@app.get("/api/stream/ping")
async def stream_ping() -> StreamingResponse:
    """Minimal SSE endpoint verifying end-to-end streaming."""

    async def event_generator() -> AsyncIterator[str]:
        steps = ["Input", "Read", "Verify", "Evolve", "Attack", "Certify"]
        for idx, step in enumerate(steps):
            yield sse_event("step", {"index": idx, "name": step, "status": "ready"})
            await asyncio.sleep(0.05)
        yield sse_event("done", {"ok": True})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if FRONTEND_DIST.exists() and (FRONTEND_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")


@app.get("/{full_path:path}")
async def serve_spa(full_path: str) -> Any:
    if full_path.startswith("api/"):
        return {"error": "Not found"}
    if full_path:
        candidate = (FRONTEND_DIST / full_path).resolve()
        if candidate.is_file() and FRONTEND_DIST.resolve() in candidate.parents:
            return FileResponse(candidate)
    index_file = FRONTEND_DIST / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {
        "message": "Frontend not built yet. Run `make build-ui` or `cd frontend && npm run dev`.",
    }
