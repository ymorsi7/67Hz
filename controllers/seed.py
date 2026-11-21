"""Seed power-smoothing controller for GridSafe Designer evolution.

Pure Python + numpy only, under 60 lines. Smooths raw AI cluster load with a
first-order EMA target and dispatches the battery first with gentle SOC
centering, leaving GPU floor/cap unforced so evolution can optimize per rulebook.
"""

from __future__ import annotations

import numpy as np


def control(obs: dict) -> dict[str, float]:
    """Return GPU floor, GPU cap, and battery setpoint (MW) for the current step."""
    p_raw = float(obs["p_raw_mw"])
    rated = float(obs["rated_mw"])
    dt = float(obs["dt"])
    soc = float(obs["soc"])
    b_max = float(obs["battery_max_power_mw"])
    state = obs["state"]

    # Exponential moving average target of cluster power (tau = 0.25 s)
    alpha = float(np.clip(dt / (0.25 + dt), 0.0, 1.0))
    if "p_ema" not in state:
        state["p_ema"] = p_raw
    else:
        state["p_ema"] = (1.0 - alpha) * state["p_ema"] + alpha * p_raw

    # Gentle SOC centering bias toward 50% SOC
    soc_bias = (0.5 - soc) * 0.25 * b_max
    p_target = float(np.clip(state["p_ema"] + soc_bias, 0.0, rated))

    # Battery absorbs deviation between raw cluster load and smoothed grid target
    batt_cmd = float(np.clip(p_raw - p_target, -b_max, b_max))

    return {
        "gpu_floor_mw": 0.0,
        "gpu_cap_mw": rated,
        "battery_setpoint_mw": batt_cmd,
    }
