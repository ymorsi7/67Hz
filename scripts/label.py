"""Interactive human gold-labeling CLI for GridSafe extraction evaluation.

Displays each candidate constraint extracted by the Reader (with page number,
verbatim snippet, type, value, unit, and Verifier confidence) so a human can:
  [a] Accept as-is
  [e] Edit fields before accepting
  [r] Reject (false positive / hallucination)
  [s] Skip remaining and jump to manual additions

Then lets the human add any constraints the Reader missed, and saves the gold
file to eval/gold/<doc_id>.json with `"labeled_by": "human"`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from rules.schema import CHECKABLE_CONSTRAINT_TYPES, Constraint, ConstraintType

CACHE_DIR = REPO_ROOT / "eval" / "cache" / "extractions"
GOLD_DIR = REPO_ROOT / "eval" / "gold"

_CONFLICT_NOTE_RE = re.compile(r"\s*\[CONFLICT[^\]]*\]", flags=re.IGNORECASE)


def strip_conflict_notes(notes: str) -> str:
    """Remove auto-generated [CONFLICT: ...] annotations before saving gold labels."""
    cleaned = _CONFLICT_NOTE_RE.sub("", notes or "")
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def filter_candidates(candidates: list[dict[str, Any]], checkable_only: bool) -> list[dict[str, Any]]:
    """Filter candidate constraints to checkable types when --checkable-only is set."""
    if not checkable_only:
        return list(candidates)
    return [
        c
        for c in candidates
        if str(c.get("constraint_type", "")).strip().lower() in CHECKABLE_CONSTRAINT_TYPES
    ]


def _prompt_optional_float(label: str, current: float | None) -> float | None:
    cur_str = "" if current is None else str(current)
    raw = input(f"  {label} [{cur_str}] (enter 'none' to clear): ").strip()
    if not raw:
        return current
    if raw.lower() in {"none", "null", "n/a"}:
        return None
    return float(raw)


def _prompt_str(label: str, current: str) -> str:
    raw = input(f"  {label} [{current}]: ").strip()
    return raw if raw else current


def _prompt_bool(label: str, current: bool) -> bool:
    cur_str = "y" if current else "n"
    raw = input(f"  {label} (y/n) [{cur_str}]: ").strip().lower()
    if not raw:
        return current
    return raw in {"y", "yes", "true", "1"}


def _edit_constraint(c: dict[str, Any]) -> dict[str, Any]:
    valid_types = [t.value for t in ConstraintType]
    print(f"  Valid types: {', '.join(valid_types)}")
    ctype = _prompt_str("constraint_type", str(c.get("constraint_type", "other"))).lower()
    while ctype not in valid_types:
        print(f"  Invalid type '{ctype}'. Choose from: {', '.join(valid_types)}")
        ctype = _prompt_str("constraint_type", "other").lower()

    value = _prompt_optional_float("value", c.get("value"))
    unit = _prompt_str("unit", str(c.get("unit", "")))
    page = int(_prompt_str("page (1-indexed)", str(c.get("page", 1))))
    snippet = _prompt_str("snippet", str(c.get("snippet", "")))
    checkable = _prompt_bool("checkable", bool(c.get("checkable", True)))
    window_s = _prompt_optional_float("window_s", c.get("window_s"))
    freq_min_hz = _prompt_optional_float("freq_min_hz", c.get("freq_min_hz"))
    freq_max_hz = _prompt_optional_float("freq_max_hz", c.get("freq_max_hz"))
    source_section = _prompt_str("source_section", str(c.get("source_section", "")))
    entity = _prompt_str("entity", str(c.get("entity", "")))
    notes = strip_conflict_notes(_prompt_str("notes", strip_conflict_notes(str(c.get("notes", "")))))

    validated = Constraint(
        id=str(c.get("id", "")),
        constraint_type=ctype,
        value=value,
        unit=unit,
        page=page,
        snippet=snippet,
        checkable=checkable,
        entity=entity,
        window_s=window_s,
        freq_min_hz=freq_min_hz,
        freq_max_hz=freq_max_hz,
        source_section=source_section,
        notes=notes,
    )
    return validated.to_dict()


def _add_manual_constraint(next_idx: int) -> dict[str, Any]:
    print("\n--- Add constraint missed by Reader ---")
    blank = {
        "id": f"manual_{next_idx}",
        "constraint_type": "ramp_rate",
        "value": None,
        "unit": "",
        "page": 1,
        "snippet": "",
        "checkable": True,
        "window_s": None,
        "freq_min_hz": None,
        "freq_max_hz": None,
        "source_section": "",
        "notes": "added manually by human",
    }
    return _edit_constraint(blank)


def main() -> int:
    parser = argparse.ArgumentParser(description="Human gold-labeling CLI for GridSafe")
    parser.add_argument(
        "candidate_json",
        nargs="?",
        help="Path to a cached extraction JSON in eval/cache/extractions/ (interactive menu if omitted)",
    )
    parser.add_argument("--split", choices=["dev", "held_out"], default="dev")
    parser.add_argument("--rated-mw", type=float, default=100.0)
    parser.add_argument(
        "--checkable-only",
        action="store_true",
        help="Show only checkable-type candidates (ramp_rate, max_step, fluctuation_band, dynamic_range), then manual additions",
    )
    args = parser.parse_args()

    if args.candidate_json:
        cand_path = Path(args.candidate_json)
    else:
        available = sorted(CACHE_DIR.glob("*.json"))
        if not available:
            print(f"No cached extraction runs found in {CACHE_DIR}. Run scripts/run_specs_reader.py first.")
            return 1
        print("Available cached extraction runs:")
        for idx, p in enumerate(available, start=1):
            print(f"  [{idx}] {p.name}")
        choice = int(input("Select document number: ").strip())
        cand_path = available[choice - 1]

    data = json.loads(cand_path.read_text(encoding="utf-8"))
    doc_id = cand_path.stem
    source_file = data.get("source_file", f"specs/{data.get('source_name', doc_id + '.pdf')}")
    all_candidates: list[dict[str, Any]] = data.get("constraints", [])
    candidates = filter_candidates(all_candidates, checkable_only=args.checkable_only)

    if args.checkable_only:
        print(
            f"\n=== Labeling {doc_id} ({len(candidates)} checkable-type candidate(s) "
            f"out of {len(all_candidates)} total) ==="
        )
    else:
        print(f"\n=== Labeling {doc_id} ({len(candidates)} candidate constraint(s)) ===")
    accepted: list[dict[str, Any]] = []

    for idx, c in enumerate(candidates, start=1):
        print(f"\n[{idx}/{len(candidates)}] id={c.get('id')} | page={c.get('page')} | type={c.get('constraint_type')}")
        print(f"  value={c.get('value')} {c.get('unit')} | checkable={c.get('checkable')} | section={c.get('source_section')!r}")
        print(f"  verifier: verified={c.get('verified')} | conf={c.get('confidence')} | review={c.get('needs_human_review')}")
        print(f"  snippet: \"{c.get('snippet')}\"")
        clean_notes = strip_conflict_notes(str(c.get("notes", "")))
        if clean_notes:
            print(f"  notes: {clean_notes}")

        while True:
            cmd = input("Action [a=accept, e=edit, r=reject, s=stop reviewing]: ").strip().lower()
            if cmd == "a":
                accepted.append(c)
                print("  -> Accepted.")
                break
            if cmd == "e":
                edited = _edit_constraint(c)
                accepted.append(edited)
                print("  -> Edited and accepted.")
                break
            if cmd == "r":
                print("  -> Rejected.")
                break
            if cmd == "s":
                print("  -> Stopped candidate review.")
                break
            print("  Please enter a, e, r, or s.")
        if cmd == "s":
            break

    while True:
        more = input("\nAdd a constraint the Reader missed? (y/n) [n]: ").strip().lower()
        if more not in {"y", "yes"}:
            break
        accepted.append(_add_manual_constraint(len(accepted)))

    # Re-index cleanly and strip model-side confidence/verified fields and [CONFLICT] notes from gold truth
    gold_constraints: list[dict[str, Any]] = []
    for idx, c in enumerate(accepted):
        entry = {
            "id": f"{c['constraint_type']}_{idx}",
            "constraint_type": c["constraint_type"],
            "value": c.get("value"),
            "unit": c.get("unit", ""),
            "page": int(c["page"]),
            "snippet": c.get("snippet", ""),
            "checkable": bool(c.get("checkable", False)),
            "entity": str(c.get("entity", "")),
            "window_s": c.get("window_s"),
            "freq_min_hz": c.get("freq_min_hz"),
            "freq_max_hz": c.get("freq_max_hz"),
            "source_section": c.get("source_section", ""),
            "notes": strip_conflict_notes(str(c.get("notes", ""))),
            "assumptions": list(c.get("assumptions") or []),
        }
        gold_constraints.append(entry)

    GOLD_DIR.mkdir(parents=True, exist_ok=True)
    out_path = GOLD_DIR / f"{doc_id}.json"
    payload = {
        "doc_id": doc_id,
        "source_file": source_file,
        "split": args.split,
        "rated_mw": args.rated_mw,
        "labeled_by": "human",
        "labeled_at_utc": datetime.now(timezone.utc).isoformat(),
        "notes": "Labeled manually via scripts/label.py",
        "constraints": gold_constraints,
    }
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nSaved {len(gold_constraints)} human-labeled gold constraint(s) to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
