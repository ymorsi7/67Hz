"""Per-utility rulebook profiles built from verified, checkable extracted constraints.

Pure Python + numpy only (sandbox-safe). Reads cached Reader + Verifier outputs
in eval/cache/extractions/*.json and constructs one RuleSet profile per utility
without ever mixing dev and held_out documents. New PDFs added to specs/ and
processed into eval/cache/extractions/ with split="held_out" are picked up
automatically without tuning.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rules.schema import Constraint, RuleSet

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXTRACTIONS_DIR = REPO_ROOT / "eval" / "cache" / "extractions"

# Ordered utility name patterns (matched against source_section then snippet)
_UTILITY_PATTERNS: list[tuple[str, str]] = [
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


@dataclass
class UtilityProfile:
    """Verified, checkable interconnection profile for a single utility."""

    profile_id: str
    utility: str
    split: str  # "dev" or "held_out"
    source_doc: str
    source_file: str
    ruleset: RuleSet
    pages: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "utility": self.utility,
            "split": self.split,
            "source_doc": self.source_doc,
            "source_file": self.source_file,
            "pages": list(self.pages),
            "ruleset": self.ruleset.to_dict(),
        }


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _infer_utility(constraint: Constraint, source_name: str, doc_id: str) -> str:
    """Assign a constraint to its originating utility from section/snippet/doc metadata."""
    sec = (constraint.source_section or "").strip()
    snip = (constraint.snippet or "").strip()

    # 1. Explicit utility match in source_section (most reliable for comparative docs)
    for utility, pat in _UTILITY_PATTERNS:
        if re.search(pat, sec, flags=re.IGNORECASE):
            return utility

    # 2. Explicit utility match at the start of snippet (e.g. "ERCOT: ...")
    head = snip[:60]
    for utility, pat in _UTILITY_PATTERNS:
        if re.search(pat, head, flags=re.IGNORECASE):
            return utility

    # 3. Document-level default utility (e.g. CAISO Straw Proposal main sections like "2.4. Ramp Rate Limit")
    doc_ctx = f"{source_name} {doc_id}"
    for utility, pat in _UTILITY_PATTERNS:
        if re.search(pat, doc_ctx, flags=re.IGNORECASE):
            return utility
    if "large-load-technical-requirements-straw-proposal" in doc_ctx.lower():
        return "CAISO"

    # 4. Fallback to cleaned doc_id for newly added utility-specific PDFs
    return doc_id


def _dedup_key(c: Constraint) -> tuple[Any, ...]:
    return (
        c.constraint_type,
        round(float(c.value), 6) if c.value is not None else None,
        c.unit.strip().lower(),
        round(float(c.window_s), 6) if c.window_s is not None else None,
        round(float(c.freq_min_hz), 6) if c.freq_min_hz is not None else None,
        round(float(c.freq_max_hz), 6) if c.freq_max_hz is not None else None,
    )


def load_utility_profiles(
    extractions_dir: str | Path = DEFAULT_EXTRACTIONS_DIR,
    *,
    split: str | None = None,
    rated_mw: float = 100.0,
) -> list[UtilityProfile]:
    """Build one UtilityProfile per (utility, source_doc) from verified checkable constraints.

    Only constraints with checkable=True, verified=True, and needs_human_review=False
    are included. Exact duplicate constraints within the same utility and document
    (e.g., LIPA limits repeated verbatim on pp. 23 & 25 of CAISO) are deduplicated
    while preserving the highest-confidence instance.
    """
    edir = Path(extractions_dir)
    if not edir.exists():
        return []

    profiles: list[UtilityProfile] = []
    seen_ids: set[str] = set()

    # Pass 1: collect dev-split utilities first so held_out comparative slides
    # do not duplicate utilities already evaluated in dev (except 2601.12686v1 Southern,
    # which is explicitly preserved as held_out).
    dev_utilities: set[str] = set()
    all_docs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(edir.glob("*.json"))]
    for data in all_docs:
        if str(data.get("split", "held_out")) != "dev":
            continue
        doc_id = str(data.get("doc_id", ""))
        source_name = str(data.get("source_name", f"{doc_id}.pdf"))
        for raw_c in data.get("constraints", []):
            c = Constraint.from_dict(raw_c)
            if c.checkable and c.value is not None and c.verified is True and not c.needs_human_review:
                dev_utilities.add(_infer_utility(c, source_name, doc_id))

    for json_path in sorted(edir.glob("*.json")):
        data = json.loads(json_path.read_text(encoding="utf-8"))
        doc_split = str(data.get("split", "held_out"))
        if split is not None and doc_split != split:
            continue

        doc_id = str(data.get("doc_id", json_path.stem))
        source_name = str(data.get("source_name", f"{doc_id}.pdf"))
        source_file = str(data.get("source_file", f"specs/{source_name}"))

        grouped: dict[str, list[Constraint]] = {}
        for raw_c in data.get("constraints", []):
            c = Constraint.from_dict(raw_c)
            if not c.checkable or c.value is None:
                continue
            if c.verified is not True or c.needs_human_review:
                continue
            # Clear cross-utility conflict flags that came from comparing utilities inside one doc
            clean_c = Constraint.from_dict(c.to_dict())
            clean_c.conflict_flag = False
            if "[CONFLICT:" in clean_c.notes:
                clean_c.notes = clean_c.notes.split("[CONFLICT:")[0].strip()

            util = _infer_utility(clean_c, source_name, doc_id)
            if doc_split == "held_out" and util in dev_utilities and "2601.12686" not in source_name:
                continue
            grouped.setdefault(util, []).append(clean_c)

        # Deterministic ordering: CAISO first, then ERCOT, ATC, AESO, Southern, LIPA, then alphabetical
        preferred_order = ["CAISO", "ERCOT", "ATC", "AESO", "Southern", "LIPA"]
        ordered_utils = sorted(
            grouped.keys(),
            key=lambda u: (preferred_order.index(u) if u in preferred_order else 999, u.lower()),
        )

        for util in ordered_utils:
            raw_list = grouped[util]
            # Deduplicate identical numeric rules within this utility profile (keep earliest page, max confidence)
            deduped: dict[tuple[Any, ...], Constraint] = {}
            for c in raw_list:
                key = _dedup_key(c)
                prev = deduped.get(key)
                if prev is None:
                    deduped[key] = c
                else:
                    if (c.confidence or 0.0) > (prev.confidence or 0.0):
                        # Keep higher confidence while noting earlier page if needed
                        deduped[key] = c

            constraints = list(deduped.values())
            if not constraints:
                continue

            base_slug = _slug(util)
            if doc_split == "held_out":
                pid = f"{base_slug}_held_out"
            elif base_slug == "southern":
                pid = "southern_dev"
            elif base_slug == "lipa" and "pseg" in source_name.lower() or "connectedlipasystem" in source_name.lower():
                pid = "lipa_pseg"
            else:
                pid = base_slug
            if pid in seen_ids:
                pid = f"{pid}_{_slug(doc_id)[:16]}"
            seen_ids.add(pid)

            pages = sorted({int(c.page) for c in constraints if c.page})
            ruleset = RuleSet(
                source_name=f"{util} ({source_name})",
                rated_mw=float(rated_mw),
                constraints=constraints,
            )
            profiles.append(
                UtilityProfile(
                    profile_id=pid,
                    utility=util,
                    split=doc_split,
                    source_doc=source_name,
                    source_file=source_file,
                    ruleset=ruleset,
                    pages=pages,
                )
            )

    # Sort dev profiles before held_out profiles
    profiles.sort(key=lambda p: (0 if p.split == "dev" else 1, p.profile_id))
    return profiles
