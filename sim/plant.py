"""Time-stepped AI cluster + battery + controller plant simulation.

Pure Python + numpy (no scipy). Enforces exact GPU power floor/cap and battery
power/SOC/efficiency limits with machine-precision energy conservation, computes
battery wear proxy, and evaluates frequency-domain turbine-generator shaft
torque via sim.shaft.ShaftModel.
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from sim.shaft import ShaftModel, ShaftResponse, load_shaft_model
from workload.synth import WorkloadTrace

ControllerFn = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class BatteryConfig:
    """Energy storage system parameters."""

    max_power_mw: float = 40.0
    capacity_mwh: float = 20.0
    initial_soc: float = 0.5
    soc_min: float = 0.1
    soc_max: float = 0.9
    eta_charge: float = math.sqrt(0.90)
    eta_discharge: float = math.sqrt(0.90)

    def validate(self) -> None:
        if self.max_power_mw < 0:
            raise ValueError("max_power_mw must be non-negative")
        if self.capacity_mwh < 0:
            raise ValueError("capacity_mwh must be non-negative")
        if not (0.0 <= self.soc_min <= self.soc_max <= 1.0):
            raise ValueError("Require 0 <= soc_min <= soc_max <= 1")
        if not (self.soc_min <= self.initial_soc <= self.soc_max):
            raise ValueError("initial_soc must lie within [soc_min, soc_max]")
        if not (0.0 < self.eta_charge <= 1.0 and 0.0 < self.eta_discharge <= 1.0):
            raise ValueError("Battery efficiencies must be in (0, 1]")


@dataclass(frozen=True)
class PlantConfig:
    """Plant and grid coupling configuration."""

    battery: BatteryConfig = BatteryConfig()
    coupling_factor: float = 1.0
    history_window: int = 250
    fbm_config_path: str | Path | None = None


@dataclass
class SimulationMetrics:
    """Energy ledger, overhead, throughput loss, battery wear, and timing."""

    energy_raw_mwh: float
    energy_gpu_mwh: float
    energy_grid_mwh: float
    energy_dummy_mwh: float
    energy_curtailed_mwh: float
    energy_batt_loss_mwh: float
    energy_batt_delta_mwh: float  # E_final - E_init
    energy_balance_error_mwh: float
    energy_overhead_pct: float
    throughput_loss_pct: float
    battery_throughput_mwh: float
    battery_efc: float
    battery_wear_proxy: float
    peak_grid_mw: float
    min_grid_mw: float
    max_shaft_torque_pu: float
    eval_time_s: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass
class SimulationResult:
    """Complete time-domain and frequency-domain plant simulation output."""

    t: np.ndarray
    dt_s: float
    rated_mw: float
    p_raw_mw: np.ndarray
    p_gpu_mw: np.ndarray
    p_batt_mw: np.ndarray  # Positive = discharge, negative = charge
    p_grid_mw: np.ndarray
    p_dummy_mw: np.ndarray
    p_curtailed_mw: np.ndarray
    soc: np.ndarray
    shaft: ShaftResponse
    metrics: SimulationMetrics
    initial_grid_mw: float = 0.0


def noop_controller(obs: dict[str, Any]) -> dict[str, float]:
    """Uncontrolled baseline: no GPU floor, no power cap, no battery action."""
    return {
        "gpu_floor_mw": 0.0,
        "gpu_cap_mw": float(obs["rated_mw"]),
        "battery_setpoint_mw": 0.0,
    }


def fixed_floor_controller(floor_fraction: float = 0.90) -> ControllerFn:
    """Conservative fixed GPU power floor controller (Baseline B1 at 0.90)."""
    frac = float(floor_fraction)

    def _control(obs: dict[str, Any]) -> dict[str, float]:
        rated = float(obs["rated_mw"])
        return {
            "gpu_floor_mw": frac * rated,
            "gpu_cap_mw": rated,
            "battery_setpoint_mw": 0.0,
        }

    return _control


def _require_finite(name: str, val: Any) -> float:
    fval = float(val)
    if not math.isfinite(fval):
        raise ValueError(f"Controller returned non-finite {name}: {val!r}")
    return fval


def simulate_plant(
    trace: WorkloadTrace,
    controller: ControllerFn = noop_controller,
    plant_config: PlantConfig | None = None,
    shaft_model: ShaftModel | None = None,
    rules_dict: dict[str, Any] | None = None,
) -> SimulationResult:
    """Run deterministic time-stepped plant simulation followed by frequency-domain shaft analysis."""
    t_start = time.perf_counter()

    cfg = plant_config if plant_config is not None else PlantConfig()
    batt = cfg.battery
    batt.validate()

    shaft = shaft_model if shaft_model is not None else load_shaft_model(cfg.fbm_config_path)

    t = np.asarray(trace.t, dtype=np.float64)
    p_raw = np.asarray(trace.p_raw_mw, dtype=np.float64)
    n_steps = len(t)
    dt_s = float(trace.dt_s)
    dt_h = dt_s / 3600.0
    rated_mw = float(trace.rated_mw)

    p_gpu = np.empty(n_steps, dtype=np.float64)
    p_batt = np.empty(n_steps, dtype=np.float64)
    p_grid = np.empty(n_steps, dtype=np.float64)
    p_dummy = np.empty(n_steps, dtype=np.float64)
    p_curtailed = np.empty(n_steps, dtype=np.float64)
    soc = np.empty(n_steps, dtype=np.float64)

    cap_mwh = float(batt.capacity_mwh)
    e_min = batt.soc_min * cap_mwh
    e_max = batt.soc_max * cap_mwh
    e_init = batt.initial_soc * cap_mwh
    e_curr = e_init
    p_batt_max = float(batt.max_power_mw)
    eta_c = float(batt.eta_charge)
    eta_d = float(batt.eta_discharge)

    total_batt_loss_mwh = 0.0
    total_batt_throughput_mwh = 0.0
    total_wear_num = 0.0

    hist_win = max(1, int(cfg.history_window))
    controller_state: dict[str, Any] = {}
    rules_payload = dict(rules_dict) if rules_dict is not None else {}

    # Pre-t=0 facility idle power; prepended by checker so t=0 jumps count against ramp/step rules.
    initial_grid_mw = float(trace.idle_mw) if getattr(trace, "idle_mw", 0.0) > 0.0 else float(p_raw[0])
    prev_grid = initial_grid_mw

    # Pre-build observation dict and mutate scalar/view fields in-place for speed.
    obs: dict[str, Any] = {
        "t": 0.0,
        "dt": dt_s,
        "step": 0,
        "p_raw_mw": float(p_raw[0]),
        "p_prev_grid_mw": prev_grid,
        "idle_mw": initial_grid_mw,
        "rated_mw": rated_mw,
        "soc": batt.initial_soc,
        "battery_energy_mwh": e_curr,
        "battery_capacity_mwh": cap_mwh,
        "battery_max_power_mw": p_batt_max,
        "history_grid_mw": p_grid[:0],
        "history_raw_mw": p_raw[:0],
        "rules": rules_payload,
        "state": controller_state,
    }

    for i in range(n_steps):
        raw_i = float(p_raw[i])
        start_h = 0 if i < hist_win else i - hist_win
        obs["t"] = float(t[i])
        obs["step"] = i
        obs["p_raw_mw"] = raw_i
        obs["p_prev_grid_mw"] = prev_grid
        obs["soc"] = (e_curr / cap_mwh) if cap_mwh > 0.0 else 0.0
        obs["battery_energy_mwh"] = e_curr
        obs["history_grid_mw"] = p_grid[start_h:i]
        obs["history_raw_mw"] = p_raw[start_h:i]

        action = controller(obs)
        if not isinstance(action, dict):
            raise ValueError(f"Controller must return dict, got {type(action).__name__}")

        cap_req = _require_finite("gpu_cap_mw", action.get("gpu_cap_mw", rated_mw))
        floor_req = _require_finite("gpu_floor_mw", action.get("gpu_floor_mw", 0.0))
        batt_req = _require_finite("battery_setpoint_mw", action.get("battery_setpoint_mw", 0.0))

        # Clamp GPU cap and floor into physical [0, rated_mw], with cap taking precedence.
        gpu_cap = min(max(cap_req, 0.0), rated_mw)
        gpu_floor = min(max(floor_req, 0.0), gpu_cap)

        p_capped = raw_i if raw_i <= gpu_cap else gpu_cap
        curt_i = raw_i - p_capped
        gpu_i = p_capped if p_capped >= gpu_floor else gpu_floor
        dummy_i = gpu_i - p_capped

        # Enforce battery power, SOC, and efficiency physics.
        if cap_mwh <= 0.0 or p_batt_max <= 0.0:
            batt_i = 0.0
        else:
            b_cmd = min(max(batt_req, -p_batt_max), p_batt_max)
            if b_cmd > 0.0:
                # Discharging: reduces stored energy by (b * dt_h) / eta_d
                max_dis = ((e_curr - e_min) * eta_d) / dt_h
                batt_i = b_cmd if b_cmd <= max_dis else max(0.0, max_dis)
                de_int = (batt_i * dt_h) / eta_d
                e_curr = max(e_min, e_curr - de_int)
                total_batt_loss_mwh += (batt_i * dt_h) * ((1.0 / eta_d) - 1.0)
            elif b_cmd < 0.0:
                # Charging: increases stored energy by p_chg * dt_h * eta_c
                p_chg = -b_cmd
                max_chg = (e_max - e_curr) / (eta_c * dt_h)
                if p_chg > max_chg:
                    p_chg = max(0.0, max_chg)
                batt_i = -p_chg
                de_int = p_chg * dt_h * eta_c
                e_curr = min(e_max, e_curr + de_int)
                total_batt_loss_mwh += (p_chg * dt_h) * (1.0 - eta_c)
            else:
                batt_i = 0.0

            abs_b = abs(batt_i)
            if abs_b > 0.0:
                step_mwh = abs_b * dt_h
                total_batt_throughput_mwh += step_mwh
                c_stress = (abs_b / p_batt_max) ** 2
                total_wear_num += step_mwh * (1.0 + 0.5 * c_stress)

        grid_i = gpu_i - batt_i

        p_gpu[i] = gpu_i
        p_batt[i] = batt_i
        p_grid[i] = grid_i
        p_dummy[i] = dummy_i
        p_curtailed[i] = curt_i
        soc[i] = (e_curr / cap_mwh) if cap_mwh > 0.0 else 0.0
        prev_grid = grid_i

    # Frequency-domain multi-mass shaft torque response
    shaft_resp = shaft.compute_shaft_response(
        p_grid_mw=p_grid,
        dt_s=dt_s,
        coupling_factor=cfg.coupling_factor,
    )

    energy_raw_mwh = float(np.sum(p_raw) * dt_h)
    energy_gpu_mwh = float(np.sum(p_gpu) * dt_h)
    energy_grid_mwh = float(np.sum(p_grid) * dt_h)
    energy_dummy_mwh = float(np.sum(p_dummy) * dt_h)
    energy_curtailed_mwh = float(np.sum(p_curtailed) * dt_h)
    energy_batt_delta_mwh = e_curr - e_init

    # Exact energy balance:
    # energy_grid_mwh + (e_init - e_curr) == energy_gpu_mwh + total_batt_loss_mwh
    lhs = energy_grid_mwh - energy_batt_delta_mwh
    rhs = energy_gpu_mwh + total_batt_loss_mwh
    energy_balance_error_mwh = abs(lhs - rhs)

    denom_raw = max(energy_raw_mwh, 1e-12)
    energy_overhead_pct = ((energy_dummy_mwh + total_batt_loss_mwh) / denom_raw) * 100.0
    throughput_loss_pct = (energy_curtailed_mwh / denom_raw) * 100.0

    battery_efc = (total_batt_throughput_mwh / (2.0 * cap_mwh)) if cap_mwh > 0.0 else 0.0
    battery_wear_proxy = (total_wear_num / (2.0 * cap_mwh)) if cap_mwh > 0.0 else 0.0

    eval_time_s = time.perf_counter() - t_start

    metrics = SimulationMetrics(
        energy_raw_mwh=energy_raw_mwh,
        energy_gpu_mwh=energy_gpu_mwh,
        energy_grid_mwh=energy_grid_mwh,
        energy_dummy_mwh=energy_dummy_mwh,
        energy_curtailed_mwh=energy_curtailed_mwh,
        energy_batt_loss_mwh=total_batt_loss_mwh,
        energy_batt_delta_mwh=energy_batt_delta_mwh,
        energy_balance_error_mwh=energy_balance_error_mwh,
        energy_overhead_pct=energy_overhead_pct,
        throughput_loss_pct=throughput_loss_pct,
        battery_throughput_mwh=total_batt_throughput_mwh,
        battery_efc=battery_efc,
        battery_wear_proxy=battery_wear_proxy,
        peak_grid_mw=float(np.max(p_grid)),
        min_grid_mw=float(np.min(p_grid)),
        max_shaft_torque_pu=shaft_resp.max_torque_pu,
        eval_time_s=eval_time_s,
    )

    return SimulationResult(
        t=t,
        dt_s=dt_s,
        rated_mw=rated_mw,
        p_raw_mw=p_raw,
        p_gpu_mw=p_gpu,
        p_batt_mw=p_batt,
        p_grid_mw=p_grid,
        p_dummy_mw=p_dummy,
        p_curtailed_mw=p_curtailed,
        soc=soc,
        shaft=shaft_resp,
        metrics=metrics,
        initial_grid_mw=initial_grid_mw,
    )
