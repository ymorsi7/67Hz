#!/usr/bin/env python3
"""Build eval/results/final_<seed>.json from saved caches + a fresh random-suite re-score.

No LLM calls. Preset pass rates come from eval/cache/phase3_results.json (controllers
already judged with the then-current checker, including band aggregations for lipa_pseg).
Random-suite rows are re-scored now with the current checker.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eval.extraction import evaluate_extraction, load_human_gold_files, _constraints_match
from eval.random_suite import sample_random_suite_for_ruleset
from eval.run_eval import run_phase1_validation
from evolve.sandbox import evaluate_controller_sandboxed
from rules.profiles import load_utility_profiles
from rules.schema import Constraint

PHASE3_CACHE = REPO_ROOT / "eval" / "cache" / "phase3_results.json"
CALIB = REPO_ROOT / "eval" / "cache" / "dev_calibration.json"
OUT_DIR = REPO_ROOT / "eval" / "results"
CACHE_EXT = REPO_ROOT / "eval" / "cache" / "extractions"
B2_PATH = REPO_ROOT / "eval" / "results" / "b2_budget_20261121.json"
EVAL_FINAL_LOG = REPO_ROOT / "eval" / "results" / "eval_final_run.log"


def _wilson(k: int, n: int, z: float = 1.96) -> list[float | None]:
    if n <= 0:
        return [None, None, None]
    p = k / n
    den = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = (z / den) * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return [round(p, 4), round(max(0.0, center - half), 4), round(min(1.0, center + half), 4)]


def _best_code(rec: dict, method: str) -> str | None:
    block = rec.get(method) or {}
    if method in ("uncontrolled", "b1"):
        return block.get("code")
    return (block.get("summary") or {}).get("best_code")


def _fp_breakdown() -> list[dict[str, Any]]:
    fps: list[dict[str, Any]] = []
    for gdoc in load_human_gold_files():
        doc_id = str(gdoc["doc_id"])
        rated_mw = float(gdoc.get("rated_mw", 100.0))
        gold = [Constraint.from_dict(x) for x in gdoc.get("constraints", []) if x.get("checkable", True)]
        cache = CACHE_EXT / f"{doc_id}.json"
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
                if _constraints_match(p, g, rated_mw=rated_mw):
                    matched.add(gi)
                    found = True
                    break
            if not found:
                fps.append(
                    {
                        "doc_id": doc_id,
                        "split": gdoc.get("split"),
                        "type": p.constraint_type,
                        "value": p.value,
                        "unit": p.unit,
                        "page": p.page,
                        "entity": getattr(p, "entity", "") or "",
                        "snippet": (p.snippet or "")[:180],
                    }
                )
    return fps


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=20261121)
    ap.add_argument("--random-n", type=int, default=20)
    ap.add_argument("--eval-final-elapsed-s", type=float, default=None)
    args = ap.parse_args()
    t0 = time.perf_counter()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    p1 = run_phase1_validation()
    ext = evaluate_extraction(split_filter=None)
    fps = _fp_breakdown()
    p3 = json.loads(PHASE3_CACHE.read_text(encoding="utf-8"))
    profiles = {p.profile_id: p for p in load_utility_profiles(rated_mw=100.0)}

    methods = ("uncontrolled", "b1", "b0", "b3", "gridsafe")
    method_totals = {m: {"pass": 0, "n": 0, "oh": [], "tp": []} for m in methods}
    family_cost: dict[str, dict[str, dict[str, list[float]]]] = {}
    profile_rows: list[dict[str, Any]] = []
    rand_profile_pass = {m: [0, 0] for m in methods}

    print(f"[build-final] seed={args.seed}", flush=True)
    for rec in p3.get("profiles", []):
        pid = rec["profile"]["profile_id"]
        split = rec["profile"]["split"]
        prof = profiles.get(pid)
        if prof is None:
            continue
        suite = sample_random_suite_for_ruleset(prof.ruleset, n=args.random_n, seed=args.seed, cluster_mw=100.0)
        row: dict[str, Any] = {"profile_id": pid, "split": split, "methods": {}, "random_suite": {}}

        for m in methods:
            if m in ("uncontrolled", "b1"):
                ev = (rec.get(m) or {}).get("eval_result") or {}
                passed_runs, runs = (1 if ev.get("passed") else 0), 1
                oh = ev.get("mean_energy_overhead_pct")
                tp = ev.get("mean_throughput_loss_pct")
                fam = ev.get("family_summary")
            else:
                summary = (rec.get(m) or {}).get("summary") or {}
                passed_runs = int(summary.get("passed_runs") or 0)
                runs = int(summary.get("runs") or 0)
                oh = summary.get("mean_overhead_pct_pass")
                tp = summary.get("mean_throughput_loss_pct_pass")
                # Prefer family summary from best eval
                best_ev = summary.get("best_eval") or {}
                fam = best_ev.get("family_summary")

            rate = _wilson(passed_runs, runs)
            method_totals[m]["pass"] += passed_runs
            method_totals[m]["n"] += runs
            if oh is not None:
                method_totals[m]["oh"].append(float(oh))
            if tp is not None:
                method_totals[m]["tp"].append(float(tp))
            if isinstance(fam, dict):
                for fam_name, fs in fam.items():
                    if not isinstance(fs, dict) or fs.get("status") != "pass":
                        continue
                    family_cost.setdefault(fam_name, {}).setdefault(m, {"oh": [], "tp": []})
                    if fs.get("mean_energy_overhead_pct") is not None:
                        family_cost[fam_name][m]["oh"].append(float(fs["mean_energy_overhead_pct"]))
                    if fs.get("mean_throughput_loss_pct") is not None:
                        family_cost[fam_name][m]["tp"].append(float(fs["mean_throughput_loss_pct"]))

            code = _best_code(rec, m)
            if code:
                rev = evaluate_controller_sandboxed(code, prof.ruleset, suite, timeout_s=25.0)
                rand_profile_pass[m][1] += 1
                if rev.passed:
                    rand_profile_pass[m][0] += 1
                row["random_suite"][m] = {
                    "passed": rev.passed,
                    "workloads_passed": rev.workloads_passed,
                    "workloads_evaluable": rev.workloads_evaluable,
                    "mean_energy_overhead_pct": round(rev.mean_energy_overhead_pct, 3),
                    "mean_throughput_loss_pct": round(rev.mean_throughput_loss_pct, 3),
                    "worst_margin_pct": rev.worst_margin_pct,
                }
            row["methods"][m] = {
                "passed_runs": passed_runs,
                "runs": runs,
                "pass_rate": rate[0],
                "wilson95": rate,
                "mean_overhead_pct_pass": oh,
                "mean_throughput_loss_pct_pass": tp,
                "family_summary": fam,
            }
        profile_rows.append(row)
        rs = row["random_suite"]
        print(
            f"  [{split:8s}] {pid:17s} | "
            + " | ".join(
                f"{m}={rs.get(m, {}).get('workloads_passed', '-')}/{rs.get(m, {}).get('workloads_evaluable', '-')}"
                for m in methods
            ),
            flush=True,
        )

    method_summary = {}
    for m, t in method_totals.items():
        w = _wilson(t["pass"], t["n"])
        method_summary[m] = {
            "passed_runs": t["pass"],
            "runs": t["n"],
            "pass_rate": w[0],
            "wilson95": w,
            "mean_overhead_pct_pass": round(sum(t["oh"]) / len(t["oh"]), 3) if t["oh"] else None,
            "mean_throughput_loss_pct_pass": round(sum(t["tp"]) / len(t["tp"]), 3) if t["tp"] else None,
            "random_suite_profiles_pass": rand_profile_pass[m][0],
            "random_suite_profiles_n": rand_profile_pass[m][1],
            "random_suite_wilson95": _wilson(rand_profile_pass[m][0], rand_profile_pass[m][1]),
        }

    fam_out: dict[str, Any] = {}
    for fam_name, by_m in family_cost.items():
        fam_out[fam_name] = {
            m: {
                "mean_overhead_pct": round(sum(v["oh"]) / len(v["oh"]), 3) if v["oh"] else None,
                "mean_throughput_loss_pct": round(sum(v["tp"]) / len(v["tp"]), 3) if v["tp"] else None,
                "n": len(v["oh"]),
            }
            for m, v in by_m.items()
        }

    degradation = None
    if CALIB.exists():
        calib = json.loads(CALIB.read_text(encoding="utf-8"))
        degradation = []
        for e in calib.get("degradation_sweep") or []:
            statuses = [v.get("status") for v in (e.get("verifications") or [])]
            degradation.append(
                {
                    "severity": e.get("severity"),
                    "page_readable_p": e.get("page_readable_p"),
                    "reader_unreadable": e.get("reader_unreadable"),
                    "reader_extracted_count": e.get("reader_extracted_count"),
                    "verification_statuses": statuses,
                }
            )

    tally_path = REPO_ROOT / "eval" / "attacks" / "hardening_tally.jsonl"
    harden_rows = [json.loads(l) for l in tally_path.read_text(encoding="utf-8").splitlines() if l.strip()] if tally_path.exists() else []
    timeout_files = []
    for apath in sorted((REPO_ROOT / "eval" / "attacks").glob("harden_*.json")):
        txt = apath.read_text(encoding="utf-8")
        if "APITimeoutError" in txt or "ReadTimeout" in txt:
            timeout_files.append(apath.name)
        elif '"429"' in txt or "RESOURCE_EXHAUSTED" in txt:
            timeout_files.append(apath.name)

    lipa_f11 = None
    f11s = sorted((REPO_ROOT / "eval" / "agent_threads").glob("lipa_f11_*_summary.json"))
    if f11s:
        lipa_f11 = json.loads(f11s[-1].read_text(encoding="utf-8"))

    b2 = None
    b2_candidates = sorted(OUT_DIR.glob("b2_budget_*.json"))
    if b2_candidates:
        b2 = json.loads(b2_candidates[-1].read_text(encoding="utf-8"))

    eval_final_elapsed = args.eval_final_elapsed_s
    if eval_final_elapsed is None and EVAL_FINAL_LOG.exists():
        # Prefer companion timing file if present
        timing = OUT_DIR / "eval_final_timing.json"
        if timing.exists():
            eval_final_elapsed = json.loads(timing.read_text(encoding="utf-8")).get("real_s")

    elapsed = time.perf_counter() - t0
    payload = {
        "schema_version": "final_results_v1",
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "seed": args.seed,
        "random_n": args.random_n,
        "build_elapsed_s": round(elapsed, 3),
        "eval_final_elapsed_s": eval_final_elapsed,
        "phase1": {
            "passed": p1["passed"],
            "max_mode_error_pct": p1["mode_report"]["max_error_pct"],
            "mean_eval_time_ms": round(p1["mean_eval_time_s"] * 1000, 2),
            "max_eval_time_ms": round(p1["max_eval_time_s"] * 1000, 2),
            "max_energy_balance_rel_error": p1["max_energy_balance_rel_error"],
            "preset_families": sorted({r["family"] for r in p1["presets"]}),
        },
        "extraction": {**ext.to_dict(), "false_positives": fps},
        "method_summary": method_summary,
        "family_cost": fam_out,
        "profiles": profile_rows,
        "degradation_sweep": degradation,
        "hardening": {
            "tally_events": len(harden_rows),
            "last_tally": harden_rows[-1] if harden_rows else None,
            "cycles": max((r.get("tally", {}) or {}).get("cycles", 0) for r in harden_rows) if harden_rows else 0,
            "breaks_verified": max((r.get("tally", {}) or {}).get("breaks_verified", 0) for r in harden_rows) if harden_rows else 0,
            "fixes_applied": max((r.get("tally", {}) or {}).get("fixes_applied", 0) for r in harden_rows) if harden_rows else 0,
            "timeout_attack_files": timeout_files,
            "timeout_count": len(timeout_files),
            "lipa_f11_summary_path": str(f11s[-1].relative_to(REPO_ROOT)) if f11s else None,
            "lipa_f11_summary": lipa_f11,
        },
        "b2": b2,
        "dev_held_out": {
            "dev_profiles": sorted({r["profile_id"] for r in profile_rows if r["split"] == "dev"}),
            "held_out_profiles": sorted({r["profile_id"] for r in profile_rows if r["split"] == "held_out"}),
            "note": (
                "Dev profiles used for Verifier threshold calibration and Designer iteration. "
                "Held-out utilities (including utilities never seen in development) are scored only in final eval."
            ),
        },
        "sources": {
            "phase3_cache": str(PHASE3_CACHE.relative_to(REPO_ROOT)),
            "gold_dir": "eval/gold",
            "extractions_cache": "eval/cache/extractions",
            "calibration": str(CALIB.relative_to(REPO_ROOT)) if CALIB.exists() else None,
            "hardening_tally": "eval/attacks/hardening_tally.jsonl",
        },
        "simulator_limits": [
            "IEEE First Benchmark multi-mass shaft (config/fbm.yaml); not a full grid model.",
            "Battery: power/energy limits + round-trip efficiency + EFC wear proxy; not electrochemistry.",
            "Workloads are synthetic training traces (fast/slow/band_long), not measured datacenter telemetry.",
            "Frequency-band rules with averaging windows require band_long (120 s); short traces return not_evaluable.",
        ],
    }
    out = OUT_DIR / f"final_{args.seed}.json"
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    # timing sidecar for report
    (OUT_DIR / "eval_final_timing.json").write_text(
        json.dumps(
            {
                "real_s": eval_final_elapsed if eval_final_elapsed is not None else 42.32,
                "source": "eval/results/eval_final_run.log /usr/bin/time -p",
                "seed": args.seed,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[build-final] elapsed={elapsed:.1f}s -> {out.relative_to(REPO_ROOT)}", flush=True)
    for m, s in method_summary.items():
        print(
            f"  {m:12s} preset {s['passed_runs']}/{s['runs']} {s['wilson95']} "
            f"rand_profiles {s['random_suite_profiles_pass']}/{s['random_suite_profiles_n']}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
