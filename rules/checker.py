"""Deterministic interconnection compliance checker.

Pure Python + numpy (no scipy, no AI). Evaluates normalized checkable constraints
against a simulated or raw net grid power trace and IEEE FBM shaft response,
returning exact pass/fail verdicts and margins.
"""

from __future__ import annotations

from collections import deque
from typing import Sequence

import numpy as np

from rules.schema import (
    ComplianceReport,
    Constraint,
    ConstraintType,
    RuleCheckResult,
    RuleSet,
)
from rules.units import UnitConversionError, normalize_constraint_limit
from sim.plant import SimulationResult
from sim.shaft import ShaftResponse

EXACT_LIMIT_REL_TOL = 1e-9


def _sliding_window_range_max(arr: np.ndarray, window_steps: int) -> float:
    """Exact O(N) maximum of (max - min) over every contiguous window of length window_steps + 1."""
    n = len(arr)
    if n < 2:
        return 0.0
    k = min(max(1, int(window_steps)), n - 1)
    # Window spans [i - k, i], i.e., width k + 1 samples
    width = k + 1
    dq_max: deque[int] = deque()
    dq_min: deque[int] = deque()
    best = 0.0

    vals = arr.tolist()
    for i, v in enumerate(vals):
        while dq_max and vals[dq_max[-1]] <= v:
            dq_max.pop()
        dq_max.append(i)
        if dq_max[0] <= i - width:
            dq_max.popleft()

        while dq_min and vals[dq_min[-1]] >= v:
            dq_min.pop()
        dq_min.append(i)
        if dq_min[0] <= i - width:
            dq_min.popleft()

        if i >= k:
            span = vals[dq_max[0]] - vals[dq_min[0]]
            if span > best:
                best = span
    return float(best)


def _measure_ramp_rate(p_grid_mw: np.ndarray, dt_s: float, window_s: float | None) -> float:
    """Maximum ramp rate in MW/s over dt_s or an explicit rolling window_s."""
    n = len(p_grid_mw)
    if n < 2:
        return 0.0
    if window_s is None or window_s <= dt_s + 1e-12:
        diffs = np.abs(np.diff(p_grid_mw))
        return float(np.max(diffs) / dt_s)

    k = max(1, int(round(window_s / dt_s)))
    if k >= n:
        return float(abs(p_grid_mw[-1] - p_grid_mw[0]) / ((n - 1) * dt_s))
    diffs = np.abs(p_grid_mw[k:] - p_grid_mw[:-k])
    return float(np.max(diffs) / (k * dt_s))


def _measure_max_step(p_grid_mw: np.ndarray, dt_s: float, window_s: float | None) -> float:
    """Maximum step change in MW (single step when window_s is None, else max window swing)."""
    n = len(p_grid_mw)
    if n < 2:
        return 0.0
    if window_s is None or window_s <= dt_s + 1e-12:
        return float(np.max(np.abs(np.diff(p_grid_mw))))
    k = max(1, int(round(window_s / dt_s)))
    return _sliding_window_range_max(p_grid_mw, k)


def _measure_dynamic_range(p_grid_mw: np.ndarray, dt_s: float, window_s: float | None) -> float:
    """Maximum peak-to-peak power swing in MW (full trace or sliding window)."""
    n = len(p_grid_mw)
    if n < 2:
        return 0.0
    if window_s is None:
        return float(np.max(p_grid_mw) - np.min(p_grid_mw))
    k = max(1, int(round(window_s / dt_s)))
    if k >= n - 1:
        return float(np.max(p_grid_mw) - np.min(p_grid_mw))
    return _sliding_window_range_max(p_grid_mw, k)


def _measure_fluctuation_band_mw(
    p_grid_mw: np.ndarray,
    dt_s: float,
    freq_min_hz: float | None,
    freq_max_hz: float | None,
) -> tuple[float, float]:
    """One-sided FFT peak amplitude (MW) within [freq_min_hz, freq_max_hz] (excluding DC)."""
    n = len(p_grid_mw)
    if n < 2:
        return 0.0, 0.0
    dp = p_grid_mw - float(np.mean(p_grid_mw))
    freqs = np.fft.rfftfreq(n, d=dt_s)
    fft_vals = np.fft.rfft(dp)
    scale = np.full(len(freqs), 2.0 / n, dtype=np.float64)
    scale[0] = 1.0 / n
    if n % 2 == 0:
        scale[-1] = 1.0 / n
    amps = np.abs(fft_vals) * scale

    f_lo = 0.0 if freq_min_hz is None else float(freq_min_hz)
    f_hi = float("inf") if freq_max_hz is None else float(freq_max_hz)

    # Always exclude DC (freq == 0) unless explicitly requested
    mask = (freqs > 0.0) & (freqs >= f_lo - 1e-12) & (freqs <= f_hi + 1e-12)
    if not np.any(mask):
        return 0.0, 0.0

    band_amps = amps[mask]
    band_freqs = freqs[mask]
    idx = int(np.argmax(band_amps))
    return float(band_amps[idx]), float(band_freqs[idx])


def _evaluate_single_constraint(
    constraint: Constraint,
    p_grid_mw: np.ndarray,
    dt_s: float,
    rated_mw: float,
    shaft: ShaftResponse | None,
    strict_review: bool,
) -> RuleCheckResult:
    if constraint.needs_human_review and strict_review:
        return RuleCheckResult(
            constraint_id=constraint.id,
            constraint_type=constraint.constraint_type,
            status="needs_review",
            passed=False,
            measured=None,
            limit=None,
            margin=None,
            margin_pct=None,
            canonical_unit="",
            page=constraint.page,
            snippet=constraint.snippet,
            confidence=constraint.confidence,
            details="Flagged for human review; cannot certify automatically.",
        )

    if not constraint.checkable or constraint.value is None:
        return RuleCheckResult(
            constraint_id=constraint.id,
            constraint_type=constraint.constraint_type,
            status="uncheckable",
            passed=True,
            measured=None,
            limit=None,
            margin=None,
            margin_pct=None,
            canonical_unit="",
            page=constraint.page,
            snippet=constraint.snippet,
            confidence=constraint.confidence,
            details="Qualitative or uncheckable constraint; preserved for audit.",
        )

    try:
        limit, canon_unit = normalize_constraint_limit(constraint, rated_mw=rated_mw)
    except UnitConversionError as exc:
        return RuleCheckResult(
            constraint_id=constraint.id,
            constraint_type=constraint.constraint_type,
            status="error",
            passed=False,
            measured=None,
            limit=None,
            margin=None,
            margin_pct=None,
            canonical_unit="",
            page=constraint.page,
            snippet=constraint.snippet,
            confidence=constraint.confidence,
            details=f"Unit normalization error: {exc}",
        )

    if limit < 0.0:
        return RuleCheckResult(
            constraint_id=constraint.id,
            constraint_type=constraint.constraint_type,
            status="error",
            passed=False,
            measured=None,
            limit=limit,
            margin=None,
            margin_pct=None,
            canonical_unit=canon_unit,
            page=constraint.page,
            snippet=constraint.snippet,
            confidence=constraint.confidence,
            details=f"Normalized limit must be non-negative, got {limit}",
        )

    ctype = constraint.constraint_type
    details = ""
    if ctype == ConstraintType.RAMP_RATE.value:
        measured = _measure_ramp_rate(p_grid_mw, dt_s, constraint.window_s)
        win_txt = f"{constraint.window_s:g}s window" if constraint.window_s else f"dt={dt_s:g}s step"
        details = f"Max ramp rate ({win_txt}): {measured:.4f} vs limit {limit:.4f} MW/s"
    elif ctype == ConstraintType.MAX_STEP.value:
        measured = _measure_max_step(p_grid_mw, dt_s, constraint.window_s)
        win_txt = f"{constraint.window_s:g}s window" if constraint.window_s else "single-step"
        details = f"Max step ({win_txt}): {measured:.4f} vs limit {limit:.4f} MW"
    elif ctype == ConstraintType.DYNAMIC_RANGE.value:
        measured = _measure_dynamic_range(p_grid_mw, dt_s, constraint.window_s)
        win_txt = f"{constraint.window_s:g}s window" if constraint.window_s else "full trace"
        details = f"Dynamic range ({win_txt}): {measured:.4f} vs limit {limit:.4f} MW"
    elif ctype == ConstraintType.FLUCTUATION_BAND.value:
        if canon_unit == "pu":
            if shaft is None:
                return RuleCheckResult(
                    constraint_id=constraint.id,
                    constraint_type=ctype,
                    status="error",
                    passed=False,
                    measured=None,
                    limit=limit,
                    margin=None,
                    margin_pct=None,
                    canonical_unit="pu",
                    page=constraint.page,
                    snippet=constraint.snippet,
                    confidence=constraint.confidence,
                    details="Shaft response required to check pu torsional torque constraint.",
                )
            measured = float(shaft.max_torque_pu)
            details = (
                f"Peak shaft torque ({shaft.dominant_section} @ {shaft.dominant_freq_hz:.2f} Hz): "
                f"{measured:.4f} vs limit {limit:.4f} pu"
            )
        else:
            measured, peak_hz = _measure_fluctuation_band_mw(
                p_grid_mw, dt_s, constraint.freq_min_hz, constraint.freq_max_hz
            )
            lo = 0.0 if constraint.freq_min_hz is None else constraint.freq_min_hz
            hi_txt = f"{constraint.freq_max_hz:g}" if constraint.freq_max_hz is not None else "inf"
            details = (
                f"Band [{lo:g}, {hi_txt}] Hz peak amplitude at {peak_hz:.2f} Hz: "
                f"{measured:.4f} vs limit {limit:.4f} MW"
            )
    else:
        return RuleCheckResult(
            constraint_id=constraint.id,
            constraint_type=ctype,
            status="uncheckable",
            passed=True,
            measured=None,
            limit=None,
            margin=None,
            margin_pct=None,
            canonical_unit="",
            page=constraint.page,
            snippet=constraint.snippet,
            confidence=constraint.confidence,
            details="Unsupported constraint type for deterministic check.",
        )

    tol = EXACT_LIMIT_REL_TOL * max(1.0, abs(limit))
    passed = measured <= limit + tol
    raw_margin = limit - measured
    margin = 0.0 if (passed and raw_margin < 0.0) else float(raw_margin)
    margin_pct = (margin / limit) * 100.0 if limit > 0.0 else (0.0 if passed else -100.0)

    return RuleCheckResult(
        constraint_id=constraint.id,
        constraint_type=ctype,
        status="pass" if passed else "fail",
        passed=passed,
        measured=float(measured),
        limit=float(limit),
        margin=margin,
        margin_pct=float(margin_pct),
        canonical_unit=canon_unit,
        page=constraint.page,
        snippet=constraint.snippet,
        confidence=constraint.confidence,
        details=details,
    )


def check_trace(
    p_grid_mw: np.ndarray | Sequence[float],
    dt_s: float,
    ruleset: RuleSet,
    shaft: ShaftResponse | None = None,
    strict_review: bool = True,
) -> ComplianceReport:
    """Deterministically evaluate a RuleSet against a grid power waveform (MW)."""
    arr = np.asarray(p_grid_mw, dtype=np.float64)
    if arr.ndim != 1 or len(arr) < 2:
        raise ValueError("p_grid_mw must be a 1D array with at least 2 samples")
    if dt_s <= 0:
        raise ValueError("dt_s must be positive")

    results: list[RuleCheckResult] = []
    checkable_count = 0
    passed_count = 0
    failed_count = 0
    uncheckable_count = 0
    needs_review_count = 0

    worst_margin_pct: float | None = None
    worst_id: str | None = None

    for c in ruleset.constraints:
        res = _evaluate_single_constraint(
            constraint=c,
            p_grid_mw=arr,
            dt_s=dt_s,
            rated_mw=ruleset.rated_mw,
            shaft=shaft,
            strict_review=strict_review,
        )
        results.append(res)
        if res.status == "uncheckable":
            uncheckable_count += 1
        elif res.status == "needs_review":
            needs_review_count += 1
        else:
            checkable_count += 1
            if res.passed:
                passed_count += 1
            else:
                failed_count += 1
            if res.margin_pct is not None:
                if worst_margin_pct is None or res.margin_pct < worst_margin_pct:
                    worst_margin_pct = res.margin_pct
                    worst_id = res.constraint_id

    refusal_reason: str | None = None
    if needs_review_count > 0 and strict_review:
        overall_passed = False
        refusal_reason = f"{needs_review_count} constraint(s) require human review before certification."
    elif failed_count > 0:
        overall_passed = False
        refusal_reason = f"{failed_count} checkable constraint(s) violated (worst: {worst_id})."
    elif checkable_count == 0:
        overall_passed = False
        refusal_reason = "No checkable numeric constraints present; refusing unverified pass."
    else:
        overall_passed = True

    return ComplianceReport(
        passed=overall_passed,
        checkable_count=checkable_count,
        passed_count=passed_count,
        failed_count=failed_count,
        uncheckable_count=uncheckable_count,
        needs_review_count=needs_review_count,
        worst_margin_pct=worst_margin_pct,
        worst_constraint_id=worst_id,
        results=results,
        refusal_reason=refusal_reason,
    )


def check_compliance(
    sim_result: SimulationResult,
    ruleset: RuleSet,
    strict_review: bool = True,
) -> ComplianceReport:
    """Deterministically check a full SimulationResult against a RuleSet."""
    return check_trace(
        p_grid_mw=sim_result.p_grid_mw,
        dt_s=sim_result.dt_s,
        ruleset=ruleset,
        shaft=sim_result.shaft,
        strict_review=strict_review,
    )
