"""GridSafe interconnection rules schema, unit normalization, and deterministic checker."""

from rules.checker import check_compliance, check_trace
from rules.schema import (
    CHECKABLE_CONSTRAINT_TYPES,
    ComplianceReport,
    Constraint,
    ConstraintType,
    RuleCheckResult,
    RuleSet,
)
from rules.units import (
    UnitConversionError,
    normalize_constraint_limit,
    normalize_frequency_hz,
    normalize_power_mw,
    normalize_ramp_rate_mw_per_s,
    normalize_time_s,
)

__all__ = [
    "CHECKABLE_CONSTRAINT_TYPES",
    "ComplianceReport",
    "Constraint",
    "ConstraintType",
    "RuleCheckResult",
    "RuleSet",
    "UnitConversionError",
    "check_compliance",
    "check_trace",
    "normalize_constraint_limit",
    "normalize_frequency_hz",
    "normalize_power_mw",
    "normalize_ramp_rate_mw_per_s",
    "normalize_time_s",
]
