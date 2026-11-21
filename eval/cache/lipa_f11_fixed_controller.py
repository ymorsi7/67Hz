"""Baseline B3: low-pass filter + battery policy tuned by random search (no LLM)."""
import math

TAU_S = 0.265840
ORDER = 2
SLEW_MW_S = 2.636541
SOC_KP = 0.599852
SOC_TARGET = 0.740383
PRELOAD_MW = 0.000000
EXACT_ACT = True

def control(obs: dict) -> dict[str, float]:
    p_raw = float(obs["p_raw_mw"])
    rated = float(obs["rated_mw"])
    dt = float(obs["dt"])
    soc = float(obs["soc"])
    b_max = float(obs["battery_max_power_mw"])
    cap = float(obs["battery_capacity_mwh"])
    e = float(obs["battery_energy_mwh"])
    idle = float(obs.get("idle_mw", obs["p_prev_grid_mw"]))
    st = obs["state"]

    if "y1" not in st:
        st["y1"] = idle
        st["y2"] = idle
        st["pg"] = idle

    desired_in = p_raw + (PRELOAD_MW if (p_raw <= idle + 2.0 and soc < 0.85) else 0.0)
    desired_in = min(rated, max(0.0, desired_in + SOC_KP * (SOC_TARGET - soc) * b_max))
    alpha = dt / (TAU_S + dt)
    st["y1"] = (1.0 - alpha) * st["y1"] + alpha * desired_in
    if ORDER >= 2:
        st["y2"] = (1.0 - alpha) * st["y2"] + alpha * st["y1"]
        filt = st["y2"]
    else:
        filt = st["y1"]

    max_d = SLEW_MW_S * dt
    p_target = st["pg"] + max(-max_d, min(max_d, filt - st["pg"]))
    p_target = min(rated, max(0.0, p_target))
    st["pg"] = p_target

    if not EXACT_ACT:
        batt = max(-b_max, min(b_max, p_raw - p_target))
        return {"gpu_floor_mw": 0.0, "gpu_cap_mw": rated, "battery_setpoint_mw": batt}

    dt_h = dt / 3600.0
    eta = math.sqrt(0.90)
    b_dis_max = min(b_max, max(0.0, (e - 0.1 * cap) * eta / dt_h))
    b_chg_max = min(b_max, max(0.0, (0.9 * cap - e) / (eta * dt_h)))
    if p_target < p_raw - b_dis_max:
        return {"gpu_floor_mw": 0.0, "gpu_cap_mw": p_target + b_dis_max, "battery_setpoint_mw": b_dis_max}
    if p_target > p_raw + b_chg_max:
        return {"gpu_floor_mw": p_target - b_chg_max, "gpu_cap_mw": rated, "battery_setpoint_mw": -b_chg_max}
    return {"gpu_floor_mw": 0.0, "gpu_cap_mw": rated, "battery_setpoint_mw": p_raw - p_target}
