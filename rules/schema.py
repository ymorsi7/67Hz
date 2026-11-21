"""Typed constraint schema for large-load interconnection rulebooks.

Pure Python stdlib only (no pydantic/scipy) so it runs identically inside the
managed agent sandbox and on the FastAPI backend.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ConstraintType(str, Enum):
    RAMP_RATE = "ramp_rate"
    MAX_STEP = "max_step"
    FLUCTUATION_BAND = "fluctuation_band"
    DYNAMIC_RANGE = "dynamic_range"
    OTHER = "other"


CHECKABLE_CONSTRAINT_TYPES = frozenset(
    {
        ConstraintType.RAMP_RATE.value,
        ConstraintType.MAX_STEP.value,
        ConstraintType.FLUCTUATION_BAND.value,
        ConstraintType.DYNAMIC_RANGE.value,
    }
)


@dataclass
class Constraint:
    """Single interconnection rule extracted from a utility document."""

    constraint_type: str
    value: float | None
    unit: str
    page: int
    snippet: str
    checkable: bool = True
    confidence: float | None = None
    id: str = ""
    window_s: float | None = None
    freq_min_hz: float | None = None
    freq_max_hz: float | None = None
    source_section: str = ""
    verified: bool | None = None
    needs_human_review: bool = False
    conflict_flag: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.constraint_type, ConstraintType):
            self.constraint_type = self.constraint_type.value
        else:
            self.constraint_type = str(self.constraint_type).strip().lower()

        if self.constraint_type not in {c.value for c in ConstraintType}:
            raise ValueError(f"Unsupported constraint_type: {self.constraint_type!r}")

        if self.constraint_type == ConstraintType.OTHER.value:
            self.checkable = False
        if self.value is None:
            self.checkable = False

        if self.page < 1:
            raise ValueError(f"page must be >= 1, got {self.page}")

        if self.confidence is not None and not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")

        if self.window_s is not None and self.window_s < 0:
            raise ValueError(f"window_s must be non-negative, got {self.window_s}")

        if self.freq_min_hz is not None and self.freq_min_hz < 0:
            raise ValueError(f"freq_min_hz must be non-negative, got {self.freq_min_hz}")
        if self.freq_max_hz is not None and self.freq_max_hz < 0:
            raise ValueError(f"freq_max_hz must be non-negative, got {self.freq_max_hz}")
        if (
            self.freq_min_hz is not None
            and self.freq_max_hz is not None
            and self.freq_max_hz < self.freq_min_hz
        ):
            raise ValueError("freq_max_hz must be >= freq_min_hz")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Constraint":
        allowed = {
            "constraint_type",
            "value",
            "unit",
            "page",
            "snippet",
            "checkable",
            "confidence",
            "id",
            "window_s",
            "freq_min_hz",
            "freq_max_hz",
            "source_section",
            "verified",
            "needs_human_review",
            "conflict_flag",
            "notes",
        }
        filtered = {k: v for k, v in data.items() if k in allowed}
        return cls(**filtered)


@dataclass
class RuleSet:
    """Collection of constraints parsed from one interconnection rulebook."""

    constraints: list[Constraint] = field(default_factory=list)
    rated_mw: float = 100.0
    source_name: str = ""

    def __post_init__(self) -> None:
        if self.rated_mw <= 0:
            raise ValueError("rated_mw must be positive")
        for idx, c in enumerate(self.constraints):
            if not c.id:
                c.id = f"{c.constraint_type}_{idx}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "rated_mw": self.rated_mw,
            "constraints": [c.to_dict() for c in self.constraints],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuleSet":
        return cls(
            source_name=str(data.get("source_name", "")),
            rated_mw=float(data.get("rated_mw", 100.0)),
            constraints=[Constraint.from_dict(item) for item in data.get("constraints", [])],
        )


@dataclass
class RuleCheckResult:
    """Deterministic pass/fail outcome with margin for one constraint."""

    constraint_id: str
    constraint_type: str
    status: str  # "pass" | "fail" | "uncheckable" | "needs_review" | "error"
    passed: bool
    measured: float | None
    limit: float | None
    margin: float | None  # limit - measured; >= 0 means compliant, < 0 means violation
    margin_pct: float | None
    canonical_unit: str
    page: int
    snippet: str
    confidence: float | None = None
    details: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ComplianceReport:
    """Aggregate deterministic compliance verification report."""

    passed: bool
    checkable_count: int
    passed_count: int
    failed_count: int
    uncheckable_count: int
    needs_review_count: int
    worst_margin_pct: float | None
    worst_constraint_id: str | None
    results: list[RuleCheckResult] = field(default_factory=list)
    refusal_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checkable_count": self.checkable_count,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "uncheckable_count": self.uncheckable_count,
            "needs_review_count": self.needs_review_count,
            "worst_margin_pct": self.worst_margin_pct,
            "worst_constraint_id": self.worst_constraint_id,
            "refusal_reason": self.refusal_reason,
            "results": [r.to_dict() for r in self.results],
        }
