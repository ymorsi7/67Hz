"""Standalone UI API server on port 8001 for local frontend testing.

Run:
  .venv/bin/uvicorn server.ui_dev:app --host 127.0.0.1 --port 8001 --reload
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from server.ui_api import ui_router

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

app = FastAPI(title="GridSafe UI Dev", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(ui_router)


@app.get("/api/health")
async def health() -> dict:
    """Compat shim so the scaffold frontend /api/health still works during UI-dev."""
    from server.ui_api import ui_health

    return await ui_health()
