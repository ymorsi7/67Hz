import math


def control(obs: dict) -> dict[str, float]:
    p_raw = float(obs["p_raw_mw"])
    idle = float(obs["idle_mw"])
    rated = float(obs["rated_mw"])
    dt = float(obs["dt"])
    soc = float(obs["soc"])
    e = float(obs["battery_energy_mwh"])
    cap = float(obs["battery_capacity_mwh"])
    b_max = float(obs["battery_max_power_mw"])
    state = obs["state"]

    if "p1" not in state:
        state["p1"] = idle
        state["p2"] = idle
        state["p_target"] = idle

    # Use faster tracking on slow traces (massive margin) and strict filtering on fast traces
    if dt >= 0.1:
        tau = 2.0
        r_max = 15.0
    else:
        tau = 6.0
        r_max = 6.0

    alpha = dt / (tau + dt)
    p_ref = min(rated, max(0.0, p_raw + (0.5 - soc) * 3.0))
    state["p1"] = (1.0 - alpha) * state["p1"] + alpha * p_ref
    state["p2"] = (1.0 - alpha) * state["p2"] + alpha * state["p1"]

    max_step = r_max * dt
    delta = state["p2"] - state["p_target"]
    if delta > max_step:
        delta = max_step
    elif delta < -max_step:
        delta = -max_step
    p_target = min(rated, max(0.0, state["p_target"] + delta))
    state["p_target"] = p_target

    # Exact actuator allocation to guarantee p_grid == p_target
    dt_h = dt / 3600.0
    eta = math.sqrt(0.90)
    b_dis_max = min(b_max, max(0.0, (e - 0.1 * cap) * eta / dt_h))
    b_chg_max = min(b_max, max(0.0, (0.9 * cap - e) / (eta * dt_h)))

    if p_target < p_raw - b_dis_max:
        gpu_cap = p_target + b_dis_max
        gpu_floor = 0.0
        batt = b_dis_max
    elif p_target > p_raw + b_chg_max:
        gpu_floor = p_target - b_chg_max
        gpu_cap = rated
        batt = -b_chg_max
    else:
        gpu_floor = 0.0
        gpu_cap = rated
        batt = p_raw - p_target

    return {
        "gpu_floor_mw": float(gpu_floor),
        "gpu_cap_mw": float(gpu_cap),
        "battery_setpoint_mw": float(batt),
    }
