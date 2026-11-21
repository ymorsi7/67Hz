# GridSafe Failure Log (`FAILURES.md`)

Tracks real failure modes discovered during development, live runs on real utility PDFs (`specs/`), and degradation/red-team tests. Every before/after number comes directly from runs in this repo.

---

## F1. Adjacent Frequency Bands Falsely Flagged as Conflicting

- **Category:** Rule extraction / conflict detection (`reader/extract.py`)
- **Example:** CAISO Straw Proposal (`Large-Load-Technical-Requirements-Straw-Proposal-Jun-15-2026.pdf`, p. 23 & p. 25) cites LIPA's two complementary frequency-domain limits:
  - `0.1–5.0 Hz`: `< 10.0 MW`
  - `5.0–55.0 Hz`: `< 3.5 MW`
  Because `_bands_overlap()` used closed-interval comparison (`max(a_lo, b_lo) <= min(a_hi, b_hi)`), sharing the single boundary point `5.0 Hz` caused all four LIPA band entries (`fluctuation_band_21`, `22`, `25`, `26`) to be falsely marked `conflict_flag=True`.
- **What changed:** Updated `_bands_overlap()` in `reader/extract.py` to require a strictly positive interior frequency overlap (`max(a_lo, b_lo) < min(a_hi, b_hi) - 1e-9`) unless both bands are degenerate single-frequency points.
- **Before vs After (CAISO Straw Proposal):**
  - False conflict flags on LIPA adjacent bands: **4 / 4 flagged** -> **0 / 4 flagged**.
  - True conflicts between different numeric limits in the same band/window (e.g., ERCOT 20 MW/min vs ATC 0.5 MW/s vs AESO 10 MW/min on p. 14): **still 100% detected** (`conflict_flag=True`).

---

## F2. Ambiguous "To Be Determined" Pages & Misquoted Snippets Passing Default Threshold (`0.75`)

- **Category:** Verifier calibration / abstention (`reader/verify.py`, `config/models.yaml`)
- **Example:** On the dev split (`CAISO` 38 pp. + `ESIG` 18 pp., `n = 50` extracted constraints), five entries were either extracted from CAISO sections explicitly titled `"Requirement: To be determined"` while quoting `"Industry Practice"` bullets (CAISO p. 27 `other_28` conf `0.5259`; p. 29 `other_29` conf `0.7866`; p. 26 `other_27` conf `0.8100`), condensed table prose (CAISO p. 6 `other_0` conf `0.7666`), or had a verbatim quote typo (`"s proposal"` instead of `"a proposal"`, ESIG p. 13 `other_13` conf `0.8328`). Meanwhile, all 44 unambiguous requirements (including 12/12 checkable numeric limits on CAISO) had `confidence >= 0.900` (`[0.900, 0.960]`).
- **What changed:** Calibrated `confidence_threshold` strictly on the dev split from default `0.75` to `0.85` in `config/models.yaml` (holding out `2601.12686v1.pdf` completely from threshold selection).
- **Before vs After (Dev split, `n = 50`):**
  - Ambiguous/TBD/misquoted items caught as `Needs human review` (abstained): **1 / 5 (20%)** at `0.75` -> **5 / 5 (100%)** at `0.85`.
  - Checkable numeric constraints on dev falsely abstained: **0 / 12 (0%)** before -> **0 / 12 (0%)** after (all 12 CAISO checkable constraints have `confidence >= 0.918`).
  - Held-out (`2601.12686v1.pdf`, `n = 23`) checkable constraint (`20 MW/min`, p. 4): `confidence = 0.9585` (still verified; all 23/23 held-out items have `confidence >= 0.914`).

---

## F3. Heavily Degraded / Unreadable Page Images Could Return Confident "No" Instead of Abstaining

- **Category:** Multimodal image robustness (`reader/verify.py`, `eval/degrade.py`)
- **Example:** Under heavy camera degradation (`eval/degrade.py` Severity 3: `11°` rotation, `blur_sigma=2.5`, `0.35x` resolution; Severity 4: `18°` rotation, `blur_sigma=5.0`, `0.16x` resolution) applied to CAISO p. 13 (`20 MW/min`), a single-question yes/no verifier can either barely guess `yes` (`p_yes = 0.8786` at Sev 3) or answer confident `no` (`p_yes = 0.2886` at Sev 4) when text is illegible.
- **What changed:** Added a parallel `page_readable` `noul` question inside the **same** batched per-page Decisions API call (zero extra latency, zero output tokens) with `readability_threshold = 0.60`. Any page with `page_readable_p < 0.60` forces abstention (`status="abstained"`, `needs_human_review=True`) instead of returning a silent pass or rejection.
- **Before vs After (CAISO p. 13 degradation sweep, Severities 1–4):**
  - Severity 1 (`page_readable_p = 0.8649`): Verified (`p_yes = 0.9594`).
  - Severity 2 (`page_readable_p = 0.8423`): Verified (`p_yes = 0.9379`).
  - Severity 3 (`page_readable_p = 0.2579`): Without readability gate would auto-verify (`0.8786 >= 0.85`) -> **Abstained (`Needs human review`)** with readability gate.
  - Severity 4 (`page_readable_p = 0.1314`): Reader sets `unreadable=True` (0 extracted) and Verifier **Abstains (`Needs human review`)** instead of guessing.

---

## F4. Subtle Unit Swaps & Fabricated Numeric Limits (Negative Controls)

- **Category:** Hallucination defense (`reader/verify.py`)
- **Example:** Testing `gemini-decision-1` against CAISO p. 13 (true limit: `20 MW/min`) with three adversarial hallucinations: wrong value (`50 MW/min`), subtle unit swap (`20 MW/s`), and invented limit (`max_step = 5 MW`).
- **What changed:** Included explicit `criteria` (`true_description` / `false_description`) in the batched `noul` Verifier prompt requiring exact numeric value AND unit match.
- **Measured result (CAISO p. 13 negative controls):**
  - True limit (`20 MW/min`): `p_yes = 0.9543` -> **Verified**.
  - Wrong value (`50 MW/min`): `p_yes = 0.0339` (`conf = 0.9661`) -> **Rejected (0% slip-through)**.
  - Wrong unit (`20 MW/s`): `p_yes = 0.0596` (`conf = 0.9404`) -> **Rejected (0% slip-through)**.
  - Invented limit (`5 MW` max step): `p_yes = 0.0460` (`conf = 0.9540`) -> **Rejected (0% slip-through)**.

---

## F5. Single-Shot Baseline (B0) Cold-Start Anchoring Causes 16–21% GPU Throughput Loss

- **Category:** Controller design / throughput curtailment (`evolve/baselines.py`, `evolve/loop.py`)
- **Example:** On all 7 utility profiles (CAISO, ERCOT, ATC, AESO, Southern-dev, LIPA, Southern-held_out), single-shot `gemini-4-neon` (B0, no checker feedback loop) anchored `p_target` at `p_prev_grid_mw = p_raw[0] = 15 MW` (idle) and rate-limited upward at `<= 20 MW/min`. Over a 10 s active training startup (`15 -> 100 MW`), even with full `40 MW` battery discharge, the cluster was capped around `~58 MW`, curtailing **34–37% of GPU training compute** on `standard_training` and `abrupt_checkpoint_drop` (**15.9–21.1% mean throughput loss** across all 4 workloads).
- **What changed:** Added diverse parallel mutation strategies (`min_overhead_center`, `slew_battery_first`, `window_envelope_guard`, `spectral_notch_smooth`, `soc_balanced_low_wear`, `high_margin_robust`) and a composite score penalizing throughput loss (`3x`) + energy overhead (`1x`) + battery wear (`50x`) inside `evolve/loop.py`.
- **Before vs After (7 profiles x 4 workloads):**
  - Mean GPU throughput loss: **15.9%–21.1%** (B0) -> **0.0%** across all 7/7 profiles (GridSafe Hydrogen evolution).
  - *Disclosed physical trade-off:* GridSafe achieves `0.0%` curtailment by pre-positioning `p_target` near the mean active training draw (`~65–72 MW`) at `t = 0` (steady-state pre-warmed operation, matching how B1's `90 MW` floor also starts at `t = 0`), whereas a strict cold start from `15 MW` idle over a 10 s window under `20 MW/min` physically requires curtailment.

---

## F6. Evolve Loop Overrunning 75 s Wall-Clock Budget Under `medium` Thinking Level

- **Category:** Latency / budget enforcement (`evolve/loop.py`, `config/models.yaml`)
- **Example:** On the first live AESO evolution run with `gemini-4-hydrogen` at `thinking_level="medium"`, generation 1 took `33.5 s` and generation 2 started with `41.3 s` remaining (above the `35 s` downgrade threshold) and finished at **`84.8 s`**, exceeding the `75 s` budget gate.
- **What changed:** Switched Designer default in `config/models.yaml` to `thinking_level: low` for fast parallel mutations and added a hard `as_completed(futs, timeout=remaining_budget)` wall-clock guard in `evolve/loop.py` with `pool.shutdown(wait=False, cancel_futures=True)`.
- **Before vs After (live 7-profile benchmark):**
  - Per-profile evolution runtime: **`84.8 s`** (over 75 s budget) -> **`26.5 s – 44.8 s`** across all 7 profiles (100% strictly under 75 s budget, 2–3 full generations of 6 parallel workers each).

---

## F7. High Candidate Rejection Rate on LIPA Dual Frequency-Band Limits

- **Category:** Frequency-domain compliance (`evolve/loop.py`, LIPA profile)
- **Example:** On LIPA (`0.1–5.0 Hz < 10 MW` AND `5.0–55.0 Hz < 3.5 MW`), **12 of 13** candidate controllers generated by `gemini-4-hydrogen` failed the deterministic FFT checker (either leaking `0.1–5 Hz` step/ramp content from `p_raw[0]=15 MW` or allowing residual high-frequency ripple), whereas single-shot B0 passed `standard_training` with only `1.7%` margin (`9.83 MW` vs `10.0 MW` limit) and `26.3%` throughput loss.
- **What changed:** Parallel `min_overhead_center` strategy combined with the deterministic FFT gate selected a `tau = 10 s` (`fc ≈ 0.016 Hz << 0.1 Hz`) low-pass target centered near mean load.
- **Before vs After (LIPA, 4 workloads):**
  - Uncontrolled: **0 / 4 pass** (`18.25 MW` in `0.1–5 Hz`, `31.47 MW` in `5–55 Hz`).
  - B1 (90% floor): **2 / 4 pass** (fails SSR at `6.11 MW > 3.5 MW`, `29.6%` overhead).
  - B0 (Neon 1x): **4 / 4 pass**, `1.7%` worst margin, `15.9%` mean throughput loss.
  - GridSafe (Hydrogen evolve): **4 / 4 pass**, **`76.0%` worst margin**, **`0.0%` throughput loss**, `3.6%` energy overhead, peak shaft torque `0.0065 pu`.

---

## F8. Single-Shot Baseline (B0) Fails Tight Held-Out IESO Periodic Oscillation Limit (`0.25%` Rated Load)

- **Category:** Multi-constraint held-out generalization (`evolve/loop.py`, `ieso_held_out`)
- **Example:** On the newly added held-out IESO spec (`LargeComputationalLoads-20260501-Technical-Requirements.pdf`, May 1, 2026 — never used in development), four verified checkable constraints bind simultaneously, including `<= 0.25% of rated load` (`0.25 MW` on 100 MW) periodic oscillation (p. 8) and `<= 20 MW/min` ramping (p. 8). Without checker feedback, single-shot B0 (`gemini-4-neon`) failed **0 / 4** workloads while incurring **21.9%** throughput loss; even Generation 1 of GridSafe Hydrogen evolution failed **0 / 6** candidates on the `0.25 MW` oscillation limit.
- **What changed:** The deterministic checker fed the exact failing `fluctuation_band_5` (`0.25 MW`) margin back into Generation 2 of parallel `gemini-4-hydrogen` mutations.
- **Before vs After (`ieso_held_out`, 4 workloads, zero tuning on held-out):**
  - Uncontrolled: **0 / 4 pass** (failed `fluctuation_band_4`, `fluctuation_band_5`, `ramp_rate_7`).
  - B1 (90% floor): **0 / 4 pass** (`29.6%` energy overhead).
  - B0 (Neon single-shot): **0 / 4 pass** (`21.9%` throughput loss).
  - GridSafe Gen 1 (before feedback): **0 / 6** candidates passed.
  - GridSafe Gen 2–3 (after checker-margin feedback): **5 / 19** candidates passed -> winning controller **4 / 4 pass**, **`3.6%` energy overhead**, **`0.0%` throughput loss**, **`0.00224` wear** in `54.6 s`.

---

## F9. Window-Length Mismatch & Non-Binding 100 MW CAISO 10-Minute Window (Phase 3b Validity Fix)

- **Category:** Evaluation validity / checker window semantics (`rules/checker.py`, `workload/synth.py`)
- **Example:** In Phase 3, CAISO's `20 MW/min` ramp limit over a **600 s (10-minute)** rolling window (p. 13) was judged on a **10 s** trace (`|p[-1] - p[0]| / 10 s`), making an uncontrolled 100 MW cluster appear to ramp at `510 MW/min` and fail `0 / 4`. Meanwhile, unwindowed `MW/min` rules (AESO, ERCOT, IESO, Southern held-out, SPP) were judged sample-to-sample at `dt = 0.002 s`.
- **What changed:**
  - Any rule with `trace_duration < 2 * window_s` (or frequency band outside trace Nyquist / `< 2` cycles) now returns `status="not_evaluable"` (`passed=None`) — never pass or fail.
  - Per-minute ramp rules (`MW/min`, `%/min`, `kW/min`) with no stated window default to a `60 s` rolling window, explicitly recorded on `Constraint.assumptions` and certificates.
  - Added a 30-minute slow scenario family (`1800 s` @ `0.5 s`, 3 workloads covering idle, job start, checkpoints, job stop) and lengthened fast traces to `20 s` (`2x` the largest `10 s` fast window). One full evaluation stays well under `0.5 s` (`mean = 18.6 ms`, `max = 36.8 ms` fast / `10.9 ms` slow).
- **Before vs After (what changed / got worse):**
  - **CAISO at 100 MW:** `20 MW/min` averaged over `600 s` allows `200 MW` change per 10 minutes. An uncontrolled `100 MW` cluster (`15 -> 100 MW`, max swing `85 MW`) can never exceed `8.5 MW/min` over 10 min, so **Noop and B1 now PASS CAISO at 100 MW** (previously falsely failed `0/4` on 10 s traces; CAISO's rule only binds for clusters `> 235 MW`).
  - All other 10 profiles (`6 dev`, `4 held_out`) remain strictly binding (`Noop FAIL`, `B1 FAIL`) across the combined fast + slow suite with zero globally unevaluated rules.

---

## F10. Prepending Facility Idle at `t = 0` Eliminates Phase 3's `0.0%` Throughput-Loss Shortcut

- **Category:** Initial condition validity (`workload/synth.py`, `sim/plant.py`, `rules/checker.py`, `evolve/loop.py`)
- **Example:** In Phase 3, GridSafe controllers initialized `p_target ≈ 72 MW` at `t = 0` (and B1 fixed `90 MW`), creating an unpenalized `+57 MW` instantaneous jump from `15 MW` idle at `t = 0` that produced an artificial `0.0%` throughput loss across all 11 profiles.
- **What changed:** Every trace (fast, slow, and random suite) now starts at facility idle (`idle_mw = idle_fraction * rated_mw`), and `check_compliance()` prepends steady `idle_mw` for all `t < 0` so any jump at `t = 0` counts against `ramp_rate`, `max_step`, and `dynamic_range`. Removed the `0.72 * rated_mw` initialization hint from `evolve/loop.py`.
- **Before vs After (honest degradation across 3 seeds x 11 profiles):**
  - **GridSafe mean throughput loss (got worse, now physically honest):** **`0.0%`** (Phase 3 t=0 shortcut) -> **`9.7% – 17.5%`** across the 9 binding ramp/step profiles (`0.0%` on non-binding CAISO; `5.2%` on frequency-only LIPA).
  - **Fast vs Slow split (3-seed passing means):** On **30-minute slow runs**, GridSafe achieves **`0.2% – 1.6%`** throughput loss by charging the battery smoothly during the idle prefix to pre-load the grid before job start. On **20-second fast runs** (only `0.5 s` of idle before a `100 MW` demand step under `10–20 MW/min` or `10 MW / 5 s` limits), startup curtailment is physically unavoidable (`16.7% – 29.5%` fast throughput loss).
  - **Relative standing vs baselines:** Even with honest idle starts, GridSafe achieves lower throughput loss and lower energy overhead than both B0 (full PDF) and B3 (matched-budget random search) on every binding ramp profile (e.g., ERCOT: GridSafe `13.8% TP / 3.5% OH` vs B0 `18.2% TP / 4.3% OH` vs B3 `18.7% TP / 4.7% OH`; IESO held-out: GridSafe `13.1% TP / 2.9% OH` vs B0 `18.7% TP / 4.2% OH` vs B3 `18.7% TP / 4.7% OH`; MISO held-out: GridSafe `9.7% TP / 2.3% OH` vs B0 `14.2% TP / 3.6% OH` vs B3 `16.9% TP / 3.9% OH`).

---

## F11. Multi-Seed Variance, `lipa` Fallback & Unseen Random-Suite (`N=20`) Generalization Gaps

- **Category:** Reliability & out-of-distribution generalization (`evolve/baselines.py`, `evolve/loop.py`, `eval/random_suite.py`)
- **Example:** Running 3 seeds (`101, 202, 303`) per profile across all 11 profiles and testing winning controllers on `N = 20` unseen randomly sampled workloads (`eval/random_suite.py`, `seed=20261121`) surfaced real variance and generalization gaps that single-seed preset runs hid:
  - **B0 (true end-to-end full PDF, no Reader page slicing):** Passed **32 / 33 (97.0%)** preset runs, failing `atc` seed `101` (`ramp_rate_8 = 532.6 MW/s > 0.5 MW/s`, `max_step_18 = 44.4 MW > 25 MW`), and failed **1 / 10** unseen slow workloads on held-out `miso_held_out` (`ramp_rate_0` margin `-15.4%`; `10 / 11` profiles passed all unseen workloads). On `ieso_held_out`, B0 rose from `0/4` to `3/3` after Step 8 replaced IESO's unbanded `0–250 Hz` check with the explicit `5–55 Hz` sub-synchronous assumption, though with `18.7%` TP loss vs GridSafe's `13.1%`.
  - **B3 (LPF + battery random search, matched simulator evals, no LLM):** Passed **33 / 33 (100%)** preset runs in `~3.1 s`, proving a strong non-LLM baseline on simple ramp rules, but suffered higher throughput loss (`+0.5% to +7.2%`) and higher overhead (`+0.3% to +1.9%`) than GridSafe on every binding ramp profile AND failed **5 / 20** unseen workloads on `lipa` (`15/20`, `fluctuation_band_25` margin `-31.6%`).
  - **GridSafe (Hydrogen evolve, 3 seeds):** Passed **33 / 33 (100%)** preset runs, **but** on `lipa` seed `101` evolution fell back to `fallback_conservative_floor` (`41.4%` OH, `0.0%` TP, raising `lipa` 3-seed mean OH to **`15.3%`** vs `2.3%` on seeds `202`/`303`), AND GridSafe's best evolved `lipa` controller failed **1 / 20** unseen workloads (`Rand20 = 19/20`, `fluctuation_band_25` `0.1–5 Hz` violated by `-4.85%` margin). This makes `lipa` a prime target for Phase 4 `gridsafe-hardener` adversarial testing.

---

## F12. Entity-Scoped Conflicts, Cross-Page Duplicates, Window Ranges & IESO Sub-synchronous Band

- **Category:** Extraction normalization & assumption transparency (`rules/schema.py`, `rules/units.py`, `reader/extract.py`, `rules/profiles.py`, `scripts/label.py`)
- **Example:** (1) Multi-utility PDFs (CAISO p. 14, MISO p. 6) falsely flagged ERCOT vs ATC vs AESO as conflicts and could leak `[CONFLICT]` notes into human gold files; (2) LIPA limits restated on CAISO pp. 23 & 25 were not deduped across pages; (3) Southern Company's `"4–6 second interval"` (CAISO p. 22) dropped `window_s=None`; (4) IESO's `"sub-synchronous frequency band"` (p. 8) had `freq_min_hz=freq_max_hz=None`.
- **What changed:** Added explicit `entity` field to `Constraint` and Reader schema, restricted `detect_conflicts()` to within-entity comparisons, added cross-page deduplication for matching `(entity, type, value, unit, window, band)`, parsed window ranges conservatively to the upper bound (`4–6 s -> 6.0 s` + assumption), set `DEFAULT_SUBSYNCHRONOUS_BAND_HZ = (5.0, 55.0)` for unspecified sub-synchronous rules (recorded on constraint + certificate), and added `--checkable-only` + `[CONFLICT]` stripping to `scripts/label.py`.
- **Human gold evaluation (all 6 PDFs now labeled in `eval/gold/`):** Against the pre-Phase-3b cached extractions, `Recall = 1.000`, `Precision = 0.679`, `Hallucination = 0.321`, `Abstention = 0.034` (`2601.12686v1`, `ESIG`, and `PSEG-LIPA` all `P=1.0, R=1.0`; `8` of `9` FPs come from MISO p. 6 comparative-utility entries and pre-dedup CAISO p. 25 LIPA duplicates, validating the Step 7 entity & cross-page dedup fixes).

---

## F13. Energy-Ledger Gate Replaced With Relative Residual (`< 1e-12`) & Accumulated Offset Precision Fix

- **Category:** Physics verification / floating-point accuracy (`sim/plant.py`, `eval/run_eval.py`, `tests/test_phase1_physics.py`)
- **Example:** In Phase 3b, lengthening fast traces to `20 s` (`10,000` steps at `dt = 0.002 s`) and adding `30 min` slow traces (`3,600` steps) caused the absolute energy residual `|E_grid - (E_final - E_init) - (E_gpu + E_batt_loss)|` to reach `1.181e-12 MWh` (`2.093e-12` relative on `abrupt_checkpoint_drop`), prompting a temporary loosening of the absolute gate from `1e-12 MWh` to `1e-9 MWh`. Root cause: adding tiny per-step battery increments (`~5e-6 MWh`) directly onto the `10.0 MWh` initial stored-energy baseline `10,000` times accumulated `~1e-12 MWh` of IEEE-754 float64 base-offset rounding, while an absolute `1e-9 MWh` threshold scales poorly across short (`0.1 s`) vs long (`30 min`) traces.
- **What changed:**
  - Replaced the absolute `1e-9 MWh` gate in `eval/run_eval.py`, `sim/plant.py`, `scripts/generate_validation_doc.py`, and `tests/test_phase1_physics.py` with a scale-invariant **relative gate**: `energy_balance_rel_error = energy_balance_error_mwh / total_energy_throughput_mwh < 1e-12` (`ENERGY_LEDGER_REL_TOL = 1e-12`), where `total_energy_throughput_mwh = max(E_gpu + E_batt_throughput + E_batt_loss, |E_grid|, 1e-12)`.
  - Accumulated stored-energy delta `e_delta` directly in `sim/plant.py` (`e_curr = e_init + e_delta`) to eliminate the `10 MWh` float64 offset cancellation while keeping the independent array-level `soc[-1] * capacity_mwh` check and permanent non-vacuity mutation test (`test_energy_ledger_relative_gate_is_not_vacuous_under_mutation`).
- **Before vs After (all 7 fast + slow presets, active-battery probe + live mutation test):**
  - Gate: loosened absolute `< 1e-9 MWh` -> strict relative `< 1e-12` (`ENERGY_LEDGER_REL_TOL`).
  - Max measured residual across fast (`20 s`) + slow (`30 min`) presets: **`1.181e-12 MWh` abs (`2.093e-12` rel)** -> **`3.553e-14 MWh` abs (`6.727e-15` rel)** (`~30x` float64 machine epsilon).
  - Live mutation check (dropping `/ eta_d` in `sim/plant.py` discharge step): immediately failed `test_energy_conservation_lossless_and_lossy` with relative residual **`5.266e-3`** (`> 5e9` times the `1e-12` gate), proving non-vacuity.

---

## F14. Managed Hardener Full Red-Team Turns Hit Client Read Timeouts

- **Category:** Managed agent latency / Interactions API (`agents/bridge.py`, Phase 4)
- **Example:** Asking `gridsafe-hardener` (`gemini-4-carbon` Antigravity) to fully attack a LIPA controller with code_execution + full controller source timed out at the client (`APITimeoutError` / `httpx.ReadTimeout`) after ~180 s. A stock ping / ACK-only hardener turn completes in **`~10.1 s`**. The full `message_agent` engineer→hardener→engineer bridge (ACK-only) completed in **`26.7 s`** (engineer `requires_action` 10.1 s + hardener 12.5 s + continue 4.1 s).
- **What changed:** Truncate controller source on the wire, instruct hardener to propose short workload JSON instead of long sims, catch timeouts and fall back to host Rand20 verification, and keep `enable_agents` as a feature flag. LIPA F11 demo packages the host-verified Rand20 break through the hardener Interactions API, then engineer-fixes via B3-seeded evolve with backend re-check.
- **Before vs After:**
  - Full red-team hardener turn: **timeout / no reply** -> ACK / structured package turns **`10–24 s`**.
  - LIPA F11 (`eval/agent_threads/lipa_f11_20261121T182024Z_summary.json`): hardener break package **`24.0 s`**, engineer fix `fallback_b3` **`25.3 s`**, backend confirms presets **PASS**, Rand20 **PASS**, counterexample no longer breaks; original controller still breaks (non-vacuous).

---

## F15. Frequency-Band Rules Collapsed Distinct Aggregations Into One Peak Bin

- **Category:** Checker / document fidelity (`rules/schema.py`, `rules/checker.py`, `workload/synth.py`)
- **Example:** LIPA Table 4-2 (native PDF pp. 9–12) states three different metrics in the same bands:
  - `0.1–5 Hz`: **sum of any two adjacent bins** ≤ 10 MW (60 s rolling avg, 10 s FFT)
  - `0.1–5 Hz`: **sum of all bins** ≤ 20 MW (same windows)
  - `5–55 Hz`: **sum of any two adjacent bins** ≤ 3.5 MW (10 s rolling avg, 1 s FFT)
  Before this fix, every `fluctuation_band` used the largest single FFT bin over the whole trace, so the 10 MW and 20 MW LIPA limits received the **same measured number**. The live CAISO-straw LIPA profile also lacked windows, so the 0.1–5 Hz rule was judged on the **fast** family only (`20 s @ 0.002 s`, Nyquist 250 Hz, Δf=0.05 Hz) — that trace *can* represent 1–5 Hz content, but it cannot satisfy a **60 s** averaging window (`2×60 s = 120 s` required).
- **What changed:** Added `aggregation` (`peak_bin` \| `adjacent_pair_sum` \| `band_sum`), `fft_window_s`, `averaging_window_s` on `Constraint` (Reader fields + snippet inference; default `peak_bin` recorded as an assumption). Checker implements all three aggregations with sliding FFT windows and rolling averages, and marks traces `< 2×` the band window `not_evaluable`. Added `band_long` family (`120 s @ 0.01 s`) for long-window low-frequency bands.
- **Before vs After (gold LIPA wording, steady 50 MW probe):**
  - Fast `20 s`: `0.1–5 Hz` adjacent/sum rules **evaluated as peak_bin (wrong)** → **`not_evaluable`** (window gate).
  - `band_long` `120 s`: same rules **evaluable**; aggregations distinguish adjacent-pair vs band-sum on hand-computed two-tone tests.
  - Native LIPA re-read → profile **`lipa_pseg`** (4 checkable rules with correct aggregations). Preset rerun: Noop/B1 **FAIL**; B3 seed101 **PASS** OH **3.38%**; GridSafe seeds **101/202/303** all **`evolved_compliant`** (OH **3.48% / 3.50% / 3.81%**) — seed 101 no longer hits the **41.4%** conservative floor (old CAISO-straw `lipa` profile).




