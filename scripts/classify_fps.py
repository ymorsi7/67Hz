#!/usr/bin/env python3
"""Classify the 9 extraction false positives vs human gold. Writes eval/results/fp_breakdown.json."""

from __future__ import annotations

import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eval.extraction import load_human_gold_files, _constraints_match
from rules.schema import Constraint

CACHE = REPO_ROOT / "eval" / "cache" / "extractions"
OUT = REPO_ROOT / "eval" / "results" / "fp_breakdown.json"


def _classify(doc_id: str, p: Constraint, gold: list[Constraint]) -> str:
    snip = (p.snippet or "").lower()
    # Cross-page duplicate of a gold constraint (same type/value, different page)
    for g in gold:
        if (
            g.constraint_type == p.constraint_type
            and p.value is not None
            and g.value is not None
            and abs(float(p.value) - float(g.value)) / max(abs(float(g.value)), 1e-9) <= 0.05
            and (p.unit or "").lower() == (g.unit or "").lower()
            and g.page != p.page
        ):
            return "duplicate"
    # Another utility's quoted comparative rule (MISO LLWG table / CAISO survey language)
    if any(k in snip for k in ("ercot", "atc", "aeso", "caiso", "lipa", "spp", "miso", "ieso")) and "miso" not in doc_id.lower():
        if "lipa" in snip and "Large-Load-Technical" in doc_id:
            return "duplicate"  # LIPA restated on CAISO p.25 vs gold p.23
    if "LLWG" in doc_id or "786296" in doc_id:
        # MISO slide comparing other utilities' ramp rates
        if p.page != 2:  # gold only keeps MISO's own 30 MW/min on p.2
            return "another_utility_quoted_rule"
    # Wrong constraint type / invented as checkable smoothing limit
    if p.constraint_type == "max_step" and p.value and float(p.value) >= 100:
        return "wrong_value_or_type"  # contingency load-loss, not POI smoothing max_step
    # Same type/value as gold but failed match (unit/page) — treat as duplicate-ish
    for g in gold:
        if g.constraint_type == p.constraint_type and p.value == g.value:
            return "duplicate"
    return "invented"


def main() -> int:
    rows = []
    for gdoc in load_human_gold_files():
        doc_id = str(gdoc["doc_id"])
        rated = float(gdoc.get("rated_mw", 100.0))
        gold = [Constraint.from_dict(x) for x in gdoc.get("constraints", []) if x.get("checkable", True)]
        cache = CACHE / f"{doc_id}.json"
        if not cache.exists():
            continue
        preds = [Constraint.from_dict(x) for x in json.loads(cache.read_text(encoding="utf-8")).get("constraints", [])]
        active = [
            c
            for c in preds
            if c.checkable and not (c.needs_human_review and c.verified is None) and c.verified is not False
        ]
        matched: set[int] = set()
        for p in active:
            found = False
            for gi, g in enumerate(gold):
                if gi in matched:
                    continue
                if _constraints_match(p, g, rated_mw=rated):
                    matched.add(gi)
                    found = True
                    break
            if not found:
                reason = _classify(doc_id, p, gold)
                rows.append(
                    {
                        "doc_id": doc_id,
                        "page": p.page,
                        "type": p.constraint_type,
                        "value": p.value,
                        "unit": p.unit,
                        "entity": p.entity or "",
                        "snippet": (p.snippet or "")[:200],
                        "why_not_in_gold": reason,
                    }
                )

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["why_not_in_gold"]] = counts.get(r["why_not_in_gold"], 0) + 1
    # Bucket for RESULTS: invented/wrong vs quotes/duplicates
    invented_or_wrong = sum(counts.get(k, 0) for k in ("invented", "wrong_value_or_type"))
    quotes_or_dupes = sum(counts.get(k, 0) for k in ("another_utility_quoted_rule", "duplicate"))
    payload = {
        "schema_version": "fp_breakdown_v1",
        "n": len(rows),
        "counts_by_reason": counts,
        "invented_or_wrong": invented_or_wrong,
        "quotes_or_duplicates": quotes_or_dupes,
        "false_positives": rows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"n": len(rows), "counts": counts, "invented_or_wrong": invented_or_wrong, "quotes_or_duplicates": quotes_or_dupes}, indent=2))
    for r in rows:
        print(f"- p.{r['page']} {r['type']} {r['value']} {r['unit']}: {r['why_not_in_gold']} | {r['doc_id'][:40]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
