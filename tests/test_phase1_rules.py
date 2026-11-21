"""Phase 1 tests: unit conversions and hand-computed deterministic checker cases."""

from __future__ import annotations

import math

import numpy as np
import pytest

from rules.checker import check_trace
from rules.schema import Constraint, RuleSet
from rules.units import (
    UnitConversionError,
    normalize_frequency_hz,
    normalize_power_mw,
    normalize_ramp_rate_mw_per_s,
    normalize_time_s,
)
from sim.shaft import load_shaft_model


class TestUnitConversions:
    def test_power_units_kw_mw_gw_w(self) -> None:
        assert normalize_power_mw(500.0, "kW") == pytest.approx(0.5)
        assert normalize_power_mw(12.5, "MW") == pytest.approx(12.5)
        assert normalize_power_mw(1.5, "GW") == pytest.approx(1500.0)
        assert normalize_power_mw(250_000.0, "W") == pytest.approx(0.25)

    def test_power_percent_of_rated_load(self) -> None:
        assert normalize_power_mw(10.0, "%", rated_mw=150.0) == pytest.approx(15.0)
        assert normalize_power_mw(5.0, "% of rated load", rated_mw=200.0) == pytest.approx(10.0)
        assert normalize_power_mw(2.5, "percent of rated power", rated_mw=80.0) == pytest.approx(2.0)
        assert normalize_power_mw(0.1, "pu", rated_mw=100.0) == pytest.approx(10.0)
        with pytest.raises(UnitConversionError):
            normalize_power_mw(10.0, "% of rated load", rated_mw=None)

    def test_ramp_rate_units(self) -> None:
        # MW/s and MW/min
        assert normalize_ramp_rate_mw_per_s(5.0, "MW/s") == pytest.approx(5.0)
        assert normalize_ramp_rate_mw_per_s(60.0, "MW/min") == pytest.approx(1.0)
        assert normalize_ramp_rate_mw_per_s(30.0, "MW per minute") == pytest.approx(0.5)
        # kW/s and kW/min
        assert normalize_ramp_rate_mw_per_s(2000.0, "kW/s") == pytest.approx(2.0)
        assert normalize_ramp_rate_mw_per_s(6000.0, "kW/min") == pytest.approx(0.1)
        assert normalize_ramp_rate_mw_per_s(1500.0, "kW per second") == pytest.approx(1.5)
        # Percent of rated load per second / per minute
        assert normalize_ramp_rate_mw_per_s(10.0, "%/min", rated_mw=120.0) == pytest.approx(0.2)
        assert normalize_ramp_rate_mw_per_s(
            5.0, "% of rated load per second", rated_mw=100.0
        ) == pytest.approx(5.0)
        assert normalize_ramp_rate_mw_per_s(
            6.0, "percent of rated load per minute", rated_mw=200.0
        ) == pytest.approx(0.2)

    def test_time_and_frequency_units(self) -> None:
        assert normalize_time_s(500.0, "ms") == pytest.approx(0.5)
        assert normalize_time_s(2.0, "minutes") == pytest.approx(120.0)
        assert normalize_time_s(1.0, "hr") == pytest.approx(3600.0)
        assert normalize_frequency_hz(500.0, "mHz") == pytest.approx(0.5)
        assert normalize_frequency_hz(60.0, "Hz") == pytest.approx(60.0)
        assert normalize_frequency_hz(120.0, "cpm") == pytest.approx(2.0)
        assert normalize_frequency_hz(2.0 * math.pi * 15.0, "rad/s") == pytest.approx(15.0)

    def test_invalid_unit_raises(self) -> None:
        with pytest.raises(UnitConversionError):
            normalize_power_mw(10.0, "furlongs")
        with pytest.raises(UnitConversionError):
            normalize_ramp_rate_mw_per_s(10.0, "MW")


class TestHandComputedCheckerCases:
    def test_ramp_rate_exact_at_limit_and_just_over(self) -> None:
        # Linear ramp from 50 MW to 100 MW over 5 seconds -> exact ramp = 10.0 MW/s = 600 MW/min
        dt = 0.5
        p = np.array([50.0 + 10.0 * (i * dt) for i in range(11)], dtype=np.float64)

        # Explicit 0.5s window (trace 5.5s >= 2*0.5s) -> exact 600 MW/min = 10.0 MW/s PASS with margin 0.0
        rs_exact = RuleSet(
            rated_mw=100.0,
            constraints=[Constraint("ramp_rate", 600.0, "MW/min", page=1, snippet="600 MW/min", window_s=0.5)],
        )
        rep_exact = check_trace(p, dt_s=dt, ruleset=rs_exact)
        assert rep_exact.passed is True
        assert rep_exact.results[0].measured == pytest.approx(10.0, abs=1e-12)
        assert rep_exact.results[0].limit == pytest.approx(10.0, abs=1e-12)
        assert rep_exact.results[0].margin == pytest.approx(0.0, abs=1e-12)

        # Tighter limit (599.4 MW/min = 9.99 MW/s) -> FAIL with margin -0.01 MW/s
        rs_fail = RuleSet(
            rated_mw=100.0,
            constraints=[Constraint("ramp_rate", 599.4, "MW/min", page=1, snippet="599.4 MW/min", window_s=0.5)],
        )
        rep_fail = check_trace(p, dt_s=dt, ruleset=rs_fail)
        assert rep_fail.passed is False
        assert rep_fail.results[0].margin == pytest.approx(-0.01, abs=1e-9)

    def test_ramp_rate_windowed(self) -> None:
        # Steps +5 MW every 0.5s, then flat (8 samples = 4.0s >= 2*2.0s): over 2s window max delta = 10 MW -> 5.0 MW/s
        dt = 0.5
        p = np.array([100.0, 105.0, 110.0, 110.0, 110.0, 110.0, 110.0, 110.0], dtype=np.float64)
        rs = RuleSet(
            rated_mw=200.0,
            constraints=[
                Constraint("ramp_rate", 5.0, "MW/s", page=2, snippet="5 MW/s over 2s", window_s=2.0)
            ],
        )
        rep = check_trace(p, dt_s=dt, ruleset=rs)
        assert rep.passed is True
        assert rep.results[0].measured == pytest.approx(5.0, abs=1e-12)
        assert rep.results[0].margin == pytest.approx(0.0, abs=1e-12)

    def test_short_trace_returns_not_evaluable_and_mw_per_min_assumes_60s(self) -> None:
        # MW/min with no stated window assumes 60 s rolling window and records assumption
        c = Constraint("ramp_rate", 20.0, "MW/min", page=14, snippet="ERCOT: 20 MW/min")
        assert c.window_s == pytest.approx(60.0)
        assert any("60 s" in a for a in c.assumptions)

        # 10 s trace (< 2 * 60 s = 120 s) MUST return not_evaluable (passed=None), never pass or fail
        p_short = np.full(20, 50.0, dtype=np.float64)
        rs = RuleSet(rated_mw=100.0, constraints=[c])
        rep_short = check_trace(p_short, dt_s=0.5, ruleset=rs)
        assert rep_short.status == "not_evaluable"
        assert rep_short.passed is False
        assert rep_short.not_evaluable_count == 1
        assert rep_short.evaluable_count == 0
        assert rep_short.results[0].status == "not_evaluable"
        assert rep_short.results[0].passed is None
        assert any("60 s" in a for a in rep_short.assumptions)

        # 120 s trace (>= 2 * 60 s) IS evaluable and passes steady signal
        p_long = np.full(240, 50.0, dtype=np.float64)
        rep_long = check_trace(p_long, dt_s=0.5, ruleset=rs)
        assert rep_long.status == "pass"
        assert rep_long.passed is True
        assert rep_long.evaluable_count == 1

    def test_idle_initial_condition_prepended_catches_t0_jump(self) -> None:
        # Facility starts at idle 15 MW; jumping immediately to 72 MW at t=0 must FAIL ramp & step rules!
        dt = 0.5
        p_jump = np.full(240, 72.0, dtype=np.float64)  # 120 s constant at 72 MW
        rs = RuleSet(
            rated_mw=100.0,
            constraints=[
                Constraint("ramp_rate", 20.0, "MW/min", page=14, snippet="20 MW/min", window_s=60.0),
                Constraint("max_step", 10.0, "MW", page=23, snippet="10 MW over 5s", window_s=5.0),
            ],
        )
        rep_jump = check_trace(p_jump, dt_s=dt, ruleset=rs, initial_power_mw=15.0)
        assert rep_jump.passed is False
        assert rep_jump.failed_count == 2
        assert rep_jump.results[0].measured == pytest.approx((72.0 - 15.0) / 60.0)
        assert rep_jump.results[1].measured == pytest.approx(72.0 - 15.0)

        # Smooth legal ramp from 15 MW at 18 MW/min (0.3 MW/s) -> PASSES both rules!
        t = np.arange(240, dtype=np.float64) * dt
        p_ramp = np.minimum(72.0, 15.0 + 0.3 * (t + dt))
        rep_ramp = check_trace(p_ramp, dt_s=dt, ruleset=rs, initial_power_mw=15.0)
        assert rep_ramp.passed is True

    def test_window_range_and_subsynchronous_band_assumptions(self) -> None:
        # Step 7: window range "4–6 second interval" -> uses larger bound 6.0 s and records assumption
        c_range = Constraint(
            "max_step",
            10.0,
            "MW",
            page=22,
            snippet="Southern Company: Requires active power variations to remain below 10 MW over a 4–6 second interval.",
        )
        assert c_range.window_s == pytest.approx(6.0)
        assert any("4-6 s" in a and "6 s" in a for a in c_range.assumptions)

        # Step 8: IESO sub-synchronous oscillation without Hz bounds -> assumes 5.0-55.0 Hz on constraint + report
        c_ieso = Constraint(
            "fluctuation_band",
            0.25,
            "% of rated load",
            page=8,
            snippet="no periodic power oscillation within the sub-synchronous frequency band exceeding +/-0.25%",
        )
        assert c_ieso.freq_min_hz == pytest.approx(5.0)
        assert c_ieso.freq_max_hz == pytest.approx(55.0)
        assert any("5-55 Hz" in a for a in c_ieso.assumptions)

        # On slow trace (dt=0.5s, Nyquist=1.0 Hz), 5-55 Hz band MUST be not_evaluable (not vacuous pass!)
        p_slow = np.full(200, 50.0, dtype=np.float64)
        rep_slow = check_trace(p_slow, dt_s=0.5, ruleset=RuleSet(rated_mw=100.0, constraints=[c_ieso]))
        assert rep_slow.status == "not_evaluable"
        assert rep_slow.results[0].status == "not_evaluable"
        assert any("5-55 Hz" in a for a in rep_slow.assumptions)

    def test_max_step_exact_at_limit_single_and_windowed(self) -> None:
        # Single-step jump of exactly 15 MW (15,000 kW)
        dt = 0.25
        p = np.array([80.0, 80.0, 95.0, 95.0, 85.0], dtype=np.float64)
        rs_exact = RuleSet(
            rated_mw=100.0,
            constraints=[Constraint("max_step", 15_000.0, "kW", page=3, snippet="15,000 kW")],
        )
        rep = check_trace(p, dt_s=dt, ruleset=rs_exact)
        assert rep.passed is True
        assert rep.results[0].measured == pytest.approx(15.0, abs=1e-12)
        assert rep.results[0].margin == pytest.approx(0.0, abs=1e-12)

        # Windowed max_step: two 7.5 MW upward steps within 0.5s -> 15.0 MW total swing
        p_two = np.array([80.0, 87.5, 95.0, 95.0, 95.0], dtype=np.float64)
        rs_win_pass = RuleSet(
            rated_mw=100.0,
            constraints=[Constraint("max_step", 15.0, "MW", page=3, snippet="15 MW / 0.5s", window_s=0.5)],
        )
        rep_win_pass = check_trace(p_two, dt_s=0.25, ruleset=rs_win_pass)
        assert rep_win_pass.passed is True
        assert rep_win_pass.results[0].margin == pytest.approx(0.0, abs=1e-12)

        rs_win_fail = RuleSet(
            rated_mw=100.0,
            constraints=[Constraint("max_step", 14.9, "MW", page=3, snippet="14.9 MW / 0.5s", window_s=0.5)],
        )
        rep_win_fail = check_trace(p_two, dt_s=0.25, ruleset=rs_win_fail)
        assert rep_win_fail.passed is False
        assert rep_win_fail.results[0].margin == pytest.approx(-0.1, abs=1e-9)

    def test_fluctuation_band_exact_sinusoid_and_shaft_torque(self) -> None:
        # Pure 20 Hz sine wave with exact amplitude 5.0 MW over 2.0 seconds (40 exact cycles)
        dt = 0.002
        n = 1000
        t = np.arange(n) * dt
        p = 100.0 + 5.0 * np.sin(2.0 * np.pi * 20.0 * t)

        # Limit = 5% of 100 MW = 5.0 MW in [15, 25] Hz -> exact boundary PASS
        rs_exact = RuleSet(
            rated_mw=100.0,
            constraints=[
                Constraint(
                    "fluctuation_band",
                    5.0,
                    "% of rated load",
                    page=4,
                    snippet="5% in 15-25 Hz",
                    freq_min_hz=15.0,
                    freq_max_hz=25.0,
                ),
                # Out-of-band [30, 45] Hz should see ~0 MW
                Constraint(
                    "fluctuation_band",
                    0.01,
                    "MW",
                    page=4,
                    snippet="0.01 MW in 30-45 Hz",
                    freq_min_hz=30.0,
                    freq_max_hz=45.0,
                ),
            ],
        )
        rep = check_trace(p, dt_s=dt, ruleset=rs_exact)
        assert rep.passed is True
        assert rep.results[0].measured == pytest.approx(5.0, abs=1e-10)
        assert rep.results[0].margin == pytest.approx(0.0, abs=1e-10)
        assert rep.results[1].measured == pytest.approx(0.0, abs=1e-10)

        # Just under 5.0 MW -> FAIL
        rs_fail = RuleSet(
            rated_mw=100.0,
            constraints=[
                Constraint(
                    "fluctuation_band",
                    4.95,
                    "MW",
                    page=4,
                    snippet="4.95 MW in 15-25 Hz",
                    freq_min_hz=15.0,
                    freq_max_hz=25.0,
                )
            ],
        )
        rep_fail = check_trace(p, dt_s=dt, ruleset=rs_fail)
        assert rep_fail.passed is False
        assert rep_fail.results[0].margin == pytest.approx(-0.05, abs=1e-9)

        # Shaft torque pu check
        shaft = load_shaft_model()
        shaft_resp = shaft.compute_shaft_response(p, dt_s=dt)
        rs_pu = RuleSet(
            rated_mw=100.0,
            constraints=[
                Constraint("fluctuation_band", shaft_resp.max_torque_pu, "pu", page=5, snippet="exact pu")
            ],
        )
        rep_pu = check_trace(p, dt_s=dt, ruleset=rs_pu, shaft=shaft_resp)
        assert rep_pu.passed is True
        assert rep_pu.results[0].margin == pytest.approx(0.0, abs=1e-12)

    def test_dynamic_range_exact_at_limit(self) -> None:
        # min=80, max=100 -> exact dynamic range = 20.0 MW = 20% of 100 MW
        p = np.array([80.0, 95.0, 100.0, 85.0], dtype=np.float64)
        rs_pass = RuleSet(
            rated_mw=100.0,
            constraints=[Constraint("dynamic_range", 20.0, "% of rated load", page=6, snippet="20%")],
        )
        rep_pass = check_trace(p, dt_s=1.0, ruleset=rs_pass)
        assert rep_pass.passed is True
        assert rep_pass.results[0].measured == pytest.approx(20.0, abs=1e-12)
        assert rep_pass.results[0].margin == pytest.approx(0.0, abs=1e-12)

        rs_fail = RuleSet(
            rated_mw=100.0,
            constraints=[Constraint("dynamic_range", 19.99, "MW", page=6, snippet="19.99 MW")],
        )
        rep_fail = check_trace(p, dt_s=1.0, ruleset=rs_fail)
        assert rep_fail.passed is False
        assert rep_fail.results[0].margin == pytest.approx(-0.01, abs=1e-9)

    def test_uncheckable_and_needs_human_review_semantics(self) -> None:
        p = np.array([90.0, 91.0, 90.0], dtype=np.float64)
        rs = RuleSet(
            rated_mw=100.0,
            constraints=[
                Constraint("dynamic_range", 5.0, "MW", page=1, snippet="5 MW"),
                Constraint(
                    "other",
                    None,
                    "",
                    page=2,
                    snippet="Customer shall coordinate maintenance windows with utility.",
                    checkable=False,
                ),
            ],
        )
        rep = check_trace(p, dt_s=1.0, ruleset=rs)
        assert rep.passed is True
        assert rep.checkable_count == 1
        assert rep.uncheckable_count == 1

        # If any constraint needs human review, strict_review refuses automatic certification
        rs_review = RuleSet(
            rated_mw=100.0,
            constraints=[
                Constraint(
                    "dynamic_range",
                    5.0,
                    "MW",
                    page=1,
                    snippet="blurry 5 MW",
                    confidence=0.45,
                    needs_human_review=True,
                ),
            ],
        )
        rep_rev = check_trace(p, dt_s=1.0, ruleset=rs_review, strict_review=True)
        assert rep_rev.passed is False
        assert rep_rev.needs_review_count == 1
        assert rep_rev.refusal_reason is not None

    def test_fluctuation_band_aggregations_hand_computed(self) -> None:
        """peak_bin vs adjacent_pair_sum vs band_sum on adjacent FFT bins (hand-computed)."""
        from rules.checker import _aggregate_band_amps

        # Direct aggregation unit check (exact)
        amps = np.array([1.0, 3.0, 5.0, 2.0], dtype=np.float64)
        freqs = np.array([1.0, 1.05, 1.10, 1.15], dtype=np.float64)
        assert _aggregate_band_amps(amps, freqs, "peak_bin")[0] == pytest.approx(5.0)
        assert _aggregate_band_amps(amps, freqs, "adjacent_pair_sum")[0] == pytest.approx(8.0)  # 3+5
        assert _aggregate_band_amps(amps, freqs, "band_sum")[0] == pytest.approx(11.0)

        dt = 0.01
        t = np.arange(0.0, 20.0, dt)  # df = 0.05 Hz
        # Two tones in adjacent bins: 1.00 Hz (amp 3) and 1.05 Hz (amp 5)
        p = 50.0 + 3.0 * np.sin(2 * math.pi * 1.00 * t) + 5.0 * np.sin(2 * math.pi * 1.05 * t)

        c_peak = Constraint(
            "fluctuation_band",
            5.0,
            "MW",
            page=1,
            snippet="peak bin limit",
            freq_min_hz=0.5,
            freq_max_hz=3.0,
            aggregation="peak_bin",
        )
        c_adj = Constraint(
            "fluctuation_band",
            8.0,
            "MW",
            page=1,
            snippet="The P0-pk for the sum of any two adjacent frequency bins shall not exceed 8 MW",
            freq_min_hz=0.5,
            freq_max_hz=3.0,
            aggregation="adjacent_pair_sum",
        )
        c_sum = Constraint(
            "fluctuation_band",
            8.0,
            "MW",
            page=1,
            snippet="The P0-pk for the sum of all bins shall not exceed 8 MW",
            freq_min_hz=0.5,
            freq_max_hz=3.0,
            aggregation="band_sum",
        )
        c_infer = Constraint(
            "fluctuation_band",
            8.0,
            "MW",
            page=12,
            snippet="Low Subsynchronous (0.1 - 5.0 Hz): sum of any two adjacent frequency bins shall not exceed 10.0 MW",
            freq_min_hz=0.1,
            freq_max_hz=5.0,
        )
        assert c_infer.aggregation == "adjacent_pair_sum"

        rep_peak = check_trace(p, dt_s=dt, ruleset=RuleSet(rated_mw=100.0, constraints=[c_peak]))
        assert rep_peak.results[0].measured == pytest.approx(5.0, rel=0.08, abs=0.4)

        rep_adj = check_trace(p, dt_s=dt, ruleset=RuleSet(rated_mw=100.0, constraints=[c_adj]))
        assert rep_adj.results[0].measured == pytest.approx(8.0, rel=0.10, abs=0.6)
        assert rep_adj.results[0].status == "pass"

        rep_sum = check_trace(p, dt_s=dt, ruleset=RuleSet(rated_mw=100.0, constraints=[c_sum]))
        # band_sum >= adjacent_pair_sum (>= 8); peak_bin (~5) is strictly smaller
        assert rep_sum.results[0].measured >= rep_adj.results[0].measured - 0.2
        assert rep_peak.results[0].measured < rep_sum.results[0].measured - 1.0

    def test_lipa_60s_band_not_evaluable_on_20s_fast_trace(self) -> None:
        c = Constraint(
            "fluctuation_band",
            10.0,
            "MW",
            page=12,
            snippet="sum of any two adjacent frequency bins shall not exceed 10.0 MW0-pk,avg (60 second rolling window).",
            notes="10 s FFT window",
            freq_min_hz=0.1,
            freq_max_hz=5.0,
            window_s=60.0,
            fft_window_s=10.0,
        )
        assert c.aggregation == "adjacent_pair_sum"
        assert c.averaging_window_s == pytest.approx(60.0)
        p20 = np.full(10_000, 50.0)  # 20 s @ 0.002
        rep = check_trace(p20, dt_s=0.002, ruleset=RuleSet(rated_mw=100.0, constraints=[c]))
        assert rep.results[0].status == "not_evaluable"
        # band_long 120 s @ 0.01 is evaluable
        p120 = np.full(12_000, 50.0)
        rep2 = check_trace(p120, dt_s=0.01, ruleset=RuleSet(rated_mw=100.0, constraints=[c]))
        assert rep2.results[0].status in {"pass", "fail"}
        assert rep2.results[0].status != "not_evaluable"
