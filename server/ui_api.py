"""GridSafe UI API router (local loop: Reader → Verifier → Evolve → Attack → Certify).

Mount with: `app.include_router(ui_router)` from `server.ui_api`.
Standalone test app: `uvicorn server.ui_dev:app --port 8001`.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable

# Engine packages live under src/ (also support legacy top-level layout).
_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
for _p in (_SRC, _ROOT):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

import numpy as np
from dotenv import load_dotenv
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from repo_paths import find_repo_root

REPO_ROOT = find_repo_root()
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


def _plain_cannot_check(details: str | None) -> str:
    """Translate checker not_evaluable details into plain words for the UI."""
    reason = details or "No measurement returned for this rule."
    low = reason.lower()
    if "nyquist" in low:
        return "This chart is too coarse to check that high-frequency rule. Try Break it for a finer check."
    if "2x rule window" in reason or "2×" in reason or "2x" in low:
        return "This chart is too short to check that slow rule."
    if "2 cycles" in low:
        return "This chart is too short to see enough cycles of that rule."
    if "not_evaluable" in low:
        return "Could not check this rule on this chart."
    # Strip internal ids / jargon tokens if any slipped through
    if "fft" in low or "dt=" in low or "hz at" in low:
        return "Could not check this rule on this chart."
    return reason


def _is_buildable_constraint(raw: dict[str, Any]) -> bool:
    """Confirmed by Verifier, or explicitly confirmed by a person in the UI."""
    if not raw.get("checkable"):
        return False
    if raw.get("human_confirmed") is True:
        return True
    return raw.get("verified") is True and not raw.get("needs_human_review")


def _verified_ruleset(sess: UISession) -> Any:
    from rules.schema import Constraint, RuleSet

    items: list[Constraint] = []
    for raw in sess.constraints:
        if not _is_buildable_constraint(raw):
            continue
        c = Constraint.from_dict(raw)
        # Human-confirmed rules are treated as verified for the checker
        c.verified = True
        c.needs_human_review = False
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
    # Chart workload: keep short/fine enough that frequency-band rules stay checkable.
    # Long ramp windows are covered by the separate 30-minute cost run, not this chart.
    has_band = any(c.constraint_type == "fluctuation_band" for c in ruleset.constraints)
    max_window = max((float(c.window_s or 0.0) for c in ruleset.constraints), default=0.0)
    duration = 20.0
    if max_window > 0 and not has_band:
        duration = min(120.0, max(20.0, 2.0 * max_window))
    dt = 0.004
    if has_band:
        # Need Nyquist above typical 55 Hz band upper edge
        dt = 0.004
        duration = max(duration, 20.0)
    elif duration >= 60.0:
        dt = 0.5
    params = WorkloadParams(
        cluster_mw=sess.rated_mw,
        duration_s=duration,
        dt_s=dt,
        period_s=0.5 if dt < 0.1 else 30.0,
        depth=0.45,
        duty_cycle=0.75,
        seed=42,
        family="slow" if duration >= 60.0 and dt >= 0.1 else "fast",
    )
    trace = generate_workload(params)
    ctrl = _load_controller(code)

    before = simulate_plant(trace, controller=noop_controller)
    after = simulate_plant(trace, controller=ctrl)
    report_before = check_compliance(before, ruleset, strict_review=False)
    report = check_compliance(after, ruleset, strict_review=False)

    def _used_pct(measured: Any, limit: Any) -> float | None:
        if isinstance(measured, (int, float)) and isinstance(limit, (int, float)) and float(limit) != 0.0:
            return round(100.0 * float(measured) / float(limit), 1)
        return None

    rule_bars: list[dict[str, Any]] = []
    by_id_before = {r.constraint_id: r for r in report_before.results}
    by_id_after = {r.constraint_id: r for r in report.results}
    for c in ruleset.constraints:
        rb = by_id_before.get(c.id)
        ra = by_id_after.get(c.id)
        used_off = _used_pct(rb.measured, rb.limit) if rb is not None else None
        used_on = _used_pct(ra.measured, ra.limit) if ra is not None else None
        rule_bars.append(
            {
                "id": c.id,
                "plain": plain_rule_sentence(c),
                "entity": c.entity,
                "page": c.page,
                "constraint_type": c.constraint_type,
                "measured_off": float(rb.measured) if rb is not None and rb.measured is not None else None,
                "measured_on": float(ra.measured) if ra is not None and ra.measured is not None else None,
                "limit": float(ra.limit) if ra is not None and ra.limit is not None else (
                    float(rb.limit) if rb is not None and rb.limit is not None else None
                ),
                "used_pct_off": used_off,
                "used_pct_on": used_on,
                "passed_off": bool(rb.passed) if rb is not None and rb.passed is not None else None,
                "passed_on": bool(ra.passed) if ra is not None and ra.passed is not None else None,
                "cannot_check_reason": (
                    None
                    if (ra is not None and ra.status != "not_evaluable")
                    else _plain_cannot_check(ra.details if ra is not None else None)
                ),
            }
        )

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

    # --- Cost: cold-start (20 s) vs 30-minute slow-family run ---
    cold_params = WorkloadParams(
        cluster_mw=sess.rated_mw,
        duration_s=20.0,
        dt_s=0.004,
        period_s=0.5,
        depth=0.45,
        duty_cycle=0.75,
        seed=42,
        family="fast",
    )
    cold_trace = generate_workload(cold_params)
    cold_after = simulate_plant(cold_trace, controller=ctrl)
    cold_start = {
        "duration_s": 20.0,
        "label": "First 20 seconds after a cold start",
        "extra_energy_pct": float(cold_after.metrics.energy_overhead_pct),
        "training_slowed_pct": float(cold_after.metrics.throughput_loss_pct),
        "battery_wear": float(cold_after.metrics.battery_wear_proxy),
    }

    long_ok = False
    long_metrics: dict[str, Any] = {
        "duration_s": 1800.0,
        "label": "Over a 30-minute run",
        "family": "slow",
        "extra_energy_pct": None,
        "training_slowed_pct": None,
        "battery_wear": None,
        "error": None,
    }
    try:
        from workload.synth import slow_preset_workloads

        slow_p = next(iter(slow_preset_workloads(cluster_mw=sess.rated_mw).values()))
        long_trace = generate_workload(slow_p)
        long_after = simulate_plant(long_trace, controller=ctrl)
        long_metrics = {
            "duration_s": float(slow_p.duration_s),
            "label": "Over a 30-minute run",
            "family": "slow",
            "workload": slow_p.name if hasattr(slow_p, "name") else "slow_training_run",
            "extra_energy_pct": float(long_after.metrics.energy_overhead_pct),
            "training_slowed_pct": float(long_after.metrics.throughput_loss_pct),
            "battery_wear": float(long_after.metrics.battery_wear_proxy),
            "error": None,
        }
        long_ok = True
    except Exception as exc:
        long_metrics["error"] = str(exc)

    # Primary cost numbers: prefer 30-min; fall back to the chart workload span
    if long_ok:
        primary_extra = long_metrics["extra_energy_pct"]
        primary_slowed = long_metrics["training_slowed_pct"]
        primary_wear = long_metrics["battery_wear"]
        primary_span_s = float(long_metrics["duration_s"])
        primary_label = "Over a 30-minute run"
    else:
        primary_extra = float(after.metrics.energy_overhead_pct)
        primary_slowed = float(after.metrics.throughput_loss_pct)
        primary_wear = float(after.metrics.battery_wear_proxy)
        primary_span_s = float(duration)
        primary_label = f"Over a {primary_span_s:g}-second run"

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
            "extra_energy_pct": primary_extra,
            "training_slowed_pct": primary_slowed,
            "battery_wear": primary_wear,
            "duration_s": primary_span_s,
            "span_label": primary_label,
            "from_30min": long_ok,
            "lower_is_better": True,
            "cold_start": cold_start,
            "run_30min": long_metrics,
            "before": before.metrics.to_dict(),
            "after": after.metrics.to_dict(),
        },
        "rule_bars": rule_bars,
        "n_rules": len(rule_bars),
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
    append: bool = False


class RemoveConstraintBody(BaseModel):
    constraint_id: str


class ConfirmConstraintBody(BaseModel):
    constraint_id: str


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


def outcome_verdict(outcome: str, n_rules: int) -> str:
    """Large Controller-screen verdict. No jargon."""
    if outcome in {"evolved_compliant", "fallback_b3", "fallback_conservative_floor"}:
        return f"Follows all {n_rules} rules"
    if outcome == "refused":
        return "GridSafe refused: no controller met the rules."
    return "Controller run finished."


def outcome_winner_sentence(outcome: str, gemini_passed: int | None = None) -> str:
    if outcome == "evolved_compliant":
        return "Winner: a Gemini-written controller."
    if outcome == "fallback_b3":
        n = gemini_passed if gemini_passed is not None else 0
        return (
            f"Winner: the tuned filter baseline (no AI). {n} Gemini designs followed the rules, "
            "but none was cheaper."
        )
    if outcome == "fallback_conservative_floor":
        return "Fell back to the safe, expensive setting."
    if outcome == "refused":
        return "No safe controller was found."
    return ""


def controller_author_line(outcome: str) -> str:
    if outcome == "evolved_compliant":
        return "Written by Gemini 4 Hydrogen, checked by GridSafe"
    if outcome == "fallback_b3":
        return "Tuned filter baseline (no AI), checked by GridSafe"
    if outcome == "fallback_conservative_floor":
        return "Safe expensive fallback (no AI), checked by GridSafe"
    return "Checked by GridSafe"


def outcome_headline(outcome: str, n_rules: int, gemini_passed: int | None = None) -> str:
    verdict = outcome_verdict(outcome, n_rules)
    winner = outcome_winner_sentence(outcome, gemini_passed)
    if outcome == "refused":
        return verdict
    if winner:
        return f"{verdict}. {winner}" if not winner.startswith("Fell") else f"{verdict}. {winner}"
    return verdict


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
        elif sess.pdf_bytes is not None and page <= (
            # original PDF page count may be less than page_count after add-image
            sess.page_count
        ):
            try:
                png = render_pdf_page_png(sess.pdf_bytes, page, dpi=dpi)
                cache.write_bytes(png)
            except Exception:
                if sess.pdf_path and sess.pdf_path.exists():
                    cache.write_bytes(sess.pdf_path.read_bytes())
                else:
                    raise HTTPException(status_code=404, detail="No page image") from None
        elif sess.pdf_path and sess.pdf_path.exists():
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

        reader_label = _model_display_name("gemini-4-neon")
        verifier_label = _model_display_name("gemini-decision-1")
        yield _sse(
            "status",
            {
                "phase": "read",
                "message": f"{reader_label} is preparing to read {len(pages)} page(s)",
                "pages": pages,
                "model": reader_label,
            },
        )
        await asyncio.sleep(0)

        all_constraints: list[Constraint] = []
        page_images: dict[int, bytes] = {}
        camera_pages: set[int] = set()
        reader_model = ""

        try:
            for pg in pages:
                yield _sse(
                    "status",
                    {
                        "phase": "read",
                        "message": f"{reader_label} is reading page {pg}",
                        "page": pg,
                        "model": reader_label,
                    },
                )
                yield _sse("page_read_start", {"page": pg})
                await asyncio.sleep(0)
                # Prefer stored images (camera / appended photo); else render from PDF
                stored = sess.page_images.get(pg)
                from_camera = stored is not None or sess.source_kind == "camera"
                mime = "image/jpeg" if from_camera else "image/png"
                if stored is not None:
                    img = stored
                elif sess.pdf_path and sess.source_kind == "camera":
                    img = sess.pdf_path.read_bytes()
                    from_camera = True
                    mime = "image/jpeg"
                elif sess.pdf_bytes is not None:
                    img = await asyncio.to_thread(render_pdf_page_png, sess.pdf_bytes, pg, 150)
                    mime = "image/png"
                    from_camera = False
                else:
                    yield _sse("error", {"message": f"Missing image for page {pg}"})
                    return
                page_images[pg] = img
                if from_camera:
                    camera_pages.add(pg)

                out = await asyncio.to_thread(
                    extract_from_image_bytes,
                    img,
                    mime,
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
                    d0 = c.to_dict()
                    if from_camera:
                        d0["source_label"] = "Added by camera"
                    all_constraints.append(c)
                    yield _sse("constraint", {**d0, "ui_status": "pending", "phase": "read"})
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
            yield _sse(
                "status",
                {
                    "phase": "verify",
                    "message": f"{verifier_label} is checking each rule against the page",
                    "model": verifier_label,
                },
            )
            report = await asyncio.to_thread(
                verify_constraints,
                all_constraints,
                page_images=page_images,
                mime_type="image/png",
            )
            sess.verifier_model = report.verifier_model
            sess.verification = report.to_dict()
            if report.verifier_model:
                verifier_label = _model_display_name(report.verifier_model)
                yield _sse(
                    "status",
                    {
                        "phase": "verify",
                        "message": f"{verifier_label} is checking each rule against the page",
                        "model": verifier_label,
                    },
                )

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

            append = bool(body.append) if body is not None else False
            for d in out_constraints:
                entity = str(d.get("entity") or "").strip()
                page = d.get("page")
                if page in camera_pages or sess.source_kind == "camera":
                    d["source_label"] = "Added by camera"
                elif entity and page is not None:
                    d["source_label"] = f"{entity}, page {page}"
                elif page is not None:
                    d["source_label"] = f"page {page}"
                else:
                    d["source_label"] = sess.source_name or "rulebook"

            if append and sess.constraints:
                by_id = {str(c.get("id")): c for c in sess.constraints if c.get("id")}
                for d in out_constraints:
                    by_id[str(d.get("id"))] = d
                sess.constraints = list(by_id.values())
            else:
                sess.constraints = out_constraints
            # Changing rules invalidates a prior controller
            if append:
                sess.controller_code = None
                sess.evolve_result = None
                sess.traces = None
                sess.certificate = None
            yield _sse(
                "verify_complete",
                {
                    "verification": report.to_dict(),
                    "constraints": sess.constraints,
                    "reader_model": reader_model,
                    "verifier_model": report.verifier_model,
                    "append": append,
                },
            )
            yield _sse("done", {"ok": True})
        except Exception as exc:
            yield _sse("error", {"message": str(exc), "type": type(exc).__name__})
            yield _sse("done", {"ok": False})

    return _sse_response(gen())


@ui_router.post("/session/{session_id}/add-image")
async def add_image_page(
    session_id: str,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """Append a camera photo (or page image) as a new page on an existing session."""
    sess = _get_session(session_id)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty image")
    page = int(sess.page_count) + 1
    sess.page_images[page] = data
    sess.page_count = page
    sess.selected_pages = [page]
    # Mark so Reader tags source_label as camera-added
    if sess.source_kind != "camera":
        sess.source_kind = "mixed"
    mime = file.content_type or "image/jpeg"
    ext = ".jpg" if "jpeg" in mime or "jpg" in mime else ".png"
    dest = UPLOAD_DIR / f"{session_id}_added_p{page}{ext}"
    dest.write_bytes(data)
    return {
        "session_id": session_id,
        "page": page,
        "page_count": sess.page_count,
        "source_label": "Added by camera",
        "thumbnail_url": f"/api/ui/session/{session_id}/thumbnail/{page}",
    }


@ui_router.post("/session/{session_id}/remove-constraint")
async def remove_constraint(session_id: str, body: RemoveConstraintBody) -> dict[str, Any]:
    sess = _get_session(session_id)
    before = len(sess.constraints)
    cid = str(body.constraint_id)
    sess.constraints = [c for c in sess.constraints if str(c.get("id")) != cid]
    removed = before - len(sess.constraints)
    if removed:
        sess.controller_code = None
        sess.evolve_result = None
        sess.traces = None
        sess.certificate = None
    return {
        "session_id": session_id,
        "removed": removed,
        "constraints": sess.constraints,
        "n_rules": len(sess.constraints),
    }


@ui_router.post("/session/{session_id}/confirm-constraint")
async def confirm_constraint(session_id: str, body: ConfirmConstraintBody) -> dict[str, Any]:
    """Person confirms a Verifier-unsure rule so the build can use it."""
    sess = _get_session(session_id)
    cid = str(body.constraint_id)
    found = False
    for c in sess.constraints:
        if str(c.get("id")) != cid:
            continue
        found = True
        c["human_confirmed"] = True
        c["verified"] = True
        c["needs_human_review"] = False
        c["ui_status"] = "Confirmed by a person, not by the Verifier"
        c["confirm_badge"] = "Confirmed by a person, not by the Verifier"
        break
    if not found:
        raise HTTPException(status_code=404, detail=f"No rule {cid}")
    # Controller must be rebuilt against the updated list
    sess.controller_code = None
    sess.evolve_result = None
    sess.traces = None
    sess.certificate = None
    return {
        "session_id": session_id,
        "constraint_id": cid,
        "constraints": sess.constraints,
        "message": "Confirmed by a person, not by the Verifier",
    }


@ui_router.get("/docs/results")
async def docs_results() -> dict[str, Any]:
    """Read RESULTS.md and FAILURES.md fresh from the repo. Never invent content."""
    results_path = REPO_ROOT / "RESULTS.md"
    failures_path = REPO_ROOT / "FAILURES.md"

    def _read(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {
                "exists": False,
                "path": str(path.relative_to(REPO_ROOT)),
                "markdown": None,
                "message": "not generated yet",
            }
        text = path.read_text(encoding="utf-8")
        return {
            "exists": True,
            "path": str(path.relative_to(REPO_ROOT)),
            "markdown": text,
            "bytes": len(text.encode("utf-8")),
            "mtime_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(path.stat().st_mtime)),
            "message": None,
        }

    return {
        "results": _read(results_path),
        "failures": _read(failures_path),
        "label": "read from repo files on each request",
    }


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

        designer_label = _model_display_name("gemini-4-hydrogen")
        yield _sse(
            "status",
            {
                "phase": "evolve",
                "message": f"{designer_label} is writing {opts.population_size} controllers",
                "n_constraints": len(ruleset.constraints),
                "n_workloads": len(workloads),
                "budget_s": opts.budget_s,
                "model": designer_label,
                "population_size": opts.population_size,
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
                "verdict": outcome_verdict(result.outcome, n_rules),
                "winner": outcome_winner_sentence(result.outcome, result.total_candidates_passed),
                "author_line": controller_author_line(result.outcome),
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

    n_rules = sum(1 for c in sess.constraints if _is_buildable_constraint(c))
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
        human = c.get("human_confirmed") is True
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
                "human_confirmed": human,
                "confirm_label": (
                    "Confirmed by a person, not by the Verifier" if human else None
                ),
            }
        )

    outcome = str(ev.get("outcome") or "")
    return {
        "outcome": outcome,
        "passed": ev.get("passed"),
        "headline": outcome_headline(outcome, n_rules or len(rules), gemini_passed),
        "verdict": outcome_verdict(outcome, n_rules or len(rules)),
        "winner": outcome_winner_sentence(outcome, gemini_passed),
        "author_line": controller_author_line(outcome),
        "n_rules": n_rules or len(rules),
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

        designer_label = _model_display_name("gemini-4-hydrogen")
        yield _sse(
            "status",
            {
                "message": f"{designer_label} is writing controllers with your attack included",
                "phase": "fix",
                "model": designer_label,
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
                "verdict": outcome_verdict(result.outcome, n_rules),
                "winner": outcome_winner_sentence(result.outcome, result.total_candidates_passed),
                "author_line": controller_author_line(result.outcome),
                "outcome": result.outcome,
                "passed": result.passed,
                "best_code": result.best_code,
            },
        )
        yield _sse("done", {"ok": result.passed})

    return _sse_response(gen())


def _model_display_name(model_id: str) -> str:
    mid = (model_id or "").strip().lower()
    if "decision" in mid:
        return "Gemini Decision 1"
    if "neon" in mid:
        return "Gemini 4 Neon"
    if "hydrogen" in mid:
        return "Gemini 4 Hydrogen"
    if "carbon" in mid:
        return "Gemini 4 Carbon"
    if mid:
        return model_id.replace("gemini-", "Gemini ").replace("-", " ").title()
    return "Gemini"


def _plain_fix_reject_sentence(reason: str | None, *, worse_on_n: int = 0) -> str | None:
    """Map a real gate/check reason to a plain sentence. None if reason unknown."""
    r = (reason or "").strip().lower()
    if r in {"counterexample_still_breaks", "cx_still_breaks", "counterexamples_still_break"}:
        return (
            "The agent proposed a fix, but GridSafe's check showed it did not stop the break. "
            "The fix was rejected and the issue is logged as open."
        )
    if r in {"validation_regression", "made_worse", "worse_on_validation"} or (
        r.startswith("validation") and "regress" in r
    ):
        n = max(0, int(worse_on_n or 0))
        return f"The fix was rejected because it made {n} other workloads worse."
    if r in {"presets_failed", "preset_failed", "failed_preset"}:
        return "The fix was rejected because it broke a rule on the standard tests."
    return None


def _plain_agent_verdict(
    *,
    break_found: bool | None,
    fix_accepted: bool | None,
    fix_rejected: bool | None,
    reject_reason: str | None = None,
    worse_on_n: int = 0,
) -> str | None:
    """Plain verdict from thread data only — never invent a rejection reason."""
    if break_found is True and fix_accepted is True:
        return "Break found and fixed."
    if break_found is True and (fix_rejected is True or fix_accepted is False):
        specific = _plain_fix_reject_sentence(reject_reason, worse_on_n=worse_on_n)
        if specific:
            return specific
        return "Break found and the fix was rejected."
    if break_found is False:
        return "No break found."
    return None


def _parse_agent_thread_file(path: Path) -> dict[str, Any] | None:
    """Turn a thread json/jsonl (+ optional summary/meta) into a UI-facing conversation."""
    if ".meta." in path.name or path.name.endswith("_summary.json"):
        return None
    stem = path.name
    if stem.endswith(".jsonl"):
        thread_id = stem[: -len(".jsonl")]
    elif stem.endswith(".json"):
        thread_id = stem[: -len(".json")]
    else:
        return None
    if thread_id.endswith("_thread"):
        # keep id as-is for meta lookup variants
        pass

    meta: dict[str, Any] = {}
    for meta_name in (f"{thread_id}.meta.json", f"{thread_id}_thread.meta.json"):
        mp = AGENT_THREADS_DIR / meta_name
        if mp.exists():
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
            break

    summary: dict[str, Any] = {}
    for sum_name in (f"{thread_id}_summary.json", f"{thread_id.replace('_thread', '')}_summary.json"):
        sp = AGENT_THREADS_DIR / sum_name
        if sp.exists():
            try:
                summary = json.loads(sp.read_text(encoding="utf-8"))
            except Exception:
                summary = {}
            break

    events: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".jsonl":
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if isinstance(row, dict):
                    events.append(row)
        else:
            data = json.loads(text)
            if isinstance(data, dict) and isinstance(data.get("events"), list):
                events = [e for e in data["events"] if isinstance(e, dict)]
            elif isinstance(data, list):
                events = [e for e in data if isinstance(e, dict)]
            else:
                return None
    except Exception:
        return None

    messages: list[dict[str, Any]] = []
    break_found: bool | None = None
    fix_accepted: bool | None = None
    fix_rejected: bool | None = None
    reject_reason: str | None = None
    worse_on_n = 0
    recorded_at: str | None = None

    def _who(sender: str) -> str:
        s = (sender or "").lower()
        if "engineer" in s:
            return "Engineer agent (Gemini 4 Hydrogen)"
        if "hardener" in s:
            return "Hardener agent (Gemini 4 Carbon)"
        if "gridsafe" in s or sender == "backend":
            return "GridSafe"
        return sender or "Agent"

    def _push(who: str, summary_text: str, payload: Any = None, stamp: str | None = None) -> None:
        messages.append(
            {
                "who": who,
                "summary": summary_text,
                "payload": payload,
                "stamp": stamp,
            }
        )

    # Summary-first signals (lipa_f11 style)
    if summary:
        if summary.get("break_found") is True:
            break_found = True
        if summary.get("break_found") is False:
            break_found = False
        if summary.get("backend_accepted_break") is True:
            break_found = True
        if summary.get("fix_passed") is True and summary.get("backend_cx_still_break") is False:
            fix_accepted = True
        if summary.get("completed_at_utc"):
            recorded_at = str(summary["completed_at_utc"])

    for e in events:
        ev = str(e.get("event") or "")
        ts = e.get("ts_utc") or e.get("created_at_utc")
        if isinstance(ts, str) and ts:
            recorded_at = recorded_at or ts

        if ev == "heartbeat":
            continue

        if ev in {"message_out", "agent_message"} and (e.get("from") or e.get("sender")):
            sender = str(e.get("from") or e.get("sender") or "")
            payload = e.get("payload") if isinstance(e.get("payload"), dict) else None
            typ = ""
            if payload:
                typ = str(payload.get("type") or payload.get("status") or "")
            if typ == "submit_controller" or (ev == "message_out" and "engineer" in sender.lower()):
                nbytes = None
                if payload:
                    nbytes = payload.get("controller_bytes")
                _push(
                    _who(sender),
                    f"Engineer submitted a controller for attack"
                    + (f" ({nbytes} bytes)" if nbytes else ""),
                    payload or {"type": "submit_controller"},
                )
            elif payload and e.get("summary"):
                _push(_who(sender), str(e.get("summary")), payload)
            continue

        if ev == "message_in":
            sender = str(e.get("from") or "gridsafe-hardener")
            reply = e.get("reply") if isinstance(e.get("reply"), dict) else {}
            status = str(reply.get("status") or "")
            check = e.get("backend_counterexample_check") if isinstance(e.get("backend_counterexample_check"), dict) else {}
            if status == "break_found" or (check and check.get("break_found")):
                break_found = True
                n = len(reply.get("counterexamples") or check.get("verified_breaks") or [])
                _push(
                    _who(sender),
                    f"Hardener reported a break with {n or 1} counterexample(s)",
                    reply or check,
                )
            elif status in {"no_break_within_budget", "no_break", "no_break_found"}:
                break_found = False
                _push(_who(sender), "Hardener found no break within budget", reply)
            elif status == "ack":
                _push(_who(sender), "Hardener acknowledged the fix", reply)
            elif e.get("ok") and reply:
                _push(_who(sender), f"Hardener replied ({status or 'message'})", reply)
            continue

        if ev == "hardener_reply":
            reply = e.get("reply") if isinstance(e.get("reply"), dict) else {}
            if reply.get("status") == "break_found" or reply.get("counterexamples"):
                break_found = True
                n = len(reply.get("counterexamples") or [])
                _push(_who("gridsafe-hardener"), f"Hardener reported a break with {n} counterexample(s)", reply)
            continue

        if ev in {"backend_counterexample_check", "backend_check"}:
            check = e.get("check") if isinstance(e.get("check"), dict) else e
            bf = check.get("break_found") if isinstance(check, dict) else None
            if bf is True:
                break_found = True
                stamp = "confirmed"
                _push("GridSafe", "Backend accepted the claimed break.", check, stamp)
            elif bf is False:
                _push("GridSafe", "Backend rejected the claimed breaks.", check, "rejected")
            continue

        if ev == "host_rand20_probe":
            passed = e.get("passed")
            _push(
                "GridSafe",
                (
                    "Backend Rand20 probe: controller still passes all 20 workloads."
                    if passed
                    else "Backend Rand20 probe: found at least one failing workload."
                ),
                e,
                "confirmed" if passed else "rejected",
            )
            if passed is False:
                break_found = True
            elif passed is True and break_found is None:
                break_found = False
            continue

        if ev == "engineer_fix":
            _push(
                _who("gridsafe-engineer"),
                f"Engineer produced a fix candidate (outcome={e.get('outcome')}, passed={e.get('passed')})",
                e,
            )
            continue

        if ev in {"backend_confirm", "backend_confirm_final"}:
            cx_break = e.get("counterexamples_still_break")
            if cx_break is None:
                cx_break = e.get("cx_still_break")
            preset_ok = e.get("preset_passed")
            ok = bool(preset_ok) and bool(e.get("rand20_passed")) and not bool(cx_break)
            if ok:
                fix_accepted = True
                break_found = True if break_found is None else break_found
                _push("GridSafe", "Backend confirms the fix: counterexamples clear, presets pass.", e, "confirmed")
            else:
                fix_rejected = True
                break_found = True
                if bool(cx_break):
                    reject_reason = reject_reason or "counterexample_still_breaks"
                elif preset_ok is False:
                    reject_reason = reject_reason or "presets_failed"
                plain_rej = _plain_fix_reject_sentence(reject_reason, worse_on_n=worse_on_n)
                _push(
                    "GridSafe",
                    plain_rej or "Backend could not fully confirm the fix.",
                    e,
                    "rejected",
                )
            continue

        if ev == "fix_gate":
            accepted = bool(e.get("accepted"))
            reason = str(e.get("reason") or "")
            worse_n = int(e.get("worse_on_n") or 0)
            checks = e.get("checks") if isinstance(e.get("checks"), dict) else {}
            if worse_n > 0:
                worse_on_n = worse_n
            if accepted:
                fix_accepted = True
                break_found = True
                _push("GridSafe", str(e.get("message") or "Fix accepted by backend gate."), e, "confirmed")
            else:
                fix_rejected = True
                break_found = True
                if reason:
                    reject_reason = reason
                elif checks.get("counterexample_still_breaks") is True:
                    reject_reason = "counterexample_still_breaks"
                elif isinstance(checks.get("presets"), dict) and checks["presets"].get("passed") is False:
                    reject_reason = "presets_failed"
                elif worse_n > 0 or (
                    isinstance(checks.get("validation"), dict)
                    and checks["validation"].get("passed") is False
                ):
                    reject_reason = "validation_regression"
                    if worse_n > 0:
                        worse_on_n = worse_n
                plain_rej = _plain_fix_reject_sentence(reject_reason, worse_on_n=worse_on_n)
                _push(
                    "GridSafe",
                    plain_rej or str(e.get("message") or "Fix rejected by backend gate."),
                    e,
                    "rejected",
                )
            continue

        if ev == "verdict":
            v = str(e.get("verdict") or "")
            if v == "break_found_and_fixed":
                break_found, fix_accepted = True, True
            elif v in {"break_found_not_fixed", "break_found_fix_rejected"}:
                break_found, fix_rejected = True, True
                gate = e.get("gate") if isinstance(e.get("gate"), dict) else {}
                if isinstance(gate, dict) and gate.get("reason"):
                    reject_reason = str(gate["reason"])
                    if gate.get("worse_on_n") is not None:
                        worse_on_n = int(gate.get("worse_on_n") or 0)
            elif v in {"no_break_within_budget", "no_break"}:
                break_found = False
            if e.get("message"):
                _push("GridSafe", str(e["message"]), e)

    # Meta last_result (minimal_exchange style)
    last = meta.get("last_result") if isinstance(meta.get("last_result"), dict) else {}
    hard_reply = last.get("hardener_reply") if isinstance(last.get("hardener_reply"), dict) else {}
    parsed = hard_reply.get("parsed") if isinstance(hard_reply.get("parsed"), dict) else {}
    if parsed.get("status") == "break_found" or parsed.get("break_found") is True:
        break_found = True if break_found is None else break_found
    if isinstance(hard_reply.get("raw_text"), str) and "break_found" in hard_reply["raw_text"]:
        break_found = True if break_found is None else break_found

    if not recorded_at:
        created = meta.get("created_at_utc")
        if isinstance(created, str):
            recorded_at = created
        else:
            recorded_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(path.stat().st_mtime))

    # Summary may also encode CX-still-breaks without a fix_gate event
    if (
        reject_reason is None
        and fix_rejected
        and summary.get("backend_cx_still_break") is True
        and summary.get("fix_passed") is not True
    ):
        reject_reason = "counterexample_still_breaks"

    plain = _plain_agent_verdict(
        break_found=break_found,
        fix_accepted=fix_accepted,
        fix_rejected=fix_rejected,
        reject_reason=reject_reason,
        worse_on_n=worse_on_n,
    )
    # Hardener-only completion (UI rounds cut before host verdict): still a verdict
    if plain is None and break_found is False and any(
        str((m.get("summary") or "")).lower().find("no break") >= 0 for m in messages
    ):
        plain = "No break found."
    if plain is None:
        return None
    if not messages:
        # Build a minimal conversation so the panel is never empty for a real verdict
        if break_found and fix_accepted:
            messages = [
                {
                    "who": "Hardener agent (Gemini 4 Carbon)",
                    "summary": "Hardener reported a break",
                    "payload": None,
                    "stamp": None,
                },
                {
                    "who": "GridSafe",
                    "summary": "Break found and fixed.",
                    "payload": None,
                    "stamp": "confirmed",
                },
            ]
        elif break_found:
            messages = [
                {
                    "who": "Hardener agent (Gemini 4 Carbon)",
                    "summary": "Hardener reported a break",
                    "payload": None,
                    "stamp": None,
                },
                {
                    "who": "GridSafe",
                    "summary": plain,
                    "payload": None,
                    "stamp": "rejected",
                },
            ]
        else:
            messages = [
                {
                    "who": "Hardener agent (Gemini 4 Carbon)",
                    "summary": "Hardener found no break within budget",
                    "payload": None,
                    "stamp": None,
                },
                {
                    "who": "GridSafe",
                    "summary": "No break found.",
                    "payload": None,
                    "stamp": "confirmed",
                },
            ]

    return {
        "thread_id": meta.get("thread_id") or thread_id,
        "file": path.name,
        "kind": meta.get("kind"),
        "recorded_at_utc": recorded_at,
        "break_found": bool(break_found),
        "plain_verdict": plain,
        "messages": messages,
        "mtime": path.stat().st_mtime,
    }


@ui_router.get("/agent-threads/featured")
async def featured_agent_thread() -> dict[str, Any]:
    """Most recent completed agent conversation that reached a verdict.

    Prefers a thread where a break was found. Used for the Break-it Agents panel demo.
    """
    if not AGENT_THREADS_DIR.exists():
        return {"featured": None, "message": "No agent threads yet"}

    candidates: list[dict[str, Any]] = []
    files = [
        *AGENT_THREADS_DIR.glob("*.jsonl"),
        *AGENT_THREADS_DIR.glob("*.json"),
    ]
    seen: set[str] = set()
    for p in files:
        if ".meta." in p.name or p.name.endswith("_summary.json"):
            continue
        # Prefer the jsonl when both exist
        key = p.name.rsplit(".", 1)[0]
        if key in seen:
            continue
        parsed = _parse_agent_thread_file(p)
        if parsed is None:
            continue
        seen.add(key)
        candidates.append(parsed)

    if not candidates:
        return {"featured": None, "message": "No completed agent conversations with a verdict yet"}

    # Prefer break_found, then most recent
    candidates.sort(key=lambda c: (1 if c.get("break_found") else 0, float(c.get("mtime") or 0)), reverse=True)
    best = candidates[0]
    best.pop("mtime", None)
    return {"featured": best, "n_candidates": len(candidates)}


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
    # Rebuild traces if missing/broken/stale and we have a controller
    bars = (sess.traces or {}).get("rule_bars") if isinstance(sess.traces, dict) else None
    stale_jargon = False
    if isinstance(bars, list):
        for b in bars:
            reason = str((b or {}).get("cannot_check_reason") or "")
            if "Nyquist" in reason or "not_evaluable" in reason.lower() or "dt=" in reason:
                stale_jargon = True
                break
    needs_traces = sess.controller_code and (
        sess.traces is None
        or "error" in (sess.traces or {})
        or "rule_bars" not in (sess.traces or {})
        or "cold_start" not in ((sess.traces or {}).get("metrics") or {})
        or stale_jargon
    )
    if needs_traces and sess.controller_code:
        try:
            sess.traces = await asyncio.to_thread(_build_traces, sess, sess.controller_code)
            # Refresh certificate with verdict/winner/author_line fields
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
