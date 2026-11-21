"""Deterministic unit normalization to MW, seconds, and Hz.

Pure Python stdlib only. Handles kW/MW/GW/W, % of rated load, per second /
per minute / per hour ramp rates, time windows, and frequencies.
"""

from __future__ import annotations

import math
import re

from rules.schema import Constraint, ConstraintType


class UnitConversionError(ValueError):
    """Raised when a unit string cannot be normalized safely."""


_POWER_FACTORS_TO_MW: dict[str, float] = {
    "w": 1e-6,
    "watt": 1e-6,
    "watts": 1e-6,
    "kw": 1e-3,
    "kilowatt": 1e-3,
    "kilowatts": 1e-3,
    "mw": 1.0,
    "megawatt": 1.0,
    "megawatts": 1.0,
    "gw": 1e3,
    "gigawatt": 1e3,
    "gigawatts": 1e3,
}

_PERCENT_ALIASES = frozenset(
    {
        "%",
        "pct",
        "percent",
        "percentage",
        "% rated",
        "% of rated",
        "% rated load",
        "% of rated load",
        "% of rated power",
        "% of nameplate",
        "% of cluster",
        "percent of rated load",
        "percent of rated power",
        "percent rated load",
        "pct of rated load",
    }
)

_TIME_FACTORS_TO_S: dict[str, float] = {
    "ms": 1e-3,
    "msec": 1e-3,
    "millisecond": 1e-3,
    "milliseconds": 1e-3,
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
}

_FREQ_FACTORS_TO_HZ: dict[str, float] = {
    "mhz": 1e-3,
    "millihertz": 1e-3,
    "hz": 1.0,
    "hertz": 1.0,
    "cps": 1.0,
    "khz": 1e3,
    "kilohertz": 1e3,
    "cpm": 1.0 / 60.0,
    "rad/s": 1.0 / (2.0 * math.pi),
    "rad/sec": 1.0 / (2.0 * math.pi),
    "rad per s": 1.0 / (2.0 * math.pi),
    "rad per second": 1.0 / (2.0 * math.pi),
}


def _clean_unit(unit: str) -> str:
    if not isinstance(unit, str) or not unit.strip():
        raise UnitConversionError("Unit string is empty")
    text = unit.strip()
    # Normalize unicode slashes/dashes/plus-minus and collapse whitespace
    text = text.replace("±", "").replace("+/-", "").replace("−", "-").replace("⁄", "/")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_power_mw(value: float, unit: str, rated_mw: float | None = None) -> float:
    """Convert a power quantity (W, kW, MW, GW, % of rated load, or pu) to MW."""
    val = float(value)
    if not math.isfinite(val):
        raise UnitConversionError(f"Non-finite power value: {value!r}")

    cleaned = _clean_unit(unit).lower()
    if cleaned in _POWER_FACTORS_TO_MW:
        return val * _POWER_FACTORS_TO_MW[cleaned]

    if cleaned in _PERCENT_ALIASES or (
        ("%" in cleaned or "percent" in cleaned or "pct" in cleaned)
        and ("/" not in cleaned and " per " not in cleaned)
    ):
        if rated_mw is None or rated_mw <= 0:
            raise UnitConversionError(f"rated_mw must be positive to convert percent unit {unit!r}")
        return (val / 100.0) * float(rated_mw)

    if cleaned in {"pu", "p.u.", "per unit"}:
        if rated_mw is None or rated_mw <= 0:
            raise UnitConversionError(f"rated_mw must be positive to convert pu power unit {unit!r}")
        return val * float(rated_mw)

    raise UnitConversionError(f"Unsupported power unit: {unit!r}")


def normalize_time_s(value: float, unit: str) -> float:
    """Convert a time duration (ms, s, min, hr) to seconds."""
    val = float(value)
    if not math.isfinite(val):
        raise UnitConversionError(f"Non-finite time value: {value!r}")

    cleaned = _clean_unit(unit).lower()
    if cleaned in _TIME_FACTORS_TO_S:
        return val * _TIME_FACTORS_TO_S[cleaned]
    raise UnitConversionError(f"Unsupported time unit: {unit!r}")


def normalize_frequency_hz(value: float, unit: str) -> float:
    """Convert a frequency (mHz, Hz, kHz, cpm, rad/s) to Hz."""
    val = float(value)
    if not math.isfinite(val):
        raise UnitConversionError(f"Non-finite frequency value: {value!r}")

    raw = _clean_unit(unit)
    # Distinguish mHz (millihertz) vs MHz (megahertz) before lowercasing
    if raw == "MHz":
        return val * 1e6
    if raw == "mHz":
        return val * 1e-3

    cleaned = raw.lower()
    if cleaned in _FREQ_FACTORS_TO_HZ:
        return val * _FREQ_FACTORS_TO_HZ[cleaned]
    raise UnitConversionError(f"Unsupported frequency unit: {unit!r}")


def normalize_ramp_rate_mw_per_s(value: float, unit: str, rated_mw: float | None = None) -> float:
    """Convert a ramp rate (e.g., MW/min, kW/s, % of rated load per minute) to MW/s."""
    val = float(value)
    if not math.isfinite(val):
        raise UnitConversionError(f"Non-finite ramp rate value: {value!r}")

    cleaned = _clean_unit(unit)
    # Split on "/" or " per " (case-insensitive)
    if "/" in cleaned:
        parts = [p.strip() for p in cleaned.split("/", 1)]
    else:
        m = re.split(r"\s+per\s+", cleaned, maxsplit=1, flags=re.IGNORECASE)
        if len(m) != 2:
            raise UnitConversionError(f"Ramp rate unit must have numerator/denominator: {unit!r}")
        parts = [m[0].strip(), m[1].strip()]

    num_unit, den_unit = parts[0], parts[1]
    power_mw = normalize_power_mw(val, num_unit, rated_mw=rated_mw)
    time_s = normalize_time_s(1.0, den_unit)
    return power_mw / time_s


def is_per_minute_ramp_unit(unit: str) -> bool:
    """Return True if unit is a ramp-rate unit expressed per minute (e.g., MW/min, %/min)."""
    try:
        cleaned = _clean_unit(unit)
    except UnitConversionError:
        return False

    if "/" in cleaned:
        parts = [p.strip() for p in cleaned.split("/", 1)]
    else:
        m = re.split(r"\s+per\s+", cleaned, maxsplit=1, flags=re.IGNORECASE)
        if len(m) != 2:
            return False
        parts = [m[0].strip(), m[1].strip()]

    try:
        time_s = normalize_time_s(1.0, parts[1])
    except UnitConversionError:
        return False
    return abs(time_s - 60.0) < 1e-9


_WINDOW_RANGE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:to|–|—|-)\s*(\d+(?:\.\d+)?)\s*"
    r"(ms|msec|milliseconds?|s|sec|secs|seconds?|min|mins|minutes?)\b",
    flags=re.IGNORECASE,
)


def parse_window_range_s(text: str) -> tuple[float, float] | None:
    """Parse an explicit time-window range (e.g. '4–6 second interval') into (lo_s, hi_s)."""
    if not text:
        return None
    for m in _WINDOW_RANGE_RE.finditer(text):
        lo_raw = float(m.group(1))
        hi_raw = float(m.group(2))
        unit = m.group(3)
        try:
            lo_s = normalize_time_s(lo_raw, unit)
            hi_s = normalize_time_s(hi_raw, unit)
        except UnitConversionError:
            continue
        if hi_s > lo_s > 0.0:
            return (lo_s, hi_s)
    return None


def normalize_constraint_limit(
    constraint: Constraint,
    rated_mw: float | None = None,
) -> tuple[float, str]:
    """Normalize a checkable constraint's numeric limit into canonical units (MW, MW/s, pu)."""
    if constraint.value is None:
        raise UnitConversionError("Cannot normalize constraint with value=None")

    ctype = constraint.constraint_type
    val = float(constraint.value)
    unit = constraint.unit
    cleaned = _clean_unit(unit).lower()

    if ctype == ConstraintType.RAMP_RATE.value:
        return normalize_ramp_rate_mw_per_s(val, unit, rated_mw=rated_mw), "MW/s"

    if ctype in (ConstraintType.MAX_STEP.value, ConstraintType.DYNAMIC_RANGE.value):
        return normalize_power_mw(val, unit, rated_mw=rated_mw), "MW"

    if ctype == ConstraintType.FLUCTUATION_BAND.value:
        # Fluctuation band may constrain grid power amplitude (MW / kW / %) OR shaft torque (pu)
        if cleaned in {"pu", "p.u.", "pu torque", "p.u. torque"}:
            return val, "pu"
        return normalize_power_mw(val, unit, rated_mw=rated_mw), "MW"

    raise UnitConversionError(f"Constraint type {ctype!r} has no canonical checkable unit")
