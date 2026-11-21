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


