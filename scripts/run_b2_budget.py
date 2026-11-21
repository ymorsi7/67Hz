#!/usr/bin/env python3
"""Run Baseline B2 (stock Antigravity) across profiles until a wall-clock budget expires.

Saves whatever completed within the budget to eval/results/b2_budget_<seed>.json.
No skills, hooks, custom functions, or in-loop checker (host scores after the fact).
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

from evolve.b2 import run_b2_baseline
from evolve.baselines import resolve_pdf_path
from rules.profiles import load_utility_profiles
from workload.synth import all_preset_workloads

OUT_DIR = REPO_ROOT / "eval" / "results"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--seed", type=int, default=20261121)
    p.add_argument("--budget-s", type=float, default=480.0, help="Wall-clock budget seconds (default 8 min)")
    p.add_argument("--split", choices=["dev", "held_out", "all"], default="all")
    p.add_argument("--timeout-s", type=float, default=240.0, help="Per-profile Antigravity timeout")
    args = p.parse_args()

    profiles = load_utility_profiles(rated_mw=100.0)
    if args.split != "all":
        profiles = [x for x in profiles if x.split == args.split]
    # Prefer native LIPA aggregations; keep CAISO-straw lipa after lipa_pseg.
    order = {pid: i for i, pid in enumerate(["lipa_pseg", "southern_dev", "ercot", "ieso_held_out", "aeso", "atc", "caiso", "miso_held_out", "spp_held_out", "southern_held_out", "lipa"])}
    profiles = sorted(profiles, key=lambda x: order.get(x.profile_id, 99))

    workloads = all_preset_workloads(100.0)
    t0 = time.perf_counter()
    deadline = t0 + float(args.budget_s)
    runs: list[dict] = []
    skipped: list[str] = []

    print(f"[b2] budget={args.budget_s:.0f}s seed={args.seed} profiles={len(profiles)}", flush=True)
    for prof in profiles:
        remaining = deadline - time.perf_counter()
        if remaining < 30.0:
            skipped.append(prof.profile_id)
            print(f"[b2] stop — {remaining:.1f}s left; skip {prof.profile_id}", flush=True)
            break
        pdf = resolve_pdf_path(REPO_ROOT / prof.source_file)
        per_timeout = min(float(args.timeout_s), max(30.0, remaining - 5.0))
        print(f"[b2] start {prof.profile_id} ({prof.split}) timeout={per_timeout:.0f}s", flush=True)
        try:
            res = run_b2_baseline(
                prof.ruleset,
                workloads,
                pdf_path=pdf,
                timeout_s=per_timeout,
                utility_name=prof.utility,
            )
            entry = {
                "profile_id": prof.profile_id,
                "split": prof.split,
                "utility": prof.utility,
                **res.to_dict(),
            }
            runs.append(entry)
            passed = bool(res.eval_result and res.eval_result.passed)
            print(
                f"[b2] done  {prof.profile_id} elapsed={res.elapsed_s:.1f}s "
                f"pass={passed} notes={res.notes[:80]}",
                flush=True,
            )
        except Exception as exc:
            runs.append(
                {
                    "profile_id": prof.profile_id,
                    "split": prof.split,
                    "utility": prof.utility,
                    "baseline": "B2",
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_s": round(time.perf_counter() - t0, 3),
                    "eval_result": None,
                }
            )
            print(f"[b2] ERROR {prof.profile_id}: {type(exc).__name__}: {exc}", flush=True)
            # Continue to next profile if time remains; abort on hard rate-limit language.
            if "429" in str(exc) or "rate" in str(exc).lower():
                skipped.extend([x.profile_id for x in profiles if x.profile_id not in {r["profile_id"] for r in runs}])
                break

    elapsed = time.perf_counter() - t0
    payload = {
        "schema_version": "b2_budget_v1",
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "seed": args.seed,
        "budget_s": args.budget_s,
        "elapsed_s": round(elapsed, 3),
        "profiles_attempted": len(runs),
        "profiles_completed_ok": sum(1 for r in runs if r.get("eval_result") is not None),
        "profiles_passed": sum(1 for r in runs if (r.get("eval_result") or {}).get("passed")),
        "skipped_profile_ids": skipped + [p.profile_id for p in profiles if p.profile_id not in {r["profile_id"] for r in runs} and p.profile_id not in skipped],
        "runs": runs,
    }
    # de-dup skipped
    payload["skipped_profile_ids"] = sorted(set(payload["skipped_profile_ids"]))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"b2_budget_{args.seed}.json"
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        f"[b2] finished attempted={payload['profiles_attempted']} "
        f"scored={payload['profiles_completed_ok']} passed={payload['profiles_passed']} "
        f"elapsed={elapsed:.1f}s -> {out.relative_to(REPO_ROOT)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
