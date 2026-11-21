"""Randomly sampled unseen workload suite (Step 6 — workloads we did not write).

Pure Python + numpy only. Samples N deterministic AI training cluster workloads
(half fast 20 s @ 0.002 s, half slow 30 min @ 0.5 s) from documented physical
parameter ranges using a CLI-provided seed (chosen at judging time for
`make eval-final SEED=<seed>`). Every workload begins at facility idle.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rules.checker import check_trace
from rules.schema import RuleSet
from workload.synth import WorkloadParams

# Documented sampling ranges (justified by AI training cluster telemetry & IEEE FBM physics):
# - Fast family (20 s @ 0.002 s): period spans 0.5-50 Hz (covers compute/all-reduce rhythms and
#   the entire 15-48 Hz IEEE FBM subsynchronous torsional range without hand-picked modal freqs).
# - Slow family (1800 s @ 0.5 s): 30-minute lifecycle covering idle prefix, job startup ramp,
#   training with periodic checkpoint stalls, and job shutdown ramp.
PARAMETER_RANGES: dict[str, dict[str, Any]] = {
    "fast": {
        "duration_s": 20.0,
        "dt_s": 0.002,
        "period_s_log_uniform": (0.02, 2.0),  # 0.5 Hz to 50 Hz
        "duty_cycle_uniform": (0.30, 0.90),
        "depth_uniform": (0.20, 0.75),
        "jitter_uniform": (0.0, 0.15),
        "edge_s_log_uniform": (0.002, 0.020),
        "checkpoint_prob": 0.70,
        "checkpoint_interval_s_uniform": (2.5, 8.0),
        "checkpoint_duration_s_uniform": (0.2, 1.2),
        "checkpoint_depth_uniform": (0.45, 0.85),
        "job_start_s_uniform": (0.2, 2.0),
        "job_start_ramp_s_log_uniform": (0.005, 0.5),
        "job_stop_prob": 0.50,
        "job_stop_s_uniform": (14.0, 18.5),
        "job_stop_ramp_s_log_uniform": (0.005, 0.3),
        "idle_fraction_uniform": (0.10, 0.25),
        "noise_std_fraction_uniform": (0.0, 0.015),
    },
    "band_long": {
        "duration_s": 120.0,
        "dt_s": 0.01,
        "period_s_log_uniform": (0.2, 10.0),  # 0.1–5 Hz band content
        "duty_cycle_uniform": (0.40, 0.85),
        "depth_uniform": (0.20, 0.65),
        "jitter_uniform": (0.0, 0.10),
        "edge_s_log_uniform": (0.02, 0.5),
        "checkpoint_prob": 0.50,
        "checkpoint_interval_s_uniform": (20.0, 50.0),
        "checkpoint_duration_s_uniform": (1.0, 5.0),
        "checkpoint_depth_uniform": (0.45, 0.80),
        "job_start_s_uniform": (2.0, 15.0),
        "job_start_ramp_s_log_uniform": (0.1, 5.0),
        "job_stop_prob": 0.60,
        "job_stop_s_uniform": (90.0, 115.0),
        "job_stop_ramp_s_log_uniform": (0.1, 4.0),
        "idle_fraction_uniform": (0.10, 0.25),
        "noise_std_fraction_uniform": (0.0, 0.01),
    },
    "slow": {
        "duration_s": 1800.0,
        "dt_s": 0.5,
        "period_s_log_uniform": (4.0, 60.0),
        "duty_cycle_uniform": (0.45, 0.90),
        "depth_uniform": (0.20, 0.65),
        "jitter_uniform": (0.0, 0.15),
        "edge_s_log_uniform": (0.5, 4.0),
        "checkpoint_prob": 1.0,
        "checkpoint_interval_s_uniform": (120.0, 480.0),
        "checkpoint_duration_s_uniform": (15.0, 90.0),
        "checkpoint_depth_uniform": (0.50, 0.85),
        "job_start_s_uniform": (30.0, 300.0),
        "job_start_ramp_s_log_uniform": (0.5, 60.0),
        "job_stop_prob": 0.80,
        "job_stop_s_uniform": (1350.0, 1700.0),
        "job_stop_ramp_s_log_uniform": (0.5, 45.0),
        "idle_fraction_uniform": (0.10, 0.25),
        "noise_std_fraction_uniform": (0.0, 0.01),
    },
}


def _log_uniform(rng: np.random.Generator, lo: float, hi: float) -> float:
    return float(math.exp(rng.uniform(math.log(lo), math.log(hi))))


def _sample_fast_one(rng: np.random.Generator, idx: int, cluster_mw: float) -> WorkloadParams:
    r = PARAMETER_RANGES["fast"]
    has_ckpt = bool(rng.random() < r["checkpoint_prob"])
    has_stop = bool(rng.random() < r["job_stop_prob"])
    return WorkloadParams(
        cluster_mw=float(cluster_mw),
        duration_s=float(r["duration_s"]),
        dt_s=float(r["dt_s"]),
        period_s=round(_log_uniform(rng, *r["period_s_log_uniform"]), 5),
        duty_cycle=round(float(rng.uniform(*r["duty_cycle_uniform"])), 4),
        depth=round(float(rng.uniform(*r["depth_uniform"])), 4),
        jitter=round(float(rng.uniform(*r["jitter_uniform"])), 4),
        edge_s=round(_log_uniform(rng, *r["edge_s_log_uniform"]), 5),
        checkpoint_interval_s=(
            round(float(rng.uniform(*r["checkpoint_interval_s_uniform"])), 3) if has_ckpt else None
        ),
        checkpoint_duration_s=round(float(rng.uniform(*r["checkpoint_duration_s_uniform"])), 3),
        checkpoint_depth=round(float(rng.uniform(*r["checkpoint_depth_uniform"])), 4),
        job_start_s=round(float(rng.uniform(*r["job_start_s_uniform"])), 3),
        job_start_ramp_s=round(_log_uniform(rng, *r["job_start_ramp_s_log_uniform"]), 4),
        job_stop_s=(round(float(rng.uniform(*r["job_stop_s_uniform"])), 3) if has_stop else None),
        job_stop_ramp_s=round(_log_uniform(rng, *r["job_stop_ramp_s_log_uniform"]), 4),
        idle_fraction=round(float(rng.uniform(*r["idle_fraction_uniform"])), 4),
        noise_std_fraction=round(float(rng.uniform(*r["noise_std_fraction_uniform"])), 5),
        seed=int(rng.integers(1, 2**31 - 1)),
        family="fast",
    )


def _sample_slow_one(rng: np.random.Generator, idx: int, cluster_mw: float) -> WorkloadParams:
    r = PARAMETER_RANGES["slow"]
    has_stop = bool(rng.random() < r["job_stop_prob"])
    return WorkloadParams(
        cluster_mw=float(cluster_mw),
        duration_s=float(r["duration_s"]),
        dt_s=float(r["dt_s"]),
        period_s=round(_log_uniform(rng, *r["period_s_log_uniform"]), 3),
        duty_cycle=round(float(rng.uniform(*r["duty_cycle_uniform"])), 4),
        depth=round(float(rng.uniform(*r["depth_uniform"])), 4),
        jitter=round(float(rng.uniform(*r["jitter_uniform"])), 4),
        edge_s=round(_log_uniform(rng, *r["edge_s_log_uniform"]), 3),
        checkpoint_interval_s=round(float(rng.uniform(*r["checkpoint_interval_s_uniform"])), 2),
        checkpoint_duration_s=round(float(rng.uniform(*r["checkpoint_duration_s_uniform"])), 2),
        checkpoint_depth=round(float(rng.uniform(*r["checkpoint_depth_uniform"])), 4),
        job_start_s=round(float(rng.uniform(*r["job_start_s_uniform"])), 2),
        job_start_ramp_s=round(_log_uniform(rng, *r["job_start_ramp_s_log_uniform"]), 3),
        job_stop_s=(round(float(rng.uniform(*r["job_stop_s_uniform"])), 2) if has_stop else None),
        job_stop_ramp_s=round(_log_uniform(rng, *r["job_stop_ramp_s_log_uniform"]), 3),
        idle_fraction=round(float(rng.uniform(*r["idle_fraction_uniform"])), 4),
        noise_std_fraction=round(float(rng.uniform(*r["noise_std_fraction_uniform"])), 5),
        seed=int(rng.integers(1, 2**31 - 1)),
        family="slow",
    )


def _sample_band_long_one(rng: np.random.Generator, idx: int, cluster_mw: float) -> WorkloadParams:
    r = PARAMETER_RANGES["band_long"]
    has_ckpt = bool(rng.random() < r["checkpoint_prob"])
    has_stop = bool(rng.random() < r["job_stop_prob"])
    return WorkloadParams(
        cluster_mw=float(cluster_mw),
        duration_s=float(r["duration_s"]),
        dt_s=float(r["dt_s"]),
        period_s=round(_log_uniform(rng, *r["period_s_log_uniform"]), 4),
        duty_cycle=round(float(rng.uniform(*r["duty_cycle_uniform"])), 4),
        depth=round(float(rng.uniform(*r["depth_uniform"])), 4),
        jitter=round(float(rng.uniform(*r["jitter_uniform"])), 4),
        edge_s=round(_log_uniform(rng, *r["edge_s_log_uniform"]), 4),
        checkpoint_interval_s=(
            round(float(rng.uniform(*r["checkpoint_interval_s_uniform"])), 2) if has_ckpt else None
        ),
        checkpoint_duration_s=round(float(rng.uniform(*r["checkpoint_duration_s_uniform"])), 3),
        checkpoint_depth=round(float(rng.uniform(*r["checkpoint_depth_uniform"])), 4),
        job_start_s=round(float(rng.uniform(*r["job_start_s_uniform"])), 2),
        job_start_ramp_s=round(_log_uniform(rng, *r["job_start_ramp_s_log_uniform"]), 3),
        job_stop_s=(round(float(rng.uniform(*r["job_stop_s_uniform"])), 2) if has_stop else None),
        job_stop_ramp_s=round(_log_uniform(rng, *r["job_stop_ramp_s_log_uniform"]), 3),
        idle_fraction=round(float(rng.uniform(*r["idle_fraction_uniform"])), 4),
        noise_std_fraction=round(float(rng.uniform(*r["noise_std_fraction_uniform"])), 5),
        seed=int(rng.integers(1, 2**31 - 1)),
        family="band_long",
    )


def sample_random_suite(
    n: int = 20,
    *,
    seed: int = 20261121,
    cluster_mw: float = 100.0,
    family: str = "both",
) -> dict[str, WorkloadParams]:
    """Sample N deterministic unseen workloads from documented parameter ranges.

    Uses independent deterministic RNG streams for fast and slow families derived
    from `seed`, so single-family suites (n=20 fast or n=20 slow) are exact
    supersets of the corresponding 10-workload halves in dual-family suites.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    rng_fast = np.random.default_rng([int(seed), 1])
    rng_slow = np.random.default_rng([int(seed), 2])
    rng_band = np.random.default_rng([int(seed), 3])
    out: dict[str, WorkloadParams] = {}

    if family == "fast":
        n_fast, n_slow, n_band = n, 0, 0
    elif family == "slow":
        n_fast, n_slow, n_band = 0, n, 0
    elif family == "band_long":
        n_fast, n_slow, n_band = 0, 0, n
    elif family == "both":
        n_fast = n // 2
        n_slow = n - n_fast
        n_band = 0
    else:
        raise ValueError(f"Unsupported family: {family!r} (expected 'fast', 'slow', 'band_long', or 'both')")

    for i in range(n_fast):
        out[f"rand_fast_{i:02d}"] = _sample_fast_one(rng_fast, i, cluster_mw=cluster_mw)
    for j in range(n_slow):
        out[f"rand_slow_{j:02d}"] = _sample_slow_one(rng_slow, j, cluster_mw=cluster_mw)
    for k in range(n_band):
        out[f"rand_band_{k:02d}"] = _sample_band_long_one(rng_band, k, cluster_mw=cluster_mw)
    return out


def evaluable_families_for_ruleset(ruleset: RuleSet) -> list[str]:
    """Return which workload families have >= 1 evaluable rule for `ruleset`."""
    evaluable: list[str] = []
    for fam in ("fast", "slow", "band_long"):
        r = PARAMETER_RANGES[fam]
        dur = float(r["duration_s"])
        dt = float(r["dt_s"])
        n_pts = max(2, int(round(dur / dt)))
        probe = np.full(n_pts, 0.15 * ruleset.rated_mw, dtype=np.float64)
        rep = check_trace(probe, dt_s=dt, ruleset=ruleset, strict_review=False, initial_power_mw=0.15 * ruleset.rated_mw)
        if rep.evaluable_count > 0:
            evaluable.append(fam)
    return evaluable


def sample_random_suite_for_ruleset(
    ruleset: RuleSet,
    *,
    n: int = 20,
    seed: int = 20261121,
    cluster_mw: float | None = None,
) -> dict[str, WorkloadParams]:
    """Sample N deterministic unseen workloads guaranteed to be evaluable for `ruleset`.

    Prefer fast+slow when both work. If only band_long qualifies (long averaging windows),
    sample all N from band_long.
    """
    mw = float(cluster_mw if cluster_mw is not None else ruleset.rated_mw)
    fams = set(evaluable_families_for_ruleset(ruleset))
    if "fast" in fams and "slow" in fams:
        fam_mode = "both"
    elif "fast" in fams:
        fam_mode = "fast"
    elif "slow" in fams:
        fam_mode = "slow"
    elif "band_long" in fams:
        fam_mode = "band_long"
    else:
        fam_mode = "both"
    return sample_random_suite(n=n, seed=seed, cluster_mw=mw, family=fam_mode)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sample N unseen GridSafe workloads from documented ranges.")
    parser.add_argument("--seed", type=int, required=True, help="RNG seed for deterministic workload sampling")
    parser.add_argument("--n", type=int, default=20, help="Total number of workloads to sample (default: 20)")
    parser.add_argument("--cluster-mw", type=float, default=100.0, help="Cluster rated capacity in MW")
    parser.add_argument("--family", choices=["both", "fast", "slow"], default="both")
    parser.add_argument("--out", type=str, default=None, help="Optional JSON path to save sampled workload params")
    args = parser.parse_args()

    suite = sample_random_suite(n=args.n, seed=args.seed, cluster_mw=args.cluster_mw, family=args.family)
    print(f"Sampled {len(suite)} unseen workloads (seed={args.seed}, family={args.family}, rated={args.cluster_mw:.0f} MW):")
    for name, p in suite.items():
        freq = 1.0 / p.period_s
        stop_txt = f"{p.job_stop_s:6.1f}s" if p.job_stop_s is not None else "   None"
        print(
            f"  {name:14s} [{p.family:4s}] dur={p.duration_s:6.1f}s dt={p.dt_s:5.3f}s "
            f"f={freq:6.2f}Hz depth={p.depth:.2f} idle={p.idle_fraction:.2f} "
            f"start={p.job_start_s:5.1f}s stop={stop_txt}"
        )

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "seed": args.seed,
            "n": args.n,
            "cluster_mw": args.cluster_mw,
            "family": args.family,
            "parameter_ranges": PARAMETER_RANGES,
            "workloads": {k: v.to_dict() for k, v in suite.items()},
        }
        out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"Saved -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
