"""Run live Reader (gemini-4-neon) + Verifier (gemini-decision-1) on all PDFs in specs/.

Caches full recorded outputs to eval/cache/extractions/<pdf_stem>.json (used by
scripts/label.py, offline smoke tests, and Replay), prints per-document summary
tables, and prints the dev-split confidence distribution for threshold calibration.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from reader.extract import extract_from_pdf
from reader.verify import verify_constraints

SPECS_DIR = REPO_ROOT / "specs"
CACHE_DIR = REPO_ROOT / "eval" / "cache" / "extractions"

# Split definition: held_out is NEVER used for choosing the abstention threshold.
DOC_SPLITS: dict[str, str] = {
    "LL-Performance-Requirements-Framework-and-Methodology_v2.pdf": "dev",
    "Large-Load-Technical-Requirements-Straw-Proposal-Jun-15-2026.pdf": "dev",
    "2601.12686v1.pdf": "held_out",
}


def run_all_specs(threshold: float | None = None) -> list[dict[str, Any]]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(SPECS_DIR.glob("*.pdf"))
    if not pdfs:
        print("No PDFs found in specs/. Run `make fetch-specs` first.")
        return []

    records: list[dict[str, Any]] = []
    for pdf in pdfs:
        split = DOC_SPLITS.get(pdf.name, "held_out")
        group_by_sec = "2601.12686" in pdf.name  # Multi-utility comparative paper
        print(f"\n=== [{split}] Reading {pdf.name} ===", flush=True)
        t0 = time.perf_counter()
        ext = extract_from_pdf(
            pdf,
            rated_mw=100.0,
            chunk_pages=12,
            resolution="medium",
            group_conflicts_by_section=group_by_sec,
        )
        t_read = time.perf_counter() - t0
        print(
            f"  Reader ({ext.model}): {len(ext.constraints)} candidate(s) "
            f"({sum(1 for c in ext.constraints if c.checkable)} checkable, "
            f"{sum(1 for c in ext.constraints if not c.checkable)} uncheckable) in {t_read:.1f}s",
            flush=True,
        )

        t1 = time.perf_counter()
        vrep = verify_constraints(
            ext.constraints,
            pdf_path=pdf,
            threshold=threshold,
            dpi=150,
            resolution="high",
        )
        t_ver = time.perf_counter() - t1
        print(
            f"  Verifier ({vrep.verifier_model}, {len(vrep.pages)} page call(s)): "
            f"verified={vrep.verified_count}, abstained={vrep.abstained_count}, "
            f"rejected={vrep.rejected_count} in {t_ver:.1f}s",
            flush=True,
        )

        record = {
            "doc_id": pdf.stem,
            "source_name": pdf.name,
            "source_file": f"specs/{pdf.name}",
            "split": split,
            "run_at_utc": datetime.now(timezone.utc).isoformat(),
            "reader_model": ext.model,
            "verifier_model": vrep.verifier_model,
            "verifier_fallback_used": vrep.fallback_used,
            "total_pages": ext.total_pages,
            "read_time_s": round(t_read, 2),
            "verify_time_s": round(t_ver, 2),
            "summary": ext.summary,
            "constraints": [c.to_dict() for c in ext.constraints],
            "verification": vrep.to_dict(),
        }

        out_path = CACHE_DIR / f"{pdf.stem}.json"
        out_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(f"  Cached -> {out_path.relative_to(REPO_ROOT)}", flush=True)
        records.append(record)

    return records


def print_summary(records: list[dict[str, Any]]) -> None:
    print("\n=== Per-Document Reader + Verifier Summary ===")
    header = (
        f"{'Document':58s} | {'Split':8s} | {'Total':5s} | {'Check':5s} | "
        f"{'Verif':5s} | {'Abst':4s} | {'Rej':3s} | {'Unchk':5s}"
    )
    print(header)
    print("-" * len(header))
    for r in records:
        cs = r["constraints"]
        v = r["verification"]
        checkable = sum(1 for c in cs if c["checkable"])
        uncheckable = sum(1 for c in cs if not c["checkable"])
        print(
            f"{r['source_name'][:58]:58s} | {r['split']:8s} | {len(cs):5d} | {checkable:5d} | "
            f"{v['verified_count']:5d} | {v['abstained_count']:4d} | {v['rejected_count']:3d} | {uncheckable:5d}"
        )

    # Confidence distribution on DEV split only
    dev_items = [
        item
        for r in records
        if r["split"] == "dev"
        for item in r["verification"]["items"]
    ]
    print(f"\n=== Dev Split Confidence Distribution (n={len(dev_items)}) ===")
    if dev_items:
        p_yes_vals = sorted(i["p_yes"] for i in dev_items)
        conf_vals = sorted(i["confidence"] for i in dev_items)
        print("  p_yes values:", [round(x, 3) for x in p_yes_vals])
        print("  conf  values:", [round(x, 3) for x in conf_vals])
        for bucket_lo, bucket_hi in [(0.50, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 0.95), (0.95, 1.00)]:
            cnt = sum(1 for x in conf_vals if bucket_lo <= x < bucket_hi or (bucket_hi == 1.0 and x == 1.0))
            print(f"  [{bucket_lo:.2f}, {bucket_hi:.2f}]: {cnt}")


if __name__ == "__main__":
    recs = run_all_specs()
    print_summary(recs)
