"""Deterministic interconnection compliance checker.

Pure Python + numpy (no scipy, no AI). Evaluates normalized checkable constraints
against a simulated or raw net grid power trace and IEEE FBM shaft response,
returning exact pass/fail/not_evaluable verdicts, margins, and certificate assumptions.

Validity invariants:
- If a trace is shorter than 2x a rule's measurement window, that rule returns
  status="not_evaluable" (passed=None) — never pass or fail.
- Every simulation starts with the facility at idle; check_compliance() passes
  sim_result.initial_grid_mw so check_trace() prepends the pre-t=0 idle state,
  ensuring any t=0 step jump counts against ramp, step, and dynamic range rules.
- Per-minute ramp rates without an explicit window assume a 60 s rolling window
  (recorded on Constraint.assumptions and propagated to the certificate).
- Sub-synchronous fluctuation limits without Hz bounds assume 5.0-55.0 Hz
  (recorded on Constraint.assumptions and propagated to the certificate).
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
MIN_WINDOW_MULTIPLIER = 2.0


def _sliding_window_range_max(arr: np.ndarray, window_steps: int) -> float:
    """Exact O(N) maximum of (max - min) over every contiguous window of length window_steps + 1."""
    n = len(arr)
    if n < 2:
        return 0.0
    k = min(max(1, int(window_steps)), n - 1)
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


def _prepend_idle(arr: np.ndarray, initial_power_mw: float | None, pad_steps: int = 1) -> np.ndarray:
    """Prepend steady pre-t=0 facility idle power so t=0 jumps count against time-domain rules."""
    if initial_power_mw is None or pad_steps <= 0:
        return arr
    pad = np.full(int(pad_steps), float(initial_power_mw), dtype=np.float64)
    return np.concatenate((pad, arr))


def _measure_ramp_rate(
    p_grid_mw: np.ndarray,
    dt_s: float,
    window_s: float | None,
    initial_power_mw: float | None = None,
) -> float:
    """Maximum ramp rate in MW/s over dt_s or an explicit rolling window_s."""
    n = len(p_grid_mw)
    if n < 2:
        return 0.0
    if window_s is None or window_s <= dt_s + 1e-12:
        arr = _prepend_idle(p_grid_mw, initial_power_mw, pad_steps=1)
        diffs = np.abs(np.diff(arr))
        return float(np.max(diffs) / dt_s)

    k = max(1, int(round(window_s / dt_s)))
    arr = _prepend_idle(p_grid_mw, initial_power_mw, pad_steps=k)
    diffs = np.abs(arr[k:] - arr[:-k])
    return float(np.max(diffs) / (k * dt_s))


def _measure_max_step(
    p_grid_mw: np.ndarray,
    dt_s: float,
    window_s: float | None,
    initial_power_mw: float | None = None,
) -> float:
    """Maximum step change in MW (single step when window_s is None, else max window swing)."""
    n = len(p_grid_mw)
    if n < 2:
        return 0.0
    if window_s is None or window_s <= dt_s + 1e-12:
        arr = _prepend_idle(p_grid_mw, initial_power_mw, pad_steps=1)
        return float(np.max(np.abs(np.diff(arr))))
    k = max(1, int(round(window_s / dt_s)))
    arr = _prepend_idle(p_grid_mw, initial_power_mw, pad_steps=k)
    return _sliding_window_range_max(arr, k)


def _measure_dynamic_range(
    p_grid_mw: np.ndarray,
    dt_s: float,
    window_s: float | None,
    initial_power_mw: float | None = None,
) -> float:
    """Maximum peak-to-peak power swing in MW (full trace or sliding window)."""
    n = len(p_grid_mw)
    if n < 2:
        return 0.0
    if window_s is None:
        arr = _prepend_idle(p_grid_mw, initial_power_mw, pad_steps=1)
        return float(np.max(arr) - np.min(arr))
    k = max(1, int(round(window_s / dt_s)))
    arr = _prepend_idle(p_grid_mw, initial_power_mw, pad_steps=k)
    return _sliding_window_range_max(arr, k)


def _fft_band_amplitudes(
    segment_mw: np.ndarray,
    dt_s: float,
    freq_min_hz: float | None,
    freq_max_hz: float | None,
) -> tuple[np.ndarray, np.ndarray]:
    """One-sided FFT zero-to-peak amplitudes (MW) and frequencies inside the band (excl. DC)."""
    n = len(segment_mw)
    if n < 2:
        return np.zeros(0, dtype=np.float64), np.zeros(0, dtype=np.float64)
    dp = segment_mw - float(np.mean(segment_mw))
    freqs = np.fft.rfftfreq(n, d=dt_s)
    fft_vals = np.fft.rfft(dp)
    scale = np.full(len(freqs), 2.0 / n, dtype=np.float64)
    scale[0] = 1.0 / n
    if n % 2 == 0:
        scale[-1] = 1.0 / n
    amps = np.abs(fft_vals) * scale
    f_lo = 0.0 if freq_min_hz is None else float(freq_min_hz)
    f_hi = float("inf") if freq_max_hz is None else float(freq_max_hz)
    mask = (freqs > 0.0) & (freqs >= f_lo - 1e-12) & (freqs <= f_hi + 1e-12)
    return amps[mask], freqs[mask]


def _aggregate_band_amps(amps: np.ndarray, freqs: np.ndarray, aggregation: str) -> tuple[float, float]:
    """Return (metric_mw, detail_hz) for peak_bin / adjacent_pair_sum / band_sum."""
    from rules.schema import (
        AGGREGATION_ADJACENT_PAIR_SUM,
        AGGREGATION_BAND_SUM,
        AGGREGATION_PEAK_BIN,
    )

    if len(amps) == 0:
        return 0.0, 0.0
    if aggregation == AGGREGATION_BAND_SUM:
        return float(np.sum(amps)), float(freqs[int(np.argmax(amps))])
    if aggregation == AGGREGATION_ADJACENT_PAIR_SUM:
        if len(amps) == 1:
            return float(amps[0]), float(freqs[0])
        pair_sums = amps[:-1] + amps[1:]
        idx = int(np.argmax(pair_sums))
        return float(pair_sums[idx]), float(0.5 * (freqs[idx] + freqs[idx + 1]))
    # peak_bin default
    idx = int(np.argmax(amps))
    return float(amps[idx]), float(freqs[idx])


def _measure_fluctuation_band_mw(
    p_grid_mw: np.ndarray,
    dt_s: float,
    freq_min_hz: float | None,
    freq_max_hz: float | None,
    *,
    aggregation: str = "peak_bin",
    fft_window_s: float | None = None,
    averaging_window_s: float | None = None,
) -> tuple[float, float, str]:
    """Measure fluctuation-band metric (MW). Returns (measured, detail_hz, detail_txt)."""
    n = len(p_grid_mw)
    if n < 2:
        return 0.0, 0.0, "empty trace"

    # Whole-trace FFT when no FFT window stated (legacy / peak_bin default path).
    if fft_window_s is None or fft_window_s <= dt_s:
        amps, freqs = _fft_band_amplitudes(p_grid_mw, dt_s, freq_min_hz, freq_max_hz)
        measured, detail_hz = _aggregate_band_amps(amps, freqs, aggregation)
        return measured, detail_hz, f"whole-trace FFT aggregation={aggregation}"

    win_n = max(2, int(round(float(fft_window_s) / dt_s)))
    if win_n > n:
        amps, freqs = _fft_band_amplitudes(p_grid_mw, dt_s, freq_min_hz, freq_max_hz)
        measured, detail_hz = _aggregate_band_amps(amps, freqs, aggregation)
        return measured, detail_hz, "fft_window longer than trace; used whole-trace FFT"

    # Hop by half window (or 1 sample floor) for sliding FFT windows
    hop = max(1, win_n // 2)
    series: list[float] = []
    detail_hz = 0.0
    for start in range(0, n - win_n + 1, hop):
        seg = p_grid_mw[start : start + win_n]
        amps, freqs = _fft_band_amplitudes(seg, dt_s, freq_min_hz, freq_max_hz)
        m, hz = _aggregate_band_amps(amps, freqs, aggregation)
        series.append(m)
        if m >= (max(series) if series else m):
            detail_hz = hz
    if not series:
        return 0.0, 0.0, "no FFT windows"

    metric = np.asarray(series, dtype=np.float64)
    if averaging_window_s is not None and averaging_window_s > 0:
        # Each series sample spans `hop * dt_s` seconds of advance
        avg_n = max(1, int(round(float(averaging_window_s) / (hop * dt_s))))
        if avg_n <= 1 or avg_n > len(metric):
            measured = float(np.max(metric))
            detail = f"max over FFT windows; avg_window unused (avg_n={avg_n})"
        else:
            kernel = np.ones(avg_n, dtype=np.float64) / avg_n
            rolled = np.convolve(metric, kernel, mode="valid")
            measured = float(np.max(rolled))
            detail = f"max of {averaging_window_s:g}s rolling avg over FFT windows (agg={aggregation})"
    else:
        measured = float(np.max(metric))
        detail = f"max over FFT windows (agg={aggregation})"
    return measured, float(detail_hz), detail


def _evaluate_single_constraint(
    constraint: Constraint,
    p_grid_mw: np.ndarray,
    dt_s: float,
    rated_mw: float,
    shaft: ShaftResponse | None,
    strict_review: bool,
    initial_power_mw: float | None = None,
) -> RuleCheckResult:
    assumptions = list(constraint.assumptions)

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
            entity=constraint.entity,
            details="Flagged for human review; cannot certify automatically.",
            assumptions=assumptions,
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
            entity=constraint.entity,
            details="Qualitative or uncheckable constraint; preserved for audit.",
            assumptions=assumptions,
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
            entity=constraint.entity,
            details=f"Unit normalization error: {exc}",
            assumptions=assumptions,
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
            entity=constraint.entity,
            details=f"Normalized limit must be non-negative, got {limit}",
            assumptions=assumptions,
        )

    ctype = constraint.constraint_type
    trace_duration_s = float(len(p_grid_mw)) * float(dt_s)

    # Window validity gate: trace MUST be at least 2x the rule's window; never pass or fail otherwise.
    if constraint.window_s is not None and constraint.window_s > 0.0:
        min_required_s = MIN_WINDOW_MULTIPLIER * float(constraint.window_s)
        if trace_duration_s < min_required_s - 1e-9:
            return RuleCheckResult(
                constraint_id=constraint.id,
                constraint_type=ctype,
                status="not_evaluable",
                passed=None,
                measured=None,
                limit=float(limit),
                margin=None,
                margin_pct=None,
                canonical_unit=canon_unit,
                page=constraint.page,
                snippet=constraint.snippet,
                confidence=constraint.confidence,
                entity=constraint.entity,
                details=(
                    f"Not evaluable on this trace: duration {trace_duration_s:g}s "
                    f"< 2x rule window ({min_required_s:g}s for {constraint.window_s:g}s window)."
                ),
                assumptions=assumptions,
            )

    details = ""
    if ctype == ConstraintType.RAMP_RATE.value:
        measured = _measure_ramp_rate(p_grid_mw, dt_s, constraint.window_s, initial_power_mw=initial_power_mw)
        win_txt = f"{constraint.window_s:g}s window" if constraint.window_s else f"dt={dt_s:g}s step"
        details = f"Max ramp rate ({win_txt}): {measured:.4f} vs limit {limit:.4f} MW/s"
    elif ctype == ConstraintType.MAX_STEP.value:
        measured = _measure_max_step(p_grid_mw, dt_s, constraint.window_s, initial_power_mw=initial_power_mw)
        win_txt = f"{constraint.window_s:g}s window" if constraint.window_s else "single-step"
        details = f"Max step ({win_txt}): {measured:.4f} vs limit {limit:.4f} MW"
    elif ctype == ConstraintType.DYNAMIC_RANGE.value:
        measured = _measure_dynamic_range(p_grid_mw, dt_s, constraint.window_s, initial_power_mw=initial_power_mw)
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
                    entity=constraint.entity,
                    details="Shaft response required to check pu torsional torque constraint.",
                    assumptions=assumptions,
                )
            measured = float(shaft.max_torque_pu)
            details = (
                f"Peak shaft torque ({shaft.dominant_section} @ {shaft.dominant_freq_hz:.2f} Hz): "
                f"{measured:.4f} vs limit {limit:.4f} pu"
            )
        else:
            f_nyq = 0.5 / float(dt_s)
            f_lo = 0.0 if constraint.freq_min_hz is None else float(constraint.freq_min_hz)
            f_hi = float("inf") if constraint.freq_max_hz is None else float(constraint.freq_max_hz)

            # Check Nyquist & minimum cycles observability so slow traces never vacuously pass high-Hz bands
            if f_lo > 0.0 and f_nyq <= f_lo + 1e-12:
                hi_txt = f"{f_hi:g}" if f_hi < float("inf") else "inf"
                return RuleCheckResult(
                    constraint_id=constraint.id,
                    constraint_type=ctype,
                    status="not_evaluable",
                    passed=None,
                    measured=None,
                    limit=float(limit),
                    margin=None,
                    margin_pct=None,
                    canonical_unit=canon_unit,
                    page=constraint.page,
                    snippet=constraint.snippet,
                    confidence=constraint.confidence,
                    entity=constraint.entity,
                    details=(
                        f"Not evaluable on this trace: band [{f_lo:g}, {hi_txt}] Hz "
                        f"is above trace Nyquist frequency ({f_nyq:g} Hz at dt={dt_s:g}s)."
                    ),
                    assumptions=assumptions,
                )
            if f_lo > 0.0 and trace_duration_s < (2.0 / f_lo) - 1e-9:
                return RuleCheckResult(
                    constraint_id=constraint.id,
                    constraint_type=ctype,
                    status="not_evaluable",
                    passed=None,
                    measured=None,
                    limit=float(limit),
                    margin=None,
                    margin_pct=None,
                    canonical_unit=canon_unit,
                    page=constraint.page,
                    snippet=constraint.snippet,
                    confidence=constraint.confidence,
                    entity=constraint.entity,
                    details=(
                        f"Not evaluable on this trace: duration {trace_duration_s:g}s "
                        f"< 2 cycles of {f_lo:g} Hz ({2.0 / f_lo:g}s)."
                    ),
                    assumptions=assumptions,
                )

            if f_hi < float("inf") and f_nyq < f_hi - 1e-9:
                note = (
                    f"Partial band [{f_lo:g}, {f_nyq:g}] Hz evaluated on dt={dt_s:g}s trace "
                    f"(upper bound {f_hi:g} Hz exceeds Nyquist {f_nyq:g} Hz)."
                )
                if note not in assumptions:
                    assumptions.append(note)

            # Band rules with an explicit measurement / averaging window need >= 2x that window.
            band_window_s = None
            for w_candidate in (
                getattr(constraint, "averaging_window_s", None),
                constraint.window_s,
                getattr(constraint, "fft_window_s", None),
            ):
                if w_candidate is not None and float(w_candidate) > 0:
                    band_window_s = float(w_candidate) if band_window_s is None else max(band_window_s, float(w_candidate))
            if band_window_s is not None and trace_duration_s < 2.0 * band_window_s - 1e-9:
                return RuleCheckResult(
                    constraint_id=constraint.id,
                    constraint_type=ctype,
                    status="not_evaluable",
                    passed=None,
                    measured=None,
                    limit=float(limit),
                    margin=None,
                    margin_pct=None,
                    canonical_unit=canon_unit,
                    page=constraint.page,
                    snippet=constraint.snippet,
                    confidence=constraint.confidence,
                    entity=constraint.entity,
                    details=(
                        f"Not evaluable on this trace: duration {trace_duration_s:g}s "
                        f"< 2x band measurement window {band_window_s:g}s."
                    ),
                    assumptions=assumptions,
                )

            agg = getattr(constraint, "aggregation", "peak_bin") or "peak_bin"
            measured, peak_hz, meas_detail = _measure_fluctuation_band_mw(
                p_grid_mw,
                dt_s,
                constraint.freq_min_hz,
                constraint.freq_max_hz,
                aggregation=agg,
                fft_window_s=getattr(constraint, "fft_window_s", None),
                averaging_window_s=getattr(constraint, "averaging_window_s", None),
            )
            hi_txt = f"{constraint.freq_max_hz:g}" if constraint.freq_max_hz is not None else "inf"
            details = (
                f"Band [{f_lo:g}, {hi_txt}] Hz aggregation={agg} near {peak_hz:.2f} Hz "
                f"({meas_detail}): {measured:.4f} vs limit {limit:.4f} MW"
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
            entity=constraint.entity,
            details="Unsupported constraint type for deterministic check.",
            assumptions=assumptions,
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
        entity=constraint.entity,
        details=details,
        assumptions=assumptions,
    )


def check_trace(
    p_grid_mw: np.ndarray | Sequence[float],
    dt_s: float,
    ruleset: RuleSet,
    shaft: ShaftResponse | None = None,
    strict_review: bool = True,
    initial_power_mw: float | None = None,
) -> ComplianceReport:
    """Deterministically evaluate a RuleSet against a grid power waveform (MW)."""
    arr = np.asarray(p_grid_mw, dtype=np.float64)
    if arr.ndim != 1 or len(arr) < 2:
        raise ValueError("p_grid_mw must be a 1D array with at least 2 samples")
    if dt_s <= 0:
        raise ValueError("dt_s must be positive")

    results: list[RuleCheckResult] = []
    checkable_count = 0
    evaluable_count = 0
    not_evaluable_count = 0
    passed_count = 0
    failed_count = 0
    uncheckable_count = 0
    needs_review_count = 0
    all_assumptions: list[str] = []

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
            initial_power_mw=initial_power_mw,
        )
        results.append(res)
        for a in res.assumptions:
            if a not in all_assumptions:
                all_assumptions.append(a)

        if res.status == "uncheckable":
            uncheckable_count += 1
        elif res.status == "needs_review":
            needs_review_count += 1
        elif res.status == "not_evaluable":
            checkable_count += 1
            not_evaluable_count += 1
        else:
            checkable_count += 1
            evaluable_count += 1
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
        status = "refused"
        overall_passed = False
        refusal_reason = f"{needs_review_count} constraint(s) require human review before certification."
    elif failed_count > 0:
        status = "fail"
        overall_passed = False
        refusal_reason = f"{failed_count} checkable constraint(s) violated (worst: {worst_id})."
    elif checkable_count == 0:
        status = "refused"
        overall_passed = False
        refusal_reason = "No checkable numeric constraints present; refusing unverified pass."
    elif evaluable_count == 0:
        status = "not_evaluable"
        overall_passed = False
        refusal_reason = (
            f"All {not_evaluable_count} checkable constraint(s) are not_evaluable on this trace "
            f"(duration {len(arr) * dt_s:g}s, dt={dt_s:g}s)."
        )
    else:
        status = "pass"
        overall_passed = True

    return ComplianceReport(
        status=status,
        passed=overall_passed,
        checkable_count=checkable_count,
        evaluable_count=evaluable_count,
        not_evaluable_count=not_evaluable_count,
        passed_count=passed_count,
        failed_count=failed_count,
        uncheckable_count=uncheckable_count,
        needs_review_count=needs_review_count,
        worst_margin_pct=worst_margin_pct,
        worst_constraint_id=worst_id,
        results=results,
        refusal_reason=refusal_reason,
        assumptions=all_assumptions,
    )


def check_compliance(
    sim_result: SimulationResult,
    ruleset: RuleSet,
    strict_review: bool = True,
) -> ComplianceReport:
    """Deterministically check a full SimulationResult against a RuleSet (prepending facility idle)."""
    return check_trace(
        p_grid_mw=sim_result.p_grid_mw,
        dt_s=sim_result.dt_s,
        ruleset=ruleset,
        shaft=sim_result.shaft,
        strict_review=strict_review,
        initial_power_mw=sim_result.initial_grid_mw,
    )
