"""GridSafe UI API router (local loop: Reader → Verifier → Evolve → Attack → Certify).

Mount with: `app.include_router(ui_router)` from `server.ui_api`.
Standalone test app: `uvicorn server.ui_dev:app --port 8001`.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import numpy as np
from dotenv import load_dotenv
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

UPLOAD_DIR = REPO_ROOT / "eval" / "tmp" / "ui_uploads"
REPLAY_DIR = REPO_ROOT / "eval" / "tmp" / "replays"
LIVE_DIR = REPO_ROOT / "eval" / "live"
AGENT_THREADS_DIR = REPO_ROOT / "eval" / "agent_threads"

for _d in (UPLOAD_DIR, REPLAY_DIR, LIVE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

ui_router = APIRouter(prefix="/api/ui", tags=["ui"])

# ---------------------------------------------------------------------------
# In-memory session store
# ---------------------------------------------------------------------------


@dataclass
class UISession:
    session_id: str
    source_name: str = ""
    source_kind: str = "pdf"  # pdf | camera
    pdf_path: Path | None = None
    pdf_bytes: bytes | None = None
    page_count: int = 0
    selected_pages: list[int] = field(default_factory=list)
    page_images: dict[int, bytes] = field(default_factory=dict)  # camera / override
    rated_mw: float = 100.0
    constraints: list[dict[str, Any]] = field(default_factory=list)
    verification: dict[str, Any] | None = None
    reader_model: str = ""
    verifier_model: str = ""
    controller_code: str | None = None
    evolve_result: dict[str, Any] | None = None
    traces: dict[str, Any] | None = None
    certificate: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)


_SESSIONS: dict[str, UISession] = {}
_SESSIONS_LOCK = threading.Lock()


def _get_session(session_id: str) -> UISession:
    with _SESSIONS_LOCK:
        sess = _SESSIONS.get(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"Unknown session {session_id}")
    return sess


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _sse_response(gen: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _git_meta() -> dict[str, Any]:
    try:
        count = subprocess.check_output(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=REPO_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        first = subprocess.check_output(
            ["git", "log", "--reverse", "--format=%cI", "-n", "1"],
            cwd=REPO_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return {"commit_count": int(count), "first_commit_time": first}
    except Exception:
        return {"commit_count": 0, "first_commit_time": None}


def _downsample(arr: np.ndarray, max_points: int = 400) -> list[float]:
    a = np.asarray(arr, dtype=np.float64)
    if a.size <= max_points:
        return [float(x) for x in a]
    idx = np.linspace(0, a.size - 1, max_points).astype(int)
    return [float(a[i]) for i in idx]


def _load_controller(code: str) -> Callable[[dict[str, Any]], dict[str, float]]:
    """Load controller with the same math/numpy-only sandbox rules as evolve/_runner."""
    import builtins
    import math as _math

    from evolve.sandbox import validate_controller_code

    check = validate_controller_code(code)
    if not check.ok:
        raise ValueError("; ".join(check.violations) or "Invalid controller")

    allowed_imports = {"math", "numpy", "__future__"}

    def _safe_import(
        name: str,
        globals_: Any = None,
        locals_: Any = None,
        fromlist: Any = (),
        level: int = 0,
    ) -> Any:
        top = name.split(".")[0]
        if level != 0 or top not in allowed_imports:
            raise ImportError(f"Import of '{name}' is prohibited")
        return builtins.__import__(name, globals_, locals_, fromlist, level)

    allowed_names = (
        "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter",
        "float", "frozenset", "int", "isinstance", "len", "list", "map", "max",
        "min", "pow", "range", "reversed", "round", "set", "slice", "sorted",
        "str", "sum", "tuple", "zip", "Exception", "ValueError", "KeyError",
        "TypeError", "IndexError", "ZeroDivisionError", "RuntimeError",
    )
    safe_builtins = {k: getattr(builtins, k) for k in allowed_names if hasattr(builtins, k)}
    safe_builtins["__import__"] = _safe_import
    safe_builtins["True"] = True
    safe_builtins["False"] = False
    safe_builtins["None"] = None

    ns: dict[str, Any] = {
        "__builtins__": safe_builtins,
        "np": np,
        "numpy": np,
        "math": _math,
    }
    exec(compile(code, "<ui_controller>", "exec"), ns, ns)
    fn = ns.get("control")
    if not callable(fn):
        raise ValueError("Controller must define control(obs)")
    return fn  # type: ignore[return-value]


def _verified_ruleset(sess: UISession) -> Any:
    from rules.schema import Constraint, RuleSet

    items: list[Constraint] = []
    for raw in sess.constraints:
        c = Constraint.from_dict(raw)
        if c.checkable and c.verified is True and not c.needs_human_review:
            items.append(c)
    if not items:
        # Fall back to checkable-only (labeled) if verifier abstained on everything
        for raw in sess.constraints:
            c = Constraint.from_dict(raw)
            if c.checkable and c.value is not None:
                c.needs_human_review = False
                c.verified = True
                items.append(c)
    if not items:
        raise HTTPException(status_code=400, detail="No checkable constraints to evolve on")
    return RuleSet(
        constraints=items,
        rated_mw=sess.rated_mw,
        source_name=sess.source_name or "ui_session",
    )


def _build_traces(sess: UISession, code: str) -> dict[str, Any]:
    from rules.checker import check_compliance
    from sim.plant import noop_controller, simulate_plant
    from workload.synth import WorkloadParams, generate_workload

    ruleset = _verified_ruleset(sess)
    # Fast demo workload for result view (labeled simulated)
    params = WorkloadParams(
        cluster_mw=sess.rated_mw,
        duration_s=8.0,
        dt_s=0.004,
        period_s=0.5,
        depth=0.45,
        duty_cycle=0.75,
        seed=42,
        family="fast",
    )
    trace = generate_workload(params)
    ctrl = _load_controller(code)

    before = simulate_plant(trace, controller=noop_controller)
    after = simulate_plant(trace, controller=ctrl)
    report = check_compliance(after, ruleset, strict_review=False)

    # Forbidden bands from fluctuation_band constraints
    bands: list[dict[str, float]] = []
    for c in ruleset.constraints:
        if c.constraint_type == "fluctuation_band" and c.freq_min_hz is not None and c.freq_max_hz is not None:
            bands.append({"freq_min_hz": float(c.freq_min_hz), "freq_max_hz": float(c.freq_max_hz)})

    dom_idx = after.shaft.section_names.index(after.shaft.dominant_section) if after.shaft.section_names else 0
    torque_before = before.shaft.section_torque_pu[dom_idx] if before.shaft.section_torque_pu.size else np.zeros(1)
    torque_after = after.shaft.section_torque_pu[dom_idx] if after.shaft.section_torque_pu.size else np.zeros(1)

    # Spectrum (downsample freqs)
    freqs = after.shaft.freqs_hz
    spec_before = before.shaft.section_torque_spectrum_pu[dom_idx] if before.shaft.section_torque_spectrum_pu.size else np.zeros_like(freqs)
    spec_after = after.shaft.section_torque_spectrum_pu[dom_idx] if after.shaft.section_torque_spectrum_pu.size else np.zeros_like(freqs)
    # Cap spectrum to 0–60 Hz for UI
    mask = freqs <= 60.0
    freqs_m = freqs[mask]
    return {
        "label": "simulated",
        "workload": params.to_dict(),
        "compliance": report.to_dict(),
        "power": {
            "t_s": _downsample(after.t),
            "p_raw_mw": _downsample(after.p_raw_mw),
            "p_grid_before_mw": _downsample(before.p_grid_mw),
            "p_grid_after_mw": _downsample(after.p_grid_mw),
        },
        "shaft": {
            "label": "visual scale",
            "dominant_section": after.shaft.dominant_section,
            "max_torque_before_pu": float(before.shaft.max_torque_pu),
            "max_torque_after_pu": float(after.shaft.max_torque_pu),
            "torque_before_pu": _downsample(torque_before, 300),
            "torque_after_pu": _downsample(torque_after, 300),
            "t_s": _downsample(after.t, 300),
        },
        "spectrum": {
            "freqs_hz": _downsample(freqs_m, 250),
            "torque_before_pu": _downsample(spec_before[mask], 250),
            "torque_after_pu": _downsample(spec_after[mask], 250),
            "forbidden_bands": bands,
        },
        "metrics": {
            "before": before.metrics.to_dict(),
            "after": after.metrics.to_dict(),
        },
    }


def _save_replay(sess: UISession) -> Path:
    payload = {
        "session_id": sess.session_id,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_name": sess.source_name,
        "source_kind": sess.source_kind,
        "selected_pages": sess.selected_pages,
        "rated_mw": sess.rated_mw,
        "constraints": sess.constraints,
        "verification": sess.verification,
        "reader_model": sess.reader_model,
        "verifier_model": sess.verifier_model,
        "controller_code": sess.controller_code,
        "evolve_result": sess.evolve_result,
        "traces": sess.traces,
        "certificate": sess.certificate,
        "label": "recorded",
    }
    path = REPLAY_DIR / f"{sess.session_id}_{int(time.time())}.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    latest = REPLAY_DIR / "latest.json"
    latest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class SelectPagesBody(BaseModel):
    pages: list[int] = Field(default_factory=list)


class EvolveBody(BaseModel):
    budget_s: float = 75.0
    population_size: int = 6
    seed: int = 101


class AttackBody(BaseModel):
    rhythm: float = Field(0.5, ge=0.05, le=5.0, description="Pulse period in seconds")
    depth: float = Field(0.45, ge=0.0, le=1.0)
    cluster_mw: float = Field(100.0, ge=10.0, le=500.0)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@ui_router.get("/health")
async def ui_health() -> dict[str, Any]:
    return {
        "status": "ok",
        "mode": os.getenv("GRIDSAFE_MODE", "live"),
        "api_key_configured": bool(os.getenv("GEMINI_API_KEY")),
        "git": _git_meta(),
        "sessions": len(_SESSIONS),
    }


@ui_router.post("/upload")
async def upload_pdf(
    file: UploadFile = File(...),
    rated_mw: float = Form(100.0),
) -> dict[str, Any]:
    from reader.pdf_utils import get_pdf_page_count

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload")
    name = file.filename or "upload.pdf"
    if not name.lower().endswith(".pdf"):
        # Allow non-PDF if content-type says pdf; otherwise treat as camera later
        if file.content_type != "application/pdf":
            raise HTTPException(status_code=400, detail="Expected a PDF file")

    session_id = uuid.uuid4().hex[:12]
    dest = UPLOAD_DIR / f"{session_id}_{Path(name).name}"
    dest.write_bytes(data)
    try:
        page_count = get_pdf_page_count(data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid PDF: {exc}") from exc

    sess = UISession(
        session_id=session_id,
        source_name=name,
        source_kind="pdf",
        pdf_path=dest,
        pdf_bytes=data,
        page_count=page_count,
        rated_mw=float(rated_mw),
    )
    with _SESSIONS_LOCK:
        _SESSIONS[session_id] = sess

    return {
        "session_id": session_id,
        "source_name": name,
        "page_count": page_count,
        "rated_mw": sess.rated_mw,
        "thumbnail_url_template": f"/api/ui/session/{session_id}/thumbnail/{{page}}",
    }


@ui_router.post("/camera")
async def upload_camera(
    file: UploadFile = File(...),
    rated_mw: float = Form(100.0),
) -> dict[str, Any]:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty image")
    session_id = uuid.uuid4().hex[:12]
    mime = file.content_type or "image/jpeg"
    # Store as single-page session
    sess = UISession(
        session_id=session_id,
        source_name=file.filename or "camera_frame",
        source_kind="camera",
        page_count=1,
        selected_pages=[1],
        page_images={1: data},
        rated_mw=float(rated_mw),
    )
    # Persist image for replay
    ext = ".jpg" if "jpeg" in mime or "jpg" in mime else ".png"
    dest = UPLOAD_DIR / f"{session_id}_camera{ext}"
    dest.write_bytes(data)
    sess.pdf_path = dest
    with _SESSIONS_LOCK:
        _SESSIONS[session_id] = sess
    return {
        "session_id": session_id,
        "source_name": sess.source_name,
        "page_count": 1,
        "rated_mw": sess.rated_mw,
        "mime_type": mime,
        "thumbnail_url": f"/api/ui/session/{session_id}/thumbnail/1",
        "label": "camera snapshot",
    }


@ui_router.get("/session/{session_id}/thumbnail/{page}")
async def thumbnail(session_id: str, page: int, dpi: int = 72) -> FileResponse:
    from reader.pdf_utils import render_pdf_page_png

    sess = _get_session(session_id)
    if page < 1 or page > max(sess.page_count, 1):
        raise HTTPException(status_code=404, detail="Page out of range")

    cache = UPLOAD_DIR / f"{session_id}_thumb_p{page}_d{dpi}.png"
    if not cache.exists():
        if page in sess.page_images:
            cache.write_bytes(sess.page_images[page])
        elif sess.pdf_bytes is not None and sess.source_kind == "pdf":
            png = render_pdf_page_png(sess.pdf_bytes, page, dpi=dpi)
            cache.write_bytes(png)
        elif sess.pdf_path and sess.pdf_path.exists() and sess.source_kind == "camera":
            cache.write_bytes(sess.pdf_path.read_bytes())
        else:
            raise HTTPException(status_code=404, detail="No page image")
    return FileResponse(cache, media_type="image/png")


@ui_router.post("/session/{session_id}/select-pages")
async def select_pages(session_id: str, body: SelectPagesBody) -> dict[str, Any]:
    sess = _get_session(session_id)
    pages = sorted({int(p) for p in body.pages if 1 <= int(p) <= sess.page_count})
    if not pages:
        raise HTTPException(status_code=400, detail="Select at least one page")
    sess.selected_pages = pages
    return {"session_id": session_id, "selected_pages": pages}


@ui_router.post("/session/{session_id}/read-verify")
async def read_verify_stream(session_id: str, body: SelectPagesBody | None = None) -> StreamingResponse:
    """Run Reader then Verifier on selected pages; stream constraints with status over SSE."""
    sess = _get_session(session_id)
    if body and body.pages:
        pages = sorted({int(p) for p in body.pages if 1 <= int(p) <= max(sess.page_count, 1)})
        sess.selected_pages = pages
    pages = sess.selected_pages or (list(range(1, sess.page_count + 1)) if sess.page_count <= 3 else [])
    if not pages:
        raise HTTPException(status_code=400, detail="No pages selected")

    async def gen() -> AsyncIterator[str]:
        from reader.extract import extract_from_image_bytes
        from reader.pdf_utils import render_pdf_page_png
        from reader.verify import verify_constraints
        from rules.schema import Constraint

        yield _sse("status", {"phase": "read", "message": f"Reading {len(pages)} page(s)…", "pages": pages})
        await asyncio.sleep(0)

        all_constraints: list[Constraint] = []
        page_images: dict[int, bytes] = {}
        reader_model = ""

        try:
            if sess.source_kind == "camera" or (sess.pdf_bytes is None and sess.page_images):
                for pg in pages:
                    img = sess.page_images.get(pg)
                    if img is None and sess.pdf_path:
                        img = sess.pdf_path.read_bytes()
                    if img is None:
                        yield _sse("error", {"message": f"Missing image for page {pg}"})
                        return
                    page_images[pg] = img
                    yield _sse("page_read_start", {"page": pg})
                    await asyncio.sleep(0)
                    out = await asyncio.to_thread(
                        extract_from_image_bytes,
                        img,
                        "image/jpeg" if sess.source_name.lower().endswith((".jpg", ".jpeg")) else "image/png",
                        source_name=sess.source_name,
                        page_number=pg,
                        rated_mw=sess.rated_mw,
                    )
                    reader_model = out.model
                    if out.unreadable:
                        yield _sse(
                            "page_unreadable",
                            {"page": pg, "message": "Page unreadable — Needs human review"},
                        )
                    for c in out.constraints:
                        all_constraints.append(c)
                        payload = {**c.to_dict(), "ui_status": "pending", "phase": "read"}
                        yield _sse("constraint", payload)
                        await asyncio.sleep(0)
                    yield _sse(
                        "page_read_done",
                        {"page": pg, "count": len(out.constraints), "summary": out.summary},
                    )
            else:
                # PDF: extract only selected pages as images for precise page control
                assert sess.pdf_bytes is not None
                for pg in pages:
                    yield _sse("page_read_start", {"page": pg})
                    await asyncio.sleep(0)
                    png = await asyncio.to_thread(render_pdf_page_png, sess.pdf_bytes, pg, 150)
                    page_images[pg] = png
                    out = await asyncio.to_thread(
                        extract_from_image_bytes,
                        png,
                        "image/png",
                        source_name=sess.source_name,
                        page_number=pg,
                        rated_mw=sess.rated_mw,
                    )
                    reader_model = out.model
                    if out.unreadable:
                        yield _sse("page_unreadable", {"page": pg})
                    for c in out.constraints:
                        all_constraints.append(c)
                        yield _sse(
                            "constraint",
                            {**c.to_dict(), "ui_status": "pending", "phase": "read"},
                        )
                        await asyncio.sleep(0)
                    yield _sse(
                        "page_read_done",
                        {"page": pg, "count": len(out.constraints), "summary": out.summary},
                    )

            sess.reader_model = reader_model
            yield _sse(
                "read_complete",
                {
                    "count": len(all_constraints),
                    "reader_model": reader_model,
                    "checkable": sum(1 for c in all_constraints if c.checkable),
                },
            )
            await asyncio.sleep(0)

            # Verify
            yield _sse("status", {"phase": "verify", "message": "Verifying against page images…"})
            report = await asyncio.to_thread(
                verify_constraints,
                all_constraints,
                page_images=page_images,
                mime_type="image/png",
            )
            sess.verifier_model = report.verifier_model
            sess.verification = report.to_dict()

            # Merge verification into constraint dicts and stream updates
            by_id = {v.constraint_id: v for v in report.items}
            out_constraints: list[dict[str, Any]] = []
            for c in all_constraints:
                v = by_id.get(c.id)
                if v is not None:
                    c.confidence = v.confidence
                    c.verified = v.verified
                    c.needs_human_review = v.needs_human_review
                d = c.to_dict()
                if not c.checkable:
                    ui = "Uncheckable"
                elif v is None:
                    ui = "Needs human review"
                elif v.status == "verified":
                    ui = f"Verified ({v.confidence:.0%})"
                else:
                    ui = "Needs human review"
                d["ui_status"] = ui
                d["verify_status"] = v.status if v else "unknown"
                d["verify_reason"] = v.reason if v else ""
                d["phase"] = "verify"
                out_constraints.append(d)
                yield _sse("constraint", d)
                await asyncio.sleep(0)

            sess.constraints = out_constraints
            yield _sse(
                "verify_complete",
                {
                    "verification": report.to_dict(),
                    "constraints": out_constraints,
                    "reader_model": reader_model,
                    "verifier_model": report.verifier_model,
                },
            )
            yield _sse("done", {"ok": True})
        except Exception as exc:
            yield _sse("error", {"message": str(exc), "type": type(exc).__name__})
            yield _sse("done", {"ok": False})

    return _sse_response(gen())


@ui_router.post("/session/{session_id}/evolve")
async def evolve_stream(session_id: str, body: EvolveBody | None = None) -> StreamingResponse:
    sess = _get_session(session_id)
    opts = body or EvolveBody()

    async def gen() -> AsyncIterator[str]:
        from evolve.loop import evolve_controller
        from workload.synth import all_preset_workloads

        try:
            ruleset = _verified_ruleset(sess)
        except HTTPException as exc:
            yield _sse("error", {"message": exc.detail})
            yield _sse("done", {"ok": False})
            return

        workloads = all_preset_workloads(cluster_mw=sess.rated_mw)
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def on_event(payload: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, payload)

        yield _sse(
            "status",
            {
                "phase": "evolve",
                "message": "Evolving controller (local Hydrogen loop)…",
                "n_constraints": len(ruleset.constraints),
                "n_workloads": len(workloads),
                "budget_s": opts.budget_s,
            },
        )
        await asyncio.sleep(0)

        def run() -> Any:
            return evolve_controller(
                ruleset,
                workloads,
                budget_s=opts.budget_s,
                population_size=opts.population_size,
                seed=opts.seed,
                on_event=on_event,
            )

        fut = asyncio.create_task(asyncio.to_thread(run))
        best_diff = ""
        best_score: float | None = None

        def enrich(item: dict[str, Any]) -> dict[str, Any]:
            nonlocal best_diff, best_score
            ev = item.get("event", "")
            if ev in ("candidate_done", "new_best"):
                score = item.get("score")
                if item.get("passed") and score is not None and (
                    best_score is None or float(score) < best_score
                ):
                    best_score = float(score)
                diff = item.get("diff") or item.get("diff_from_parent")
                if diff:
                    best_diff = str(diff)
                gate = "pass" if item.get("passed") else "rejected_at_checker"
                if item.get("static_violations"):
                    gate = "rejected_static"
                return {**item, "best_score": best_score, "best_diff": best_diff, "gate": gate}
            if ev in ("generation_complete", "evolve_complete", "generation_summary"):
                return {**item, "best_score": best_score, "best_diff": best_diff}
            return {**item, "best_score": best_score}

        while True:
            if fut.done() and queue.empty():
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                if fut.done():
                    while not queue.empty():
                        drained = queue.get_nowait()
                        if drained is None:
                            continue
                        yield _sse("evolve", enrich(drained))
                    break
                continue
            if item is None:
                break
            yield _sse("evolve", enrich(item))
            await asyncio.sleep(0)

        try:
            result = await fut
        except Exception as exc:
            yield _sse("error", {"message": str(exc)})
            yield _sse("done", {"ok": False})
            return

        # Attach best diff from history
        best_code = result.best_code
        diff = ""
        if result.history:
            # Prefer last improving candidate's diff
            for h in reversed(result.history):
                if h.diff_from_parent:
                    diff = h.diff_from_parent
                    break
        sess.controller_code = best_code
        sess.evolve_result = {
            **result.to_dict(),
            "best_diff": diff,
            "fallback_rung": result.outcome,
        }

        # Build traces + certificate if we have a controller
        if best_code:
            try:
                sess.traces = await asyncio.to_thread(_build_traces, sess, best_code)
            except Exception as exc:
                sess.traces = {"error": str(exc), "label": "simulated"}
            sess.certificate = _build_certificate(sess)
            _save_replay(sess)

        yield _sse(
            "evolve_complete",
            {
                "outcome": result.outcome,
                "passed": result.passed,
                "fallback_rung": result.outcome,
                "best_score": result.best_eval.composite_score if result.best_eval else None,
                "best_diff": diff,
                "best_code": best_code,
                "evolve_result": sess.evolve_result,
                "elapsed_s": result.elapsed_s,
                "model": result.model,
            },
        )
        yield _sse("done", {"ok": True, "passed": result.passed})

    return _sse_response(gen())


def _build_certificate(sess: UISession) -> dict[str, Any]:
    ev = sess.evolve_result or {}
    best_eval = ev.get("best_eval") or {}
    failing = {f.get("constraint_id"): f for f in (best_eval.get("failing_constraints") or [])}
    # Margins from per_workload last successful if available
    margin_by_id: dict[str, Any] = {}
    for wl in best_eval.get("per_workload") or []:
        for r in wl.get("results") or []:
            cid = r.get("constraint_id")
            if cid:
                margin_by_id[cid] = r

    rules = []
    for c in sess.constraints:
        if not c.get("checkable"):
            continue
        cid = c.get("id")
        m = margin_by_id.get(cid) or failing.get(cid) or {}
        rules.append(
            {
                "id": cid,
                "constraint_type": c.get("constraint_type"),
                "entity": c.get("entity"),
                "page": c.get("page"),
                "snippet": c.get("snippet"),
                "value": c.get("value"),
                "unit": c.get("unit"),
                "assumptions": c.get("assumptions") or [],
                "ui_status": c.get("ui_status"),
                "confidence": c.get("confidence"),
                "margin": m.get("margin"),
                "margin_pct": m.get("margin_pct"),
                "measured": m.get("measured"),
                "limit": m.get("limit"),
                "status": m.get("status") or ("pass" if ev.get("passed") else "unknown"),
            }
        )

    return {
        "outcome": ev.get("outcome"),
        "passed": ev.get("passed"),
        "fallback_rung": ev.get("fallback_rung") or ev.get("outcome"),
        "models": {
            "reader": {"model": sess.reader_model or "gemini-4-neon", "job": "Document → typed constraints"},
            "verifier": {
                "model": sess.verifier_model or "gemini-decision-1",
                "job": "Calibrated yes/no against page image",
            },
            "designer": {
                "model": ev.get("model") or "gemini-4-hydrogen",
                "job": "Parallel controller mutations (local evolve loop)",
            },
        },
        "source_name": sess.source_name,
        "selected_pages": sess.selected_pages,
        "rules": rules,
        "assumptions": list(
            {a for c in sess.constraints for a in (c.get("assumptions") or [])}
        ),
        "attacks_survived": None,  # filled by live attack tally in UI
        "label": "certificate from local loop run",
    }


@ui_router.get("/session/{session_id}/traces")
async def get_traces(session_id: str) -> dict[str, Any]:
    sess = _get_session(session_id)
    if sess.traces is None and sess.controller_code:
        sess.traces = await asyncio.to_thread(_build_traces, sess, sess.controller_code)
    if sess.traces is None:
        raise HTTPException(status_code=404, detail="No traces yet — run evolve first")
    return sess.traces


@ui_router.get("/session/{session_id}/state")
async def get_state(session_id: str) -> dict[str, Any]:
    sess = _get_session(session_id)
    return {
        "session_id": sess.session_id,
        "source_name": sess.source_name,
        "source_kind": sess.source_kind,
        "page_count": sess.page_count,
        "selected_pages": sess.selected_pages,
        "rated_mw": sess.rated_mw,
        "constraints": sess.constraints,
        "verification": sess.verification,
        "reader_model": sess.reader_model,
        "verifier_model": sess.verifier_model,
        "has_controller": bool(sess.controller_code),
        "evolve_result": {
            k: v
            for k, v in (sess.evolve_result or {}).items()
            if k not in ("history", "best_code")
        }
        if sess.evolve_result
        else None,
        "controller_code": sess.controller_code,
        "certificate": sess.certificate,
        "has_traces": sess.traces is not None,
    }


@ui_router.post("/session/{session_id}/attack")
async def attack(session_id: str, body: AttackBody) -> dict[str, Any]:
    """Simulate with current controller under judge workload params. Fast path for sliders."""
    sess = _get_session(session_id)
    if not sess.controller_code:
        raise HTTPException(status_code=400, detail="No controller — run evolve first")

    from rules.checker import check_compliance
    from sim.plant import simulate_plant
    from workload.synth import WorkloadParams, generate_workload

    ruleset = _verified_ruleset(sess)
    # Map rhythm → period; keep short for live dragging
    period = float(body.rhythm)
    params = WorkloadParams(
        cluster_mw=float(body.cluster_mw),
        duration_s=min(6.0, max(2.0, period * 8)),
        dt_s=0.004,
        period_s=period,
        depth=float(body.depth),
        duty_cycle=0.7,
        checkpoint_interval_s=None,
        job_stop_s=None,
        seed=7,
        family="fast",
    )
    t0 = time.perf_counter()
    try:
        ctrl = _load_controller(sess.controller_code)
        trace = generate_workload(params)
        sim = simulate_plant(trace, controller=ctrl)
        report = check_compliance(sim, ruleset, strict_review=False)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    # Persist live attack tally
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    rec = {
        "session_id": session_id,
        "ts": time.time(),
        "params": body.model_dump(),
        "passed": report.passed,
        "status": report.status,
        "worst_margin_pct": report.worst_margin_pct,
        "elapsed_ms": round(elapsed_ms, 1),
        "label": "live judge attack (simulated)",
    }
    (LIVE_DIR / f"{session_id}_{int(time.time() * 1000)}.json").write_text(
        json.dumps(rec), encoding="utf-8"
    )

    return {
        "passed": report.passed,
        "status": report.status,
        "elapsed_ms": round(elapsed_ms, 1),
        "workload": params.to_dict(),
        "max_shaft_torque_pu": float(sim.shaft.max_torque_pu),
        "results": [r.to_dict() for r in report.results],
        "worst_margin_pct": report.worst_margin_pct,
        "worst_constraint_id": report.worst_constraint_id,
        "label": "simulated",
    }


@ui_router.get("/agent-threads")
async def list_agent_threads() -> dict[str, Any]:
    if not AGENT_THREADS_DIR.exists():
        return {"threads": [], "path": str(AGENT_THREADS_DIR)}
    threads: list[dict[str, Any]] = []
    files = [
        *AGENT_THREADS_DIR.glob("*.json"),
        *AGENT_THREADS_DIR.glob("*.jsonl"),
    ]
    for p in sorted(files, key=lambda x: x.stat().st_mtime, reverse=True):
        if ".meta." in p.name:
            continue
        text = ""
        try:
            text = p.read_text(encoding="utf-8")
            if p.suffix == ".jsonl":
                lines = [json.loads(line) for line in text.splitlines() if line.strip()]
                data: Any = {"events": lines, "event_count": len(lines)}
            else:
                data = json.loads(text)
        except Exception as exc:
            data = {"raw_error": str(exc), "preview": text[:400]}
        threads.append({"file": p.name, "data": data})
    return {"threads": threads, "path": str(AGENT_THREADS_DIR)}


def _latest_replay_path() -> Path | None:
    latest = REPLAY_DIR / "latest.json"
    if latest.exists():
        return latest
    files = sorted(REPLAY_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)
    files = [f for f in files if f.name != "latest.json"]
    return files[0] if files else None


@ui_router.get("/replay/latest")
async def replay_latest() -> JSONResponse:
    latest = _latest_replay_path()
    if latest is None:
        return JSONResponse({"replay": None, "label": "recorded", "message": "No replays yet"})
    data = json.loads(latest.read_text(encoding="utf-8"))
    data["label"] = "recorded"
    return JSONResponse({"replay": data, "label": "recorded"})


@ui_router.post("/replay/load")
async def replay_load() -> dict[str, Any]:
    """Hydrate an in-memory session from the latest recorded replay (for Attack/Certify after restart)."""
    latest = _latest_replay_path()
    if latest is None:
        raise HTTPException(status_code=404, detail="No recorded replays yet")
    data = json.loads(latest.read_text(encoding="utf-8"))
    session_id = uuid.uuid4().hex[:12]
    sess = UISession(
        session_id=session_id,
        source_name=str(data.get("source_name") or "replay"),
        source_kind="replay",
        page_count=max(data.get("selected_pages") or [1]),
        selected_pages=list(data.get("selected_pages") or []),
        rated_mw=float(data.get("rated_mw") or 100.0),
        constraints=list(data.get("constraints") or []),
        verification=data.get("verification"),
        reader_model=str(data.get("reader_model") or ""),
        verifier_model=str(data.get("verifier_model") or ""),
        controller_code=data.get("controller_code"),
        evolve_result=data.get("evolve_result"),
        traces=data.get("traces") if isinstance(data.get("traces"), dict) and "error" not in (data.get("traces") or {}) else None,
        certificate=data.get("certificate"),
    )
    with _SESSIONS_LOCK:
        _SESSIONS[session_id] = sess
    # Rebuild traces if missing/broken and we have a controller
    if sess.controller_code and sess.traces is None:
        try:
            sess.traces = await asyncio.to_thread(_build_traces, sess, sess.controller_code)
            if sess.certificate is None:
                sess.certificate = _build_certificate(sess)
        except Exception as exc:
            sess.traces = {"error": str(exc), "label": "simulated"}
    return {
        "session_id": session_id,
        "label": "recorded",
        "source_name": sess.source_name,
        "has_controller": bool(sess.controller_code),
        "has_traces": sess.traces is not None and "error" not in (sess.traces or {}),
        "constraints": sess.constraints,
        "controller_code": sess.controller_code,
        "certificate": sess.certificate,
        "evolve_result": {
            k: v for k, v in (sess.evolve_result or {}).items() if k not in ("history", "best_code")
        }
        if sess.evolve_result
        else None,
        "fallback_rung": (sess.evolve_result or {}).get("outcome"),
    }


@ui_router.post("/session/{session_id}/save-replay")
async def save_replay(session_id: str) -> dict[str, Any]:
    sess = _get_session(session_id)
    path = _save_replay(sess)
    return {"path": str(path.relative_to(REPO_ROOT)), "label": "recorded"}
