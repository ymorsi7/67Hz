"""Dev-split-only Verifier calibration & degradation probe.

Uses ONLY dev-split documents (CAISO + ESIG; NEVER touches held-out 2601.12686v1.pdf)
to measure:
  1. Verifier confidence distribution on true dev constraints (clean pages).
  2. Verifier response on fabricated / unit-swapped hallucinations (negative controls).
  3. Reader + Verifier behavior across degradation severities 1..4 on CAISO p.13 & p.23.
  4. Reader zero-constraint behavior on a clean no-limits page (CAISO p.1).

Saves recorded responses to eval/cache/dev_calibration.json for offline smoke tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from eval.degrade import degrade_image
from reader.extract import extract_from_image_bytes
from reader.pdf_utils import render_pdf_page_png
from reader.verify import verify_page_constraints
from rules.schema import Constraint

CAISO_PDF = REPO_ROOT / "specs" / "Large-Load-Technical-Requirements-Straw-Proposal-Jun-15-2026.pdf"
CACHE_DIR = REPO_ROOT / "eval" / "cache"


def main() -> int:
    if not CAISO_PDF.exists():
        print(f"Missing {CAISO_PDF}; run `make fetch-specs`.")
        return 1

    out: dict[str, Any] = {
        "split": "dev_only",
        "source_pdf": CAISO_PDF.name,
        "no_limits_page": {},
        "hallucination_controls": [],
        "degradation_sweep": [],
    }

    # 1. Clean page with NO numeric limits (CAISO page 1: cover page)
    print("[1/3] Testing no-limits page (CAISO p.1)...", flush=True)
    p1_png = render_pdf_page_png(CAISO_PDF, 1, dpi=150)
    p1_ext = extract_from_image_bytes(p1_png, source_name="caiso_p1_cover", page_number=1)
    out["no_limits_page"] = {
        "page": 1,
        "constraints_found": len(p1_ext.constraints),
        "checkable_found": sum(1 for c in p1_ext.constraints if c.checkable),
        "unreadable": p1_ext.unreadable,
        "summary": p1_ext.summary,
    }
    print(f"  CAISO p.1 -> constraints={len(p1_ext.constraints)}, checkable={out['no_limits_page']['checkable_found']}")

    # 2. Hallucination / wrong-unit negative controls on CAISO p.13 (actual rule: 20 MW/min over 10-min rolling)
    print("[2/3] Testing Verifier negative controls on CAISO p.13...", flush=True)
    p13_png = render_pdf_page_png(CAISO_PDF, 13, dpi=150)
    controls = [
        Constraint(
            id="true_20mw_min",
            constraint_type="ramp_rate",
            value=20.0,
            unit="MW/min",
            page=13,
            snippet="The average active power ramp rate of the large load, measured over a rolling 10-minute interval, shall not exceed 20 MW/min.",
            checkable=True,
            window_s=600.0,
        ),
        Constraint(
            id="halluc_wrong_value_50mw_min",
            constraint_type="ramp_rate",
            value=50.0,
            unit="MW/min",
            page=13,
            snippet="The average active power ramp rate of the large load shall not exceed 50 MW/min.",
            checkable=True,
            window_s=600.0,
        ),
        Constraint(
            id="halluc_wrong_unit_20mw_s",
            constraint_type="ramp_rate",
            value=20.0,
            unit="MW/s",
            page=13,
            snippet="The average active power ramp rate of the large load shall not exceed 20 MW/s.",
            checkable=True,
        ),
        Constraint(
            id="halluc_invented_max_step",
            constraint_type="max_step",
            value=5.0,
            unit="MW",
            page=13,
            snippet="Instantaneous active power steps shall not exceed 5 MW.",
            checkable=True,
        ),
    ]
    ver_items, p13_summary = verify_page_constraints(13, p13_png, controls, threshold=0.80)
    for item in ver_items:
        out["hallucination_controls"].append(item.to_dict())
        print(
            f"  {item.constraint_id:28s} | p_yes={item.p_yes:.4f} | conf={item.confidence:.4f} "
            f"| readable={item.page_readable_p:.4f} | status={item.status}"
        )

    # 3. Degradation sweep (severities 1..4) on CAISO p.13
    print("[3/3] Running degradation sweep (severities 1..4) on CAISO p.13...", flush=True)
    for sev in (1, 2, 3, 4):
        deg_png, deg_spec = degrade_image(p13_png, severity=sev, seed=42)
        deg_ext = extract_from_image_bytes(
            deg_png,
            source_name=f"caiso_p13_sev{sev}",
            page_number=13,
            thinking_level="medium",
        )
        # Verify BOTH what Neon extracted AND the known true constraint against the degraded image
        probe_true = Constraint(
            id=f"true_p13_sev{sev}",
            constraint_type="ramp_rate",
            value=20.0,
            unit="MW/min",
            page=13,
            snippet="The average active power ramp rate of the large load, measured over a rolling 10-minute interval, shall not exceed 20 MW/min.",
            checkable=True,
            window_s=600.0,
        )
        to_verify = list(deg_ext.constraints) + [probe_true]
        v_items, v_sum = verify_page_constraints(13, deg_png, to_verify, threshold=0.80)

        entry = {
            "severity": sev,
            "spec": deg_spec.to_dict(),
            "reader_unreadable": deg_ext.unreadable,
            "reader_extracted_count": len(deg_ext.constraints),
            "reader_extracted": [c.to_dict() for c in deg_ext.constraints],
            "page_readable_p": v_sum.page_readable_p,
            "verifications": [v.to_dict() for v in v_items],
        }
        out["degradation_sweep"].append(entry)
        probe_v = v_items[-1]
        print(
            f"  Severity {sev}: reader_unreadable={deg_ext.unreadable} | extracted={len(deg_ext.constraints)} "
            f"| page_readable_p={v_sum.page_readable_p:.4f} | true_probe p_yes={probe_v.p_yes:.4f} "
            f"conf={probe_v.confidence:.4f} ({probe_v.status})"
        )

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CACHE_DIR / "dev_calibration.json"
    out_path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"\nSaved dev calibration record -> {out_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
