"""Phase 1 tests: workload synthesis, IEEE FBM shaft model, plant physics, conservation, determinism, speed."""

from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pytest

from rules.checker import check_compliance
from rules.schema import Constraint, RuleSet
from sim.plant import (
    BatteryConfig,
    PlantConfig,
    fixed_floor_controller,
    noop_controller,
    simulate_plant,
)
from sim.shaft import create_illustrative_shaft, load_shaft_model
from workload.synth import WorkloadParams, generate_workload, preset_workloads

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_sandbox_packages_have_no_scipy_or_pymupdf() -> None:
    """Decision #4: sandbox code (workload/, sim/, rules/) uses pure Python + numpy only."""
    forbidden = {"scipy", "fitz", "pymupdf"}
    for pkg in ("workload", "sim", "rules"):
        for py_file in (REPO_ROOT / pkg).glob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        assert top not in forbidden, f"{py_file} imports forbidden module {top}"
                elif isinstance(node, ast.ImportFrom) and node.module:
                    top = node.module.split(".")[0]
                    assert top not in forbidden, f"{py_file} imports forbidden module {top}"


def test_ieee_fbm_modes_within_two_percent() -> None:
    """Gate: computed IEEE FBM torsional modes match published PAS-96 values within 2%."""
    shaft = load_shaft_model()
    assert not shaft.illustrative
    assert len(shaft.computed_modes_hz) == 5

    report = shaft.validate_modes(max_error_pct=2.0)
    assert report["passed"] is True
    assert report["max_error_pct"] < 0.1  # Actual analytical match is <0.02%
    for row in report["modes"]:
        assert row["within_gate"] is True

    dom_sections = [name for name, _ in shaft.modal_dominant_sections()]
    assert dom_sections == ["LPA-LPB", "GEN-EXC", "IP-LPA", "LPB-GEN", "HP-IP"]


def test_shaft_resonance_amplifies_modal_excitation() -> None:
    """Excitation at Mode 1 (15.71 Hz) must produce far greater torque than off-resonance (5 Hz)."""
    shaft = load_shaft_model()
    dt = 0.002
    n = 5000
    t = np.arange(n) * dt

    p_resonant = 100.0 + 10.0 * np.sin(2.0 * np.pi * shaft.computed_modes_hz[0] * t)
    p_off_res = 100.0 + 10.0 * np.sin(2.0 * np.pi * 5.0 * t)

    resp_res = shaft.compute_shaft_response(p_resonant, dt_s=dt)
    resp_off = shaft.compute_shaft_response(p_off_res, dt_s=dt)

    assert resp_res.max_torque_pu > 10.0 * resp_off.max_torque_pu
    assert abs(resp_res.dominant_freq_hz - shaft.computed_modes_hz[0]) < 0.2


def test_illustrative_shaft_fallback_is_labeled() -> None:
    shaft = create_illustrative_shaft(resonant_freq_hz=18.0)
    assert shaft.illustrative is True
    assert len(shaft.computed_modes_hz) == 1
    assert abs(shaft.computed_modes_hz[0] - 18.0) < 1e-6


def test_energy_conservation_lossless_and_lossy() -> None:
    """Verify exact step-by-step energy conservation with GPU floor, cap, and battery limits."""
    trace = generate_workload(preset_workloads(100.0)["standard_training"])

    def active_controller(obs: dict) -> dict[str, float]:
        # Exercises GPU floor, GPU cap, battery discharge, and battery charge across trace
        p_raw = obs["p_raw_mw"]
        target = 75.0
        err = p_raw - target
        return {
            "gpu_floor_mw": 45.0,
            "gpu_cap_mw": 92.0,
            "battery_setpoint_mw": err * 0.9,
        }

    # 1. Lossless battery (eta = 1.0)
    lossless_cfg = PlantConfig(
        battery=BatteryConfig(
            max_power_mw=30.0,
            capacity_mwh=0.05,  # Small capacity to exercise SOC min/max saturation too!
            initial_soc=0.5,
            soc_min=0.1,
            soc_max=0.9,
            eta_charge=1.0,
            eta_discharge=1.0,
        )
    )
    res_lossless = simulate_plant(trace, controller=active_controller, plant_config=lossless_cfg)
    assert res_lossless.metrics.energy_batt_loss_mwh == pytest.approx(0.0, abs=1e-12)
    assert res_lossless.metrics.energy_balance_error_mwh < 1e-12
    assert res_lossless.metrics.energy_dummy_mwh > 0.0
    assert res_lossless.metrics.energy_curtailed_mwh > 0.0
    # Verify GPU ledger: E_gpu == E_raw - E_curt + E_dummy
    assert res_lossless.metrics.energy_gpu_mwh == pytest.approx(
        res_lossless.metrics.energy_raw_mwh
        - res_lossless.metrics.energy_curtailed_mwh
        + res_lossless.metrics.energy_dummy_mwh,
        abs=1e-12,
    )
    # Physical bounds strictly respected
    assert np.all(res_lossless.soc >= 0.1 - 1e-12)
    assert np.all(res_lossless.soc <= 0.9 + 1e-12)
    assert np.all(np.abs(res_lossless.p_batt_mw) <= 30.0 + 1e-12)

    # 2. Lossy battery (90% round-trip efficiency)
    lossy_cfg = PlantConfig(
        battery=BatteryConfig(
            max_power_mw=30.0,
            capacity_mwh=0.05,
            initial_soc=0.5,
            soc_min=0.1,
            soc_max=0.9,
        )
    )
    res_lossy = simulate_plant(trace, controller=active_controller, plant_config=lossy_cfg)
    assert res_lossy.metrics.energy_batt_loss_mwh > 0.0
    assert res_lossy.metrics.energy_balance_error_mwh < 1e-12
    # Independent check directly from output arrays and BatteryConfig efficiencies (non-vacuous):
    dt_h = trace.dt_s / 3600.0
    dis_mwh = float(np.sum(np.maximum(res_lossy.p_batt_mw, 0.0)) * dt_h)
    chg_mwh = float(np.sum(np.maximum(-res_lossy.p_batt_mw, 0.0)) * dt_h)
    expected_loss = dis_mwh * (1.0 / lossy_cfg.battery.eta_discharge - 1.0) + chg_mwh * (
        1.0 - lossy_cfg.battery.eta_charge
    )
    e_init = lossy_cfg.battery.initial_soc * lossy_cfg.battery.capacity_mwh
    e_final = float(res_lossy.soc[-1]) * lossy_cfg.battery.capacity_mwh
    indep_residual = abs(
        float(np.sum(res_lossy.p_grid_mw) * dt_h)
        + (e_init - e_final)
        - (float(np.sum(res_lossy.p_gpu_mw) * dt_h) + expected_loss)
    )
    assert indep_residual < 1e-12
    assert res_lossy.metrics.battery_wear_proxy > 0.0
    assert res_lossy.metrics.battery_efc > 0.0
    assert np.all(res_lossy.soc >= 0.1 - 1e-12)
    assert np.all(res_lossy.soc <= 0.9 + 1e-12)


def test_determinism_end_to_end() -> None:
    """Identical inputs produce bit-for-bit identical traces, plant outputs, shaft torque, and checker verdicts."""
    params = WorkloadParams(cluster_mw=120.0, jitter=0.12, seed=777)
    t1 = generate_workload(params)
    t2 = generate_workload(params)
    assert np.array_equal(t1.t, t2.t)
    assert np.array_equal(t1.p_raw_mw, t2.p_raw_mw)

    shaft = load_shaft_model()
    ctrl = fixed_floor_controller(0.85)
    r1 = simulate_plant(t1, controller=ctrl, shaft_model=shaft)
    r2 = simulate_plant(t2, controller=ctrl, shaft_model=shaft)

    assert np.array_equal(r1.p_grid_mw, r2.p_grid_mw)
    assert np.array_equal(r1.soc, r2.soc)
    assert np.array_equal(r1.shaft.section_torque_pu, r2.shaft.section_torque_pu)

    ruleset = RuleSet(
        rated_mw=120.0,
        constraints=[
            Constraint("ramp_rate", 60.0, "MW/min", page=1, snippet="60 MW/min", window_s=1.0),
            Constraint("dynamic_range", 25.0, "% of rated load", page=1, snippet="25%"),
            Constraint("fluctuation_band", 5.0, "MW", page=2, snippet="5 MW", freq_min_hz=10.0, freq_max_hz=50.0),
        ],
    )
    c1 = check_compliance(r1, ruleset)
    c2 = check_compliance(r2, ruleset)
    assert c1.to_dict() == c2.to_dict()


def test_evaluation_speed_under_half_second() -> None:
    """Gate: one full evaluation (synth + plant + IEEE FBM shaft + checker) runs under 0.5 seconds."""
    shaft = load_shaft_model()
    ruleset = RuleSet(
        rated_mw=100.0,
        constraints=[
            Constraint("ramp_rate", 30.0, "MW/min", page=1, snippet="30 MW/min", window_s=1.0),
            Constraint("max_step", 10.0, "MW", page=1, snippet="10 MW", window_s=1.0),
            Constraint("fluctuation_band", 2.0, "MW", page=2, snippet="2 MW", freq_min_hz=12.0, freq_max_hz=50.0),
            Constraint("dynamic_range", 20.0, "MW", page=2, snippet="20 MW"),
        ],
    )

    import time

    t0 = time.perf_counter()
    trace = generate_workload(preset_workloads(100.0)["standard_training"])
    sim_res = simulate_plant(trace, controller=noop_controller, shaft_model=shaft)
    report = check_compliance(sim_res, ruleset)
    elapsed = time.perf_counter() - t0

    assert elapsed < 0.5, f"Full evaluation took {elapsed:.3f}s, exceeding 0.5s gate"
    assert report.checkable_count == 4


def test_workload_scales_with_cluster_size() -> None:
    """Cluster size scales power trace proportionally."""
    p50 = WorkloadParams(cluster_mw=50.0, seed=10)
    p200 = WorkloadParams(cluster_mw=200.0, seed=10)
    t50 = generate_workload(p50)
    t200 = generate_workload(p200)
    assert np.allclose(t200.p_raw_mw, 4.0 * t50.p_raw_mw, atol=1e-12)


def test_nan_controller_action_is_rejected() -> None:
    trace = generate_workload(WorkloadParams(duration_s=0.1, dt_s=0.01))

    def bad_ctrl(obs: dict) -> dict[str, float]:
        return {"gpu_floor_mw": float("nan"), "gpu_cap_mw": 100.0, "battery_setpoint_mw": 0.0}

    with pytest.raises(ValueError, match="non-finite"):
        simulate_plant(trace, controller=bad_ctrl)
