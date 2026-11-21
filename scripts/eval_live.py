#!/usr/bin/env python3
"""Score cached final controllers on 20 freshly sampled workloads for a judge-picked seed.

Target: print a pass table in under 60 seconds. No LLM calls.
Reads controllers from eval/cache/phase3_results.json (and optional B2 results).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eval.random_suite import sample_random_suite_for_ruleset
from evolve.sandbox import evaluate_controller_sandboxed
from rules.profiles import load_utility_profiles

PHASE3_CACHE = REPO_ROOT / "eval" / "cache" / "phase3_results.json"
OUT_DIR = REPO_ROOT / "eval" / "results"


def _best_code(rec: dict, method: str) -> str | None:
    block = rec.get(method) or {}
    if method in ("uncontrolled", "b1"):
        return block.get("code")
    summary = block.get("summary") or {}
    return summary.get("best_code")


def main() -> int:
    p = argparse.ArgumentParser(description="Live random-suite score of cached controllers")
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--n", type=int, default=20)
    p.add_argument("--split", choices=["dev", "held_out", "all"], default="all")
    args = p.parse_args()

    t0 = time.perf_counter()
    if not PHASE3_CACHE.exists():
        print("ERROR: missing eval/cache/phase3_results.json — run Phase 3 baselines first")
        return 1

    p3 = json.loads(PHASE3_CACHE.read_text(encoding="utf-8"))
    profiles = {pr.profile_id: pr for pr in load_utility_profiles(rated_mw=100.0)}
    rows: list[dict] = []

    print(f"[eval-live] seed={args.seed} n={args.n} split={args.split}", flush=True)
    print(
        f"{'Profile':17s} | {'Split':8s} | {'Noop':10s} | {'B1':10s} | {'B0':10s} | {'B3':10s} | {'GridSafe':10s}",
        flush=True,
    )
    print("-" * 90, flush=True)

    for rec in p3.get("profiles", []):
        pid = rec["profile"]["profile_id"]
        split = rec["profile"]["split"]
        if args.split != "all" and split != args.split:
            continue
        prof = profiles.get(pid)
        if prof is None:
            continue
        suite = sample_random_suite_for_ruleset(prof.ruleset, n=args.n, seed=args.seed, cluster_mw=100.0)
        cells: dict[str, str] = {}
        detail: dict[str, dict] = {}
        for method, label in (
            ("uncontrolled", "Noop"),
            ("b1", "B1"),
            ("b0", "B0"),
            ("b3", "B3"),
            ("gridsafe", "GridSafe"),
        ):
            code = _best_code(rec, method)
            if not code:
                cells[label] = "n/a"
                continue
            ev = evaluate_controller_sandboxed(code, prof.ruleset, suite, timeout_s=20.0)
            cells[label] = f"{'PASS' if ev.passed else 'FAIL'} {ev.workloads_passed}/{ev.workloads_evaluable}"
            detail[label] = {
                "passed": ev.passed,
                "workloads_passed": ev.workloads_passed,
                "workloads_evaluable": ev.workloads_evaluable,
                "mean_energy_overhead_pct": round(ev.mean_energy_overhead_pct, 3),
                "mean_throughput_loss_pct": round(ev.mean_throughput_loss_pct, 3),
                "worst_margin_pct": ev.worst_margin_pct,
            }
        print(
            f"{pid:17s} | {split:8s} | {cells.get('Noop','n/a'):10s} | {cells.get('B1','n/a'):10s} | "
            f"{cells.get('B0','n/a'):10s} | {cells.get('B3','n/a'):10s} | {cells.get('GridSafe','n/a'):10s}",
            flush=True,
        )
        rows.append({"profile_id": pid, "split": split, "cells": cells, "detail": detail})

    elapsed = time.perf_counter() - t0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"eval_live_{args.seed}.json"
    out.write_text(
        json.dumps(
            {
                "schema_version": "eval_live_v1",
                "run_at_utc": datetime.now(timezone.utc).isoformat(),
                "seed": args.seed,
                "n": args.n,
                "split": args.split,
                "elapsed_s": round(elapsed, 3),
                "rows": rows,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\n[eval-live] elapsed={elapsed:.2f}s -> {out.relative_to(REPO_ROOT)}", flush=True)
    if elapsed >= 60.0:
        print("[eval-live] WARNING: exceeded 60s target", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
