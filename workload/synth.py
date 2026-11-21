"""Synthetic AI training cluster power trace generator.

Pure Python + numpy (no scipy). Generates deterministic power demand traces
modeling periodic training step/all-reduce rhythms, duty cycle, modulation depth,
timing/amplitude jitter, checkpoint stalls, and job start/stop transients,
scaled to an arbitrary cluster size in MW.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np


@dataclass(frozen=True)
class WorkloadParams:
    """Parameters defining a synthetic AI training cluster workload."""

    cluster_mw: float = 100.0
    duration_s: float = 10.0
    dt_s: float = 0.002
    period_s: float = 0.5
    duty_cycle: float = 0.75
    depth: float = 0.45
    jitter: float = 0.05
    edge_s: float = 0.008
    checkpoint_interval_s: float | None = 4.0
    checkpoint_duration_s: float = 0.6
    checkpoint_depth: float = 0.65
    job_start_s: float = 0.5
    job_start_ramp_s: float = 0.1
    job_stop_s: float | None = 9.0
    job_stop_ramp_s: float = 0.05
    idle_fraction: float = 0.15
    noise_std_fraction: float = 0.005
    seed: int = 42

    def validate(self) -> None:
        if self.cluster_mw <= 0:
            raise ValueError("cluster_mw must be positive")
        if self.duration_s <= 0 or self.dt_s <= 0:
            raise ValueError("duration_s and dt_s must be positive")
        if self.period_s <= 0:
            raise ValueError("period_s must be positive")
        if not (0.0 < self.duty_cycle <= 1.0):
            raise ValueError("duty_cycle must be in (0, 1]")
        if not (0.0 <= self.depth <= 1.0):
            raise ValueError("depth must be in [0, 1]")
        if not (0.0 <= self.jitter < 1.0):
            raise ValueError("jitter must be in [0, 1)")
        if self.edge_s < 0:
            raise ValueError("edge_s must be non-negative")
        if not (0.0 <= self.idle_fraction <= 1.0):
            raise ValueError("idle_fraction must be in [0, 1]")
        if not (0.0 <= self.checkpoint_depth <= 1.0):
            raise ValueError("checkpoint_depth must be in [0, 1]")
        if self.noise_std_fraction < 0:
            raise ValueError("noise_std_fraction must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkloadTrace:
    """Generated cluster power trace in MW."""

    t: np.ndarray
    p_raw_mw: np.ndarray
    dt_s: float
    rated_mw: float
    params: WorkloadParams
    events: list[dict[str, Any]] = field(default_factory=list)


def _trapezoid_cycle(
    t_local: np.ndarray,
    high_dur: float,
    period: float,
    edge_s: float,
) -> np.ndarray:
    """Return a [0, 1] pulse shape: 1 during compute phase, 0 during valley."""
    if high_dur >= period:
        return np.ones_like(t_local)
    if high_dur <= 0.0:
        return np.zeros_like(t_local)

    eff_edge = min(edge_s, 0.25 * high_dur, 0.25 * (period - high_dur))
    if eff_edge <= 0.0:
        return np.where(t_local < high_dur, 1.0, 0.0)

    out = np.zeros_like(t_local)
    # Rise edge [0, eff_edge)
    rise = t_local < eff_edge
    out[rise] = t_local[rise] / eff_edge
    # High plateau [eff_edge, high_dur - eff_edge)
    plateau = (t_local >= eff_edge) & (t_local < high_dur - eff_edge)
    out[plateau] = 1.0
    # Fall edge [high_dur - eff_edge, high_dur)
    fall = (t_local >= high_dur - eff_edge) & (t_local < high_dur)
    out[fall] = (high_dur - t_local[fall]) / eff_edge
    return out


def generate_workload(params: WorkloadParams | None = None) -> WorkloadTrace:
    """Synthesize a deterministic training cluster power trace (MW)."""
    p = params if params is not None else WorkloadParams()
    p.validate()

    rng = np.random.default_rng(p.seed)
    n_steps = max(2, int(round(p.duration_s / p.dt_s)))
    t = np.arange(n_steps, dtype=np.float64) * p.dt_s

    # 1. Build periodic compute/all-reduce training rhythm with seeded jitter.
    active_frac = np.ones(n_steps, dtype=np.float64)
    events: list[dict[str, Any]] = []

    cursor_t = 0.0
    cycle_idx = 0
    while cursor_t < p.duration_s + p.dt_s:
        period_scale = 1.0 + (rng.uniform(-p.jitter, p.jitter) if p.jitter > 0 else 0.0)
        cur_period = max(p.dt_s * 2.0, p.period_s * period_scale)
        amp_jitter = 1.0 + (rng.uniform(-0.5 * p.jitter, 0.5 * p.jitter) if p.jitter > 0 else 0.0)
        cur_depth = float(np.clip(p.depth * amp_jitter, 0.0, 1.0))

        start_idx = int(np.searchsorted(t, cursor_t, side="left"))
        end_idx = int(np.searchsorted(t, cursor_t + cur_period, side="left"))
        if end_idx > start_idx:
            t_local = t[start_idx:end_idx] - cursor_t
            pulse = _trapezoid_cycle(t_local, p.duty_cycle * cur_period, cur_period, p.edge_s)
            # pulse=1 -> peak (1.0), pulse=0 -> valley (1.0 - cur_depth)
            active_frac[start_idx:end_idx] = (1.0 - cur_depth) + cur_depth * pulse

        cursor_t += cur_period
        cycle_idx += 1

    # 2. Apply job start / stop envelope.
    envelope = np.ones(n_steps, dtype=np.float64)
    if p.job_start_s > 0.0:
        events.append({"type": "job_start", "t_s": p.job_start_s, "ramp_s": p.job_start_ramp_s})
        if p.job_start_ramp_s <= 0.0:
            envelope = np.where(t < p.job_start_s, 0.0, envelope)
        else:
            ramp_in = np.clip((t - p.job_start_s) / p.job_start_ramp_s, 0.0, 1.0)
            envelope = np.minimum(envelope, ramp_in)

    if p.job_stop_s is not None and p.job_stop_s < p.duration_s:
        events.append({"type": "job_stop", "t_s": p.job_stop_s, "ramp_s": p.job_stop_ramp_s})
        if p.job_stop_ramp_s <= 0.0:
            envelope = np.where(t >= p.job_stop_s, 0.0, envelope)
        else:
            ramp_out = np.clip((p.job_stop_s + p.job_stop_ramp_s - t) / p.job_stop_ramp_s, 0.0, 1.0)
            envelope = np.minimum(envelope, ramp_out)

    # Combine active compute fraction with facility idle baseline.
    power_frac = p.idle_fraction + (1.0 - p.idle_fraction) * envelope * active_frac

    # 3. Overlay periodic checkpoint write stalls while job is running.
    if p.checkpoint_interval_s is not None and p.checkpoint_interval_s > 0.0 and p.checkpoint_duration_s > 0.0:
        ckpt_t = max(p.job_start_s + p.job_start_ramp_s, p.checkpoint_interval_s)
        stop_limit = p.job_stop_s if p.job_stop_s is not None else p.duration_s
        while ckpt_t < stop_limit:
            ckpt_end = min(ckpt_t + p.checkpoint_duration_s, stop_limit)
            events.append({"type": "checkpoint", "t_start_s": ckpt_t, "t_end_s": ckpt_end})
            c_start = int(np.searchsorted(t, ckpt_t, side="left"))
            c_end = int(np.searchsorted(t, ckpt_end, side="left"))
            if c_end > c_start:
                t_seg = t[c_start:c_end] - ckpt_t
                seg_dur = ckpt_end - ckpt_t
                eff_edge = min(p.edge_s, 0.2 * seg_dur)
                if eff_edge <= 0.0:
                    stall = np.ones(c_end - c_start, dtype=np.float64)
                else:
                    up = np.clip(t_seg / eff_edge, 0.0, 1.0)
                    down = np.clip((seg_dur - t_seg) / eff_edge, 0.0, 1.0)
                    stall = np.minimum(up, down)
                # Drop active load toward checkpoint floor during stall
                ckpt_level = max(p.idle_fraction, 1.0 - p.checkpoint_depth)
                power_frac[c_start:c_end] = power_frac[c_start:c_end] * (1.0 - stall) + ckpt_level * stall
            ckpt_t += p.checkpoint_interval_s

    # 4. Add small seeded stochastic micro-jitter.
    if p.noise_std_fraction > 0.0:
        power_frac = power_frac + rng.normal(0.0, p.noise_std_fraction, size=n_steps)

    p_raw_mw = np.clip(power_frac, 0.0, 1.0) * p.cluster_mw

    return WorkloadTrace(
        t=t,
        p_raw_mw=p_raw_mw,
        dt_s=p.dt_s,
        rated_mw=p.cluster_mw,
        params=p,
        events=events,
    )


def preset_workloads(cluster_mw: float = 100.0) -> dict[str, WorkloadParams]:
    """Curated deterministic workload presets for testing, demos, and red-teaming."""
    return {
        "standard_training": WorkloadParams(
            cluster_mw=cluster_mw,
            duration_s=10.0,
            dt_s=0.002,
            period_s=0.5,
            duty_cycle=0.75,
            depth=0.45,
            jitter=0.05,
            checkpoint_interval_s=4.0,
            checkpoint_duration_s=0.6,
            job_start_s=0.5,
            job_stop_s=9.0,
            seed=42,
        ),
        "ssr_mode1_attack": WorkloadParams(
            cluster_mw=cluster_mw,
            duration_s=10.0,
            dt_s=0.002,
            period_s=1.0 / 15.7122,  # IEEE FBM Mode 1 torsional resonance (15.71 Hz)
            duty_cycle=0.5,
            depth=0.60,
            jitter=0.0,
            edge_s=0.002,
            checkpoint_interval_s=None,
            job_start_s=0.0,
            job_start_ramp_s=0.0,
            job_stop_s=None,
            noise_std_fraction=0.0,
            seed=101,
        ),
        "ssr_mode2_attack": WorkloadParams(
            cluster_mw=cluster_mw,
            duration_s=10.0,
            dt_s=0.002,
            period_s=1.0 / 20.2113,  # IEEE FBM Mode 2 torsional resonance (20.21 Hz)
            duty_cycle=0.5,
            depth=0.60,
            jitter=0.0,
            edge_s=0.002,
            checkpoint_interval_s=None,
            job_start_s=0.0,
            job_start_ramp_s=0.0,
            job_stop_s=None,
            noise_std_fraction=0.0,
            seed=102,
        ),
        "abrupt_checkpoint_drop": WorkloadParams(
            cluster_mw=cluster_mw,
            duration_s=10.0,
            dt_s=0.002,
            period_s=1.0,
            duty_cycle=0.80,
            depth=0.35,
            jitter=0.02,
            edge_s=0.004,
            checkpoint_interval_s=3.0,
            checkpoint_duration_s=0.8,
            checkpoint_depth=0.75,
            job_start_s=0.2,
            job_start_ramp_s=0.01,
            job_stop_s=8.5,
            job_stop_ramp_s=0.005,
            seed=203,
        ),
    }
