"""Per-utility rulebook profiles built from verified, checkable extracted constraints.

Pure Python + numpy only (sandbox-safe). Reads cached Reader + Verifier outputs
in eval/cache/extractions/*.json and constructs one RuleSet profile per utility
without ever mixing dev and held_out documents. Backfills explicit `entity`
attribution, deduplicates limits stated across multiple pages, and preserves
all documented checker assumptions (60 s per-minute ramp window, window ranges,
sub-synchronous Hz band).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from repo_paths import find_repo_root
from typing import Any

from rules.schema import Constraint, RuleSet, canonicalize_entity
from rules.units import UnitConversionError, normalize_constraint_limit

REPO_ROOT = find_repo_root()
DEFAULT_EXTRACTIONS_DIR = REPO_ROOT / "eval" / "cache" / "extractions"

_CONFLICT_NOTE_RE = re.compile(r"\s*\[CONFLICT[^\]]*\]", flags=re.IGNORECASE)


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
            "assumptions": self.ruleset.collect_assumptions(),
            "ruleset": self.ruleset.to_dict(),
        }


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _infer_utility(constraint: Constraint, source_name: str, doc_id: str) -> str:
    """Assign a constraint to its originating utility entity."""
    ent = canonicalize_entity(
        entity=constraint.entity,
        source_section=constraint.source_section,
        snippet=constraint.snippet,
        doc_context=f"{source_name} {doc_id}",
    )
    return ent if ent else doc_id


def _dedup_key(c: Constraint, rated_mw: float) -> tuple[Any, ...]:
    try:
        norm_val, canon_unit = normalize_constraint_limit(c, rated_mw=rated_mw)
        val_key = round(float(norm_val), 4)
    except UnitConversionError:
        canon_unit = c.unit.strip().lower()
        val_key = round(float(c.value), 4) if c.value is not None else None
    return (
        c.entity.strip().lower(),
        c.constraint_type,
        val_key,
        canon_unit,
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
    are included. Exact/equivalent numeric constraints repeated across multiple pages
    for the same entity (e.g., LIPA limits repeated on pp. 23 & 25 of CAISO, or MISO
    30 MW/min on pp. 2 & 8) are deduplicated into one constraint while preserving
    additional page citations.
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
            clean_c = Constraint.from_dict(c.to_dict())
            clean_c.conflict_flag = False
            clean_c.notes = _CONFLICT_NOTE_RE.sub("", clean_c.notes).strip()

            util = _infer_utility(clean_c, source_name, doc_id)
            clean_c.entity = util
            if doc_split == "held_out" and util in dev_utilities and "2601.12686" not in source_name:
                continue
            grouped.setdefault(util, []).append(clean_c)

        preferred_order = ["CAISO", "ERCOT", "ATC", "AESO", "Southern", "LIPA"]
        ordered_utils = sorted(
            grouped.keys(),
            key=lambda u: (preferred_order.index(u) if u in preferred_order else 999, u.lower()),
        )

        for util in ordered_utils:
            raw_list = grouped[util]
            deduped: dict[tuple[Any, ...], Constraint] = {}
            for c in raw_list:
                key = _dedup_key(c, rated_mw=rated_mw)
                prev = deduped.get(key)
                if prev is None:
                    deduped[key] = c
                else:
                    if c.page != prev.page:
                        also = f"Also stated on p.{c.page}."
                        if also not in prev.notes:
                            prev.notes = f"{prev.notes} {also}".strip()
                    if (c.confidence or 0.0) > (prev.confidence or 0.0):
                        # Keep higher confidence while retaining earliest page citation
                        merged_notes = prev.notes
                        if prev.page != c.page:
                            also_prev = f"Also stated on p.{prev.page}."
                            if also_prev not in c.notes:
                                c.notes = f"{c.notes} {also_prev}".strip()
                        elif merged_notes and not c.notes:
                            c.notes = merged_notes
                        deduped[key] = c

            constraints = list(deduped.values())
            if not constraints:
                continue

            base_slug = _slug(util)
            if doc_split == "held_out":
                pid = f"{base_slug}_held_out"
            elif base_slug == "southern":
                pid = "southern_dev"
            elif (base_slug == "lipa" and "pseg" in source_name.lower()) or "connectedlipasystem" in source_name.lower():
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

    profiles.sort(key=lambda p: (0 if p.split == "dev" else 1, p.profile_id))
    return profiles
