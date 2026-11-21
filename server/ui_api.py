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
    """Footer meta. Oldest commit time — never use `git log --reverse -n 1` (limits before reverse)."""
    try:
        count = subprocess.check_output(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=REPO_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        roots = subprocess.check_output(
            ["git", "rev-list", "--max-parents=0", "HEAD"],
            cwd=REPO_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip().splitlines()
        root = roots[0] if roots else "HEAD"
        first = subprocess.check_output(
            ["git", "log", "-n", "1", "--format=%cI", root],
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


def _power_spectrum_mw(p_grid_mw: np.ndarray, dt_s: float) -> tuple[np.ndarray, np.ndarray]:
    """One-sided FFT amplitude spectrum of grid power (MW), matching checker convention."""
    n = len(p_grid_mw)
    dp = p_grid_mw - float(np.mean(p_grid_mw))
    freqs = np.fft.rfftfreq(n, d=dt_s)
    fft_vals = np.fft.rfft(dp)
    scale = np.full(len(freqs), 2.0 / n, dtype=np.float64)
    scale[0] = 1.0 / n
    if n % 2 == 0:
        scale[-1] = 1.0 / n
    amps = np.abs(fft_vals) * scale
    return freqs, amps


def _build_traces(sess: UISession, code: str) -> dict[str, Any]:
    from rules.checker import check_compliance
    from sim.plant import noop_controller, simulate_plant
    from workload.synth import WorkloadParams, generate_workload

    ruleset = _verified_ruleset(sess)
    # Result-view workload: long enough for typical ramp windows when present
    max_window = max((float(c.window_s or 0.0) for c in ruleset.constraints), default=0.0)
    duration = max(20.0, 2.0 * max_window) if max_window > 0 else 20.0
    duration = min(duration, 120.0)
    dt = 0.5 if duration >= 60.0 else 0.004
    params = WorkloadParams(
        cluster_mw=sess.rated_mw,
        duration_s=duration,
        dt_s=dt,
        period_s=0.5 if dt < 0.1 else 30.0,
        depth=0.45,
        duty_cycle=0.75,
        seed=42,
        family="slow" if duration >= 60.0 else "fast",
    )
    trace = generate_workload(params)
    ctrl = _load_controller(code)

    before = simulate_plant(trace, controller=noop_controller)
    after = simulate_plant(trace, controller=ctrl)
    report = check_compliance(after, ruleset, strict_review=False)

    limit_bands: list[dict[str, Any]] = []
    for c in ruleset.constraints:
        if c.constraint_type != "fluctuation_band" or c.value is None:
            continue
        from rules.units import UnitConversionError, normalize_constraint_limit

        try:
            lim, unit = normalize_constraint_limit(c, rated_mw=ruleset.rated_mw)
        except UnitConversionError:
            continue
        if unit != "MW":
            continue
        limit_bands.append(
            {
                "freq_min_hz": float(c.freq_min_hz or 0.0),
                "freq_max_hz": float(c.freq_max_hz or 55.0),
                "limit_mw": float(lim),
                "plain": plain_rule_sentence(c),
            }
        )

    freqs_b, amp_b = _power_spectrum_mw(before.p_grid_mw, before.dt_s)
    freqs_a, amp_a = _power_spectrum_mw(after.p_grid_mw, after.dt_s)
    mask = freqs_a <= 60.0

    # Shaft mass twists for visual (6 discs)
    masses = ["HP", "IP", "LPA", "LPB", "GEN", "EXC"]
    twist_before = before.shaft.mass_twist_rad
    twist_after = after.shaft.mass_twist_rad
    # Peak |twist| per mass over time for animation scale
    peak_twist_before = [float(np.max(np.abs(twist_before[i]))) if twist_before.size else 0.0 for i in range(min(6, twist_before.shape[0]))] if twist_before.ndim == 2 else [0.0] * 6
    peak_twist_after = [float(np.max(np.abs(twist_after[i]))) if twist_after.size else 0.0 for i in range(min(6, twist_after.shape[0]))] if twist_after.ndim == 2 else [0.0] * 6
    while len(peak_twist_before) < 6:
        peak_twist_before.append(0.0)
    while len(peak_twist_after) < 6:
        peak_twist_after.append(0.0)

    t_b = float(before.shaft.max_torque_pu)
    t_a = float(after.shaft.max_torque_pu)
    ratio = (t_b / t_a) if t_a > 1e-9 else None

    return {
        "label": "simulated",
        "workload": params.to_dict(),
        "compliance": report.to_dict(),
        "power": {
            "t_s": _downsample(after.t),
            "p_raw_mw": _downsample(after.p_raw_mw),
            "without_mw": _downsample(before.p_grid_mw),
            "with_mw": _downsample(after.p_grid_mw),
            # aliases
            "p_grid_before_mw": _downsample(before.p_grid_mw),
            "p_grid_after_mw": _downsample(after.p_grid_mw),
            "x_label": "Time (seconds)",
            "y_label": "Grid power (MW)",
            "legend": {"without": "Without GridSafe", "with": "With GridSafe"},
        },
        "shaft": {
            "label": "visual scale, simulated",
            "masses": masses,
            "peak_twist_before": peak_twist_before,
            "peak_twist_after": peak_twist_after,
            "max_torque_before_pu": t_b,
            "max_torque_after_pu": t_a,
            "stress_before_pu": t_b,
            "stress_after_pu": t_a,
            "times_lower": round(ratio, 1) if ratio is not None else None,
            "dominant_section": after.shaft.dominant_section,
        },
        "spectrum": {
            "freqs_hz": _downsample(freqs_a[mask], 250),
            "without_mw": _downsample(amp_b[mask], 250),
            "with_mw": _downsample(amp_a[mask], 250),
            "limit_bands": limit_bands,
            "x_label": "Frequency (Hz)",
            "y_label": "Power swing amplitude (MW)",
            "legend": {"without": "Without GridSafe", "with": "With GridSafe"},
        },
        "metrics": {
            "extra_energy_pct": float(after.metrics.energy_overhead_pct),
            "training_slowed_pct": float(after.metrics.throughput_loss_pct),
            "battery_wear": float(after.metrics.battery_wear_proxy),
            "lower_is_better": True,
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
    """Judge attack params. pulse_rate = training steps per second (period = 1/rate)."""

    pulse_rate: float = Field(2.0, ge=0.2, le=20.0, description="Training steps per second")
    depth: float = Field(0.45, ge=0.0, le=1.0, description="Fraction of full power")
    cluster_mw: float = Field(100.0, ge=10.0, le=500.0)
    # Back-compat with older UI that sent period as rhythm
    rhythm: float | None = Field(None, description="Deprecated: pulse period in seconds")


def plain_rule_sentence(c: dict[str, Any] | Any) -> str:
    """Template plain-English sentence from typed constraint fields (no model)."""
    get = c.get if isinstance(c, dict) else lambda k, d=None: getattr(c, k, d)
    ctype = str(get("constraint_type") or "")
    value = get("value")
    unit = str(get("unit") or "")
    window = get("window_s")
    fmin = get("freq_min_hz")
    fmax = get("freq_max_hz")
    if ctype == "ramp_rate" and value is not None:
        return f"Power may not change faster than {value:g} {unit}."
    if ctype == "max_step" and value is not None:
        win = f"{window:g}" if window is not None else "a few"
        return f"Power may not jump more than {value:g} MW within {win} seconds."
    if ctype == "fluctuation_band" and value is not None:
        unit_l = unit.strip().lower()
        if unit_l in {"pu", "p.u.", "pu torque", "p.u. torque"}:
            return f"Shaft stress from repeating swings must stay under {value:g} pu."
        lo = f"{fmin:g}" if fmin is not None else "?"
        hi = f"{fmax:g}" if fmax is not None else "?"
        return f"Repeating power swings between {lo} and {hi} Hz must stay under {value:g} MW."
    if ctype == "dynamic_range" and value is not None:
        return f"Power must stay within a {value:g} MW range."
    snippet = str(get("snippet") or "a rule on this page")
    return f"Other requirement: {snippet[:120]}"


def how_measured(c: dict[str, Any] | Any) -> tuple[str, bool]:
    """Return (plain measurement description, simplified_vs_document).

    Descriptions match what rules/checker.py actually computes.
    """
    get = c.get if isinstance(c, dict) else lambda k, d=None: getattr(c, k, d)
    ctype = str(get("constraint_type") or "")
    window = get("window_s")
    entity = str(get("entity") or "")
    snippet = str(get("snippet") or "")
    notes = str(get("notes") or "")
    unit = str(get("unit") or "").strip().lower()

    simplified = False
    if ctype == "ramp_rate":
        if window is not None:
            text = (
                f"Largest change in grid power across any {window:g} s rolling window, "
                f"divided by {window:g} s (MW/s)."
            )
        else:
            text = "Largest step-to-step change in grid power, divided by the time step (MW/s)."
    elif ctype == "max_step":
        if window is not None:
            text = f"Largest peak-to-peak swing of grid power inside any {window:g} s window (MW)."
        else:
            text = "Largest single time-step jump in grid power (MW)."
    elif ctype == "dynamic_range":
        if window is not None:
            text = f"Largest peak-to-peak swing of grid power inside any {window:g} s window (MW)."
        else:
            text = "Peak-to-peak swing of grid power over the whole run (MW)."
    elif ctype == "fluctuation_band":
        if unit in {"pu", "p.u.", "pu torque", "p.u. torque"}:
            text = "Peak torsional shaft torque from the IEEE First Benchmark Model over the whole run (pu)."
        else:
            agg = str(get("aggregation") or "peak_bin")
            if agg == "adjacent_pair_sum":
                text = (
                    "Largest sum of any two adjacent FFT bins of grid power (after removing the average) "
                    "inside the stated frequency band (MW)."
                )
            elif agg == "band_sum":
                text = (
                    "Sum of all FFT bins of grid power (after removing the average) "
                    "inside the stated frequency band (MW)."
                )
            else:
                text = (
                    "Largest single FFT bin amplitude of grid power (after removing the average) "
                    "inside the stated frequency band, over the whole run (MW)."
                )
            avg_w = get("averaging_window_s")
            blob = f"{snippet} {notes} {entity}".lower()
            # Document asks for rolling-window averaging of FFT outputs; checker may use whole-run FFT
            if avg_w or "rolling" in blob:
                if agg == "peak_bin" or "rolling" in blob:
                    simplified = True
                    text += (
                        " Simplified measurement: the document describes averaging over rolling windows; "
                        "this check uses the spectrum of the whole run."
                    )
            if agg == "peak_bin" and any(k in blob for k in ("adjacent", "sum of", "bins")):
                simplified = True
                text += (
                    " The document describes summing bins; this check uses the single largest bin."
                )
    else:
        text = "This rule is not simulated automatically."
    return text, simplified


def outcome_headline(outcome: str, n_rules: int, gemini_passed: int | None = None) -> str:
    if outcome == "evolved_compliant":
        return f"Winner: a Gemini-written controller. Follows all {n_rules} rules."
    if outcome == "fallback_b3":
        n = gemini_passed if gemini_passed is not None else 0
        return (
            f"Winner: the tuned filter baseline (no AI). {n} Gemini designs followed the rules, "
            "but none was cheaper."
        )
    if outcome == "fallback_conservative_floor":
        return "Fell back to the safe, expensive setting."
    if outcome == "refused":
        return "GridSafe refused: no controller met the rules."
    return "Controller run finished."


def _period_from_attack(body: AttackBody) -> float:
    if body.rhythm is not None and body.rhythm > 0:
        return float(body.rhythm)
    rate = max(0.2, float(body.pulse_rate))
    return 1.0 / rate


def _sim_params_for_constraint(
    c: Any,
    *,
    cluster_mw: float,
    period_s: float,
    depth: float,
    seed: int = 7,
) -> Any:
    """Choose duration/dt so this rule is evaluable (2x window, Nyquist above band)."""
    from workload.synth import WorkloadParams

    window = float(c.window_s) if c.window_s is not None else 0.0
    f_lo = float(c.freq_min_hz) if c.freq_min_hz is not None else 0.0
    f_hi = float(c.freq_max_hz) if c.freq_max_hz is not None else 0.0

    duration = 8.0
    if window > 0:
        duration = max(duration, 2.0 * window)
    if f_lo > 0:
        duration = max(duration, 2.0 / f_lo)
    # Cap so slider stays interactive; still prefer evaluable
    duration = float(min(duration, 180.0))

    if c.constraint_type == "fluctuation_band" and f_hi > 0:
        # Need Nyquist > f_hi ⇒ dt < 1/(2*f_hi); use margin
        dt = min(0.002, 0.35 / (2.0 * f_hi))
    elif window >= 30.0:
        dt = min(0.5, max(0.05, window / 200.0))
    elif window >= 5.0:
        dt = 0.02
    else:
        dt = 0.004

    # Keep sample count reasonable (~50k max)
    n_est = duration / dt
    if n_est > 50_000:
        dt = duration / 50_000.0

    family = "slow" if duration >= 60.0 or dt >= 0.1 else "fast"
    return WorkloadParams(
        cluster_mw=float(cluster_mw),
        duration_s=duration,
        dt_s=float(dt),
        period_s=float(period_s),
        depth=float(depth),
        duty_cycle=0.7,
        checkpoint_interval_s=None,
        job_stop_s=None,
        seed=seed,
        family=family,
    )


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

        n_rules = len(ruleset.constraints)
        be = result.best_eval
        yield _sse(
            "evolve_complete",
            {
                "outcome": result.outcome,
                "passed": result.passed,
                "headline": outcome_headline(result.outcome, n_rules, result.total_candidates_passed),
                "n_rules": n_rules,
                "candidates_tried": result.total_candidates_evaluated,
                "candidates_passed": result.total_candidates_passed,
                "extra_energy_pct": be.mean_energy_overhead_pct if be else None,
                "training_slowed_pct": be.mean_throughput_loss_pct if be else None,
                "battery_wear": be.mean_battery_wear if be else None,
                "lower_is_better": True,
                "best_diff": diff,
                "best_code": best_code,
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
    margin_by_id: dict[str, Any] = {}
    for wl in best_eval.get("per_workload") or []:
        for r in wl.get("results") or []:
            cid = r.get("constraint_id")
            if cid:
                margin_by_id[cid] = r

    n_rules = sum(1 for c in sess.constraints if c.get("checkable") and c.get("verified") is True)
    gemini_passed = int(ev.get("total_candidates_passed") or 0)
    rules = []
    for c in sess.constraints:
        if not c.get("checkable"):
            continue
        cid = c.get("id")
        m = margin_by_id.get(cid) or failing.get(cid) or {}
        measured = m.get("measured")
        limit = m.get("limit")
        used_pct = None
        if isinstance(measured, (int, float)) and isinstance(limit, (int, float)) and limit:
            used_pct = round(100.0 * float(measured) / float(limit), 1)
        measure_text, simplified = how_measured(c)
        rules.append(
            {
                "id": cid,
                "plain": plain_rule_sentence(c),
                "entity": c.get("entity"),
                "page": c.get("page"),
                "snippet": c.get("snippet"),
                "used_pct_of_limit": used_pct,
                "margin_pct": m.get("margin_pct"),
                "measured": measured,
                "limit": limit,
                "status": m.get("status") or ("pass" if ev.get("passed") else "unknown"),
                "how_measured": measure_text,
                "simplified_measurement": simplified,
                "confidence": c.get("confidence"),
                "verified": c.get("verified"),
                "needs_human_review": c.get("needs_human_review"),
            }
        )

    return {
        "outcome": ev.get("outcome"),
        "passed": ev.get("passed"),
        "headline": outcome_headline(str(ev.get("outcome") or ""), n_rules or len(rules), gemini_passed),
        "models": {
            "reader": {"model": sess.reader_model or "gemini-4-neon", "job": "Reads the rulebook pages"},
            "verifier": {
                "model": sess.verifier_model or "gemini-decision-1",
                "job": "Confirms each rule against the page image",
            },
            "designer": {
                "model": ev.get("model") or "gemini-4-hydrogen",
                "job": "Writes candidate controllers",
            },
        },
        "source_name": sess.source_name,
        "selected_pages": sess.selected_pages,
        "rules": rules,
        "metrics": {
            "extra_energy_pct": (best_eval or {}).get("mean_energy_overhead_pct"),
            "training_slowed_pct": (best_eval or {}).get("mean_throughput_loss_pct"),
            "battery_wear": (best_eval or {}).get("mean_battery_wear"),
            "lower_is_better": True,
        },
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
    """Attack the controller. Each rule is checked on a trace long/fine enough to evaluate it."""
    sess = _get_session(session_id)
    if not sess.controller_code:
        raise HTTPException(status_code=400, detail="No controller yet — design one first")

    from rules.checker import check_compliance
    from rules.schema import RuleSet
    from sim.plant import simulate_plant
    from workload.synth import generate_workload

    full = _verified_ruleset(sess)
    period = _period_from_attack(body)
    depth = float(body.depth)
    cluster = float(body.cluster_mw)
    t0 = time.perf_counter()

    def run() -> dict[str, Any]:
        ctrl = _load_controller(sess.controller_code or "")
        # Cache sims by (duration, dt) so shared windows share one trace
        sim_cache: dict[tuple[float, float], Any] = {}
        results: list[dict[str, Any]] = []
        broken_plain: str | None = None
        broken_over_pct: float | None = None

        for c in full.constraints:
            params = _sim_params_for_constraint(c, cluster_mw=cluster, period_s=period, depth=depth)
            key = (round(params.duration_s, 3), round(params.dt_s, 6))
            if key not in sim_cache:
                trace = generate_workload(params)
                sim_cache[key] = simulate_plant(trace, controller=ctrl)
            sim = sim_cache[key]
            # Evaluate this rule alone so other rules' windows don't confuse status
            single = RuleSet(constraints=[c], rated_mw=full.rated_mw, source_name=full.source_name)
            report = check_compliance(sim, single, strict_review=False)
            r = report.results[0] if report.results else None
            plain = plain_rule_sentence(c)
            if r is None:
                results.append(
                    {
                        "plain": plain,
                        "entity": c.entity,
                        "page": c.page,
                        "passed": None,
                        "used_pct_of_limit": None,
                        "cannot_check_reason": "No measurement returned for this rule.",
                    }
                )
                continue
            if r.status in ("not_evaluable",):
                # Should be rare after per-rule sizing; translate to plain words
                reason = r.details or "This run was too short or too coarse for that rule."
                if "2x rule window" in reason:
                    reason = (
                        f"Needs a run at least {2.0 * float(c.window_s or 0):g} seconds long "
                        f"(2× the rule's {c.window_s:g} s window)."
                    )
                elif "Nyquist" in reason:
                    reason = "Needs a finer time step to see the high-frequency band in this rule."
                elif "2 cycles" in reason:
                    reason = "Needs a longer run to see enough cycles of the lowest frequency in this rule."
                results.append(
                    {
                        "plain": plain,
                        "entity": c.entity,
                        "page": c.page,
                        "passed": None,
                        "used_pct_of_limit": None,
                        "cannot_check_reason": reason,
                    }
                )
                continue

            used = None
            if r.measured is not None and r.limit is not None and r.limit != 0:
                used = round(100.0 * float(r.measured) / float(r.limit), 1)
            passed = bool(r.passed) if r.passed is not None else False
            over = None
            if used is not None and used > 100.0:
                over = round(used - 100.0, 1)
                if broken_plain is None:
                    broken_plain = plain
                    broken_over_pct = over
            results.append(
                {
                    "plain": plain,
                    "entity": c.entity,
                    "page": c.page,
                    "passed": passed,
                    "used_pct_of_limit": used,
                    "status": "pass" if passed else "fail",
                    "measured": r.measured,
                    "limit": r.limit,
                    "constraint_type": c.constraint_type,
                }
            )

        evaluable = [x for x in results if x.get("passed") is not None]
        all_held = bool(evaluable) and all(x.get("passed") for x in evaluable)
        return {
            "passed": all_held,
            "held": all_held,
            "n_rules": len(evaluable),
            "broken_plain": broken_plain,
            "broken_over_pct": broken_over_pct,
            "banner": (
                f"HELD: all {len(evaluable)} rules still met"
                if all_held and evaluable
                else (
                    f"BROKEN: {broken_plain} exceeded by {broken_over_pct:g}%"
                    if broken_plain is not None and broken_over_pct is not None
                    else "No rules could be checked on this attack."
                )
            ),
            "results": results,
            "pulse_rate": 1.0 / period if period > 0 else body.pulse_rate,
            "depth": depth,
            "cluster_mw": cluster,
            "label": "simulated",
        }

    try:
        out = await asyncio.to_thread(run)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    out["elapsed_ms"] = round(elapsed_ms, 1)

    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    (LIVE_DIR / f"{session_id}_{int(time.time() * 1000)}.json").write_text(
        json.dumps({**out, "session_id": session_id, "ts": time.time()}, default=str),
        encoding="utf-8",
    )
    return out


class FixAttackBody(AttackBody):
    """Re-run design with this attack workload added to the training set."""

    budget_s: float = 60.0


@ui_router.post("/session/{session_id}/fix-attack")
async def fix_attack_stream(session_id: str, body: FixAttackBody) -> StreamingResponse:
    """Add the judge's attack as a workload and re-run the local design loop (SSE)."""
    sess = _get_session(session_id)

    async def gen() -> AsyncIterator[str]:
        from evolve.loop import evolve_controller
        from workload.synth import all_preset_workloads

        try:
            ruleset = _verified_ruleset(sess)
        except HTTPException as exc:
            yield _sse("error", {"message": str(exc.detail)})
            yield _sse("done", {"ok": False})
            return

        period = _period_from_attack(body)
        attack_params = _sim_params_for_constraint(
            ruleset.constraints[0],
            cluster_mw=float(body.cluster_mw),
            period_s=period,
            depth=float(body.depth),
            seed=99,
        )
        # Prefer longest needed among all rules for the fix workload
        for c in ruleset.constraints[1:]:
            p = _sim_params_for_constraint(
                c, cluster_mw=float(body.cluster_mw), period_s=period, depth=float(body.depth), seed=99
            )
            if p.duration_s > attack_params.duration_s:
                attack_params = p

        workloads = all_preset_workloads(cluster_mw=sess.rated_mw)
        workloads["judge_attack"] = attack_params

        yield _sse(
            "status",
            {
                "message": "Fixing: redesigning the controller with your attack included…",
                "phase": "fix",
            },
        )
        await asyncio.sleep(0)

        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def on_event(payload: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, payload)

        def run() -> Any:
            return evolve_controller(
                ruleset,
                workloads,
                budget_s=body.budget_s,
                population_size=4,
                seed=202,
                on_event=on_event,
            )

        fut = asyncio.create_task(asyncio.to_thread(run))
        while True:
            if fut.done() and queue.empty():
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                if fut.done():
                    while not queue.empty():
                        yield _sse("evolve", queue.get_nowait())
                    break
                continue
            yield _sse("evolve", item)
            await asyncio.sleep(0)

        try:
            result = await fut
        except Exception as exc:
            yield _sse("error", {"message": str(exc)})
            yield _sse("done", {"ok": False})
            return

        sess.controller_code = result.best_code
        sess.evolve_result = {**result.to_dict(), "fallback_rung": result.outcome}
        if result.best_code:
            try:
                sess.traces = await asyncio.to_thread(_build_traces, sess, result.best_code)
            except Exception as exc:
                sess.traces = {"error": str(exc), "label": "simulated"}
            sess.certificate = _build_certificate(sess)
            _save_replay(sess)

        n_rules = len(ruleset.constraints)
        yield _sse(
            "fix_complete",
            {
                "ok": result.passed,
                "headline": outcome_headline(result.outcome, n_rules, result.total_candidates_passed),
                "outcome": result.outcome,
                "passed": result.passed,
                "best_code": result.best_code,
            },
        )
        yield _sse("done", {"ok": result.passed})

    return _sse_response(gen())


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
