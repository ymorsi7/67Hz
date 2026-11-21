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
