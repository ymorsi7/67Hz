"""Typed constraint schema for large-load interconnection rulebooks.

Pure Python stdlib only (no pydantic/scipy) so it runs identically inside the
managed agent sandbox and on the FastAPI backend.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import re
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

# Documented checker/constraint defaults (always attached to Constraint.assumptions and certificates)
DEFAULT_PER_MINUTE_RAMP_WINDOW_S = 60.0
DEFAULT_SUBSYNCHRONOUS_BAND_HZ: tuple[float, float] = (5.0, 55.0)

_SUBSYNCHRONOUS_RE = re.compile(r"sub[-\s]?synchronous|\bssti\b|\bssci\b", flags=re.IGNORECASE)

# Ordered utility / entity canonicalization patterns
ENTITY_PATTERNS: list[tuple[str, str]] = [
    ("ERCOT", r"\bercot\b"),
    ("LIPA", r"\blipa\b|long island power|pseg\s+long\s+island|lipasystem"),
    ("AESO", r"\baeso\b|alberta electric"),
    ("ATC", r"\batc\b|american transmission"),
    ("Southern", r"\bsouthern\s+company\b|\bsouthern\b|\bsct\b|\bsoco\b"),
    ("CAISO", r"\bcaiso\b|california iso"),
    ("IESO", r"\bieso\b|independent electricity system operator|largecomputationalloads"),
    ("MISO", r"\bmiso\b|ameren|\bllwg\b"),
    ("PJM", r"\bpjm\b"),
    ("NYISO", r"\bnyiso\b"),
    ("ISO-NE", r"\biso-ne\b|iso new england"),
    ("SPP", r"\bspp\b|southwest power pool"),
    ("AEP", r"\baep\b|american electric power"),
    ("Dominion", r"\bdominion\b"),
    ("PG&E", r"\bpg&e\b|pacific gas"),
    ("Oncor", r"\boncor\b"),
    ("Duke", r"\bduke\s+energy\b"),
    ("BPA", r"\bbpa\b|bonneville power"),
    ("NERC", r"\bnerc\b"),
]


def canonicalize_entity(
    entity: str = "",
    source_section: str = "",
    snippet: str = "",
    doc_context: str = "",
) -> str:
    """Normalize a utility/ISO entity name deterministically from explicit or contextual fields."""
    raw_ent = (entity or "").strip()
    if raw_ent:
        for canonical, pat in ENTITY_PATTERNS:
            if re.search(pat, raw_ent, flags=re.IGNORECASE):
                return canonical
        return raw_ent

    sec = (source_section or "").strip()
    if sec:
        for canonical, pat in ENTITY_PATTERNS:
            if re.search(pat, sec, flags=re.IGNORECASE):
                return canonical

    head = (snippet or "").strip()[:80]
    if head:
        for canonical, pat in ENTITY_PATTERNS:
            if re.search(pat, head, flags=re.IGNORECASE):
                return canonical

    ctx = (doc_context or "").strip()
    if ctx:
        for canonical, pat in ENTITY_PATTERNS:
            if re.search(pat, ctx, flags=re.IGNORECASE):
                return canonical
        if "large-load-technical-requirements-straw-proposal" in ctx.lower():
            return "CAISO"

    return ""


def _append_unique(items: list[str], msg: str) -> None:
    if msg and msg not in items:
        items.append(msg)


AGGREGATION_PEAK_BIN = "peak_bin"
AGGREGATION_ADJACENT_PAIR_SUM = "adjacent_pair_sum"
AGGREGATION_BAND_SUM = "band_sum"
VALID_AGGREGATIONS = {
    AGGREGATION_PEAK_BIN,
    AGGREGATION_ADJACENT_PAIR_SUM,
    AGGREGATION_BAND_SUM,
}


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
    entity: str = ""
    window_s: float | None = None
    freq_min_hz: float | None = None
    freq_max_hz: float | None = None
    # Frequency-band measurement semantics (LIPA Table 4-2 and similar):
    # peak_bin (default), adjacent_pair_sum, band_sum
    aggregation: str = AGGREGATION_PEAK_BIN
    fft_window_s: float | None = None
    averaging_window_s: float | None = None
    source_section: str = ""
    verified: bool | None = None
    needs_human_review: bool = False
    conflict_flag: bool = False
    notes: str = ""
    assumptions: list[str] = field(default_factory=list)

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

        self.aggregation = str(self.aggregation or AGGREGATION_PEAK_BIN).strip().lower()
        if self.aggregation not in VALID_AGGREGATIONS:
            raise ValueError(f"Unsupported aggregation: {self.aggregation!r}")
        if self.fft_window_s is not None and self.fft_window_s <= 0:
            raise ValueError(f"fft_window_s must be positive, got {self.fft_window_s}")
        if self.averaging_window_s is not None and self.averaging_window_s <= 0:
            raise ValueError(f"averaging_window_s must be positive, got {self.averaging_window_s}")

        self.entity = str(self.entity or "").strip()
        if self.assumptions is None:
            self.assumptions = []
        else:
            dedup_a: list[str] = []
            for a in self.assumptions:
                _append_unique(dedup_a, str(a).strip())
            self.assumptions = dedup_a

        # Apply documented, transparent normalization assumptions for checkable rules
        if self.checkable and self.value is not None:
            from rules.units import is_per_minute_ramp_unit, parse_window_range_s

            # 1. Step/ramp/range limit with a stated window range ("4-6 second interval"):
            #    use the larger (more conservative) bound and record the range.
            if (
                self.constraint_type
                in (
                    ConstraintType.MAX_STEP.value,
                    ConstraintType.RAMP_RATE.value,
                    ConstraintType.DYNAMIC_RANGE.value,
                )
                and self.window_s is None
            ):
                win_range = parse_window_range_s(f"{self.snippet} {self.notes}")
                if win_range is not None:
                    lo_s, hi_s = win_range
                    self.window_s = hi_s
                    _append_unique(
                        self.assumptions,
                        f"Window range {lo_s:g}-{hi_s:g} s stated in source; using upper bound {hi_s:g} s.",
                    )

            # 2. Per-minute ramp rate (e.g. MW/min, %/min) with no stated window:
            #    use a 60 s rolling window and record as an assumption.
            if (
                self.constraint_type == ConstraintType.RAMP_RATE.value
                and self.window_s is None
                and is_per_minute_ramp_unit(self.unit)
            ):
                self.window_s = DEFAULT_PER_MINUTE_RAMP_WINDOW_S
                _append_unique(
                    self.assumptions,
                    "Assumed 60 s rolling window for per-minute ramp limit (no window stated in source).",
                )

            # 3. Sub-synchronous oscillation limit without explicit Hz bounds (e.g. IESO p.8):
            #    assume 5.0-55.0 Hz SSTI/SSCI sub-synchronous band and record as an assumption.
            unit_low = self.unit.strip().lower()
            is_pu_torque = unit_low in {"pu", "p.u.", "pu torque", "p.u. torque"}
            if (
                self.constraint_type == ConstraintType.FLUCTUATION_BAND.value
                and not is_pu_torque
                and self.freq_min_hz is None
                and self.freq_max_hz is None
                and _SUBSYNCHRONOUS_RE.search(f"{self.snippet} {self.notes}")
            ):
                lo_hz, hi_hz = DEFAULT_SUBSYNCHRONOUS_BAND_HZ
                self.freq_min_hz = lo_hz
                self.freq_max_hz = hi_hz
                _append_unique(
                    self.assumptions,
                    f"Sub-synchronous frequency band not numerically specified in source; assumed {lo_hz:g}-{hi_hz:g} Hz "
                    "(60 Hz SSTI/SSCI band covering IEEE FBM torsional modes 15.7-47.5 Hz).",
                )

            # 4. Frequency-band aggregation: infer from wording when present; else peak_bin + assumption.
            if self.constraint_type == ConstraintType.FLUCTUATION_BAND.value and not is_pu_torque:
                text = f"{self.snippet} {self.notes}".lower()
                if self.aggregation == AGGREGATION_PEAK_BIN:
                    if "sum of any two adjacent" in text or "two adjacent" in text:
                        self.aggregation = AGGREGATION_ADJACENT_PAIR_SUM
                    elif "sum of all bins" in text or "sum of all frequency bins" in text:
                        self.aggregation = AGGREGATION_BAND_SUM
                    else:
                        _append_unique(
                            self.assumptions,
                            "Frequency-band aggregation not stated; using peak_bin (largest single FFT bin).",
                        )
                # averaging window: prefer explicit averaging_window_s; else reuse window_s when stated as rolling avg
                if self.averaging_window_s is None and self.window_s is not None:
                    if "rolling" in text or "avg" in text or "average" in text or "p0-pk" in text:
                        self.averaging_window_s = float(self.window_s)
                # FFT window from notes like "10 s FFT window" / "1.0 s FFT"
                if self.fft_window_s is None:
                    import re as _re

                    m = _re.search(r"(\d+(?:\.\d+)?)\s*s(?:econd)?s?\s+fft\s+window", text)
                    if m:
                        self.fft_window_s = float(m.group(1))

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
            "entity",
            "window_s",
            "freq_min_hz",
            "freq_max_hz",
            "aggregation",
            "fft_window_s",
            "averaging_window_s",
            "source_section",
            "verified",
            "needs_human_review",
            "conflict_flag",
            "notes",
            "assumptions",
        }
        filtered = {k: v for k, v in data.items() if k in allowed}
        if "assumptions" in filtered and filtered["assumptions"] is not None:
            filtered["assumptions"] = list(filtered["assumptions"])
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

    def collect_assumptions(self) -> list[str]:
        out: list[str] = []
        for c in self.constraints:
            for a in c.assumptions:
                _append_unique(out, a)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "rated_mw": self.rated_mw,
            "assumptions": self.collect_assumptions(),
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
    """Deterministic pass/fail/not_evaluable outcome with margin for one constraint."""

    constraint_id: str
    constraint_type: str
    status: str  # "pass" | "fail" | "not_evaluable" | "uncheckable" | "needs_review" | "error"
    passed: bool | None  # None when status == "not_evaluable"
    measured: float | None
    limit: float | None
    margin: float | None  # limit - measured; >= 0 means compliant, < 0 means violation
    margin_pct: float | None
    canonical_unit: str
    page: int
    snippet: str
    confidence: float | None = None
    entity: str = ""
    details: str = ""
    assumptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ComplianceReport:
    """Aggregate deterministic compliance verification report (certificate backing)."""

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
    status: str = "pass"  # "pass" | "fail" | "not_evaluable" | "refused"
    evaluable_count: int = 0
    not_evaluable_count: int = 0
    assumptions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "passed": self.passed,
            "checkable_count": self.checkable_count,
            "evaluable_count": self.evaluable_count,
            "not_evaluable_count": self.not_evaluable_count,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "uncheckable_count": self.uncheckable_count,
            "needs_review_count": self.needs_review_count,
            "worst_margin_pct": self.worst_margin_pct,
            "worst_constraint_id": self.worst_constraint_id,
            "refusal_reason": self.refusal_reason,
            "assumptions": list(self.assumptions),
            "results": [r.to_dict() for r in self.results],
        }
