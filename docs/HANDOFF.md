# GridSafe Agent Handoff

Fresh-agent briefing for `/Users/yusufmorsi/Documents/GitHub/67Hz` (public repo `ymorsi7/67Hz`, MIT). Read `SPEC.md`, `docs/API_NOTES.md`, and `FAILURES.md` alongside this file before starting Phase 4.

## 1. Hard Rules & User Workflow (Non-Negotiable)
- **User makes ALL commits and pushes (`AGENTS.md`):** NEVER run `git commit` or `git push`. End every completed phase with a one-line commit message for the user and wait for `"next phase"`.
- **Never invent results or gold labels:** Every metric, table, and claim must come from an executed run in this repo. Never write `eval/gold/*.json` entries yourself (user creates them via `scripts/label.py --checkable-only` with `"labeled_by": "human"`).
- **Never leak secrets into sandboxes:** Keep `.env` (`GEMINI_API_KEY`) strictly on the host backend. Never pass API keys into `evolve/_runner.py` or Antigravity remote sandboxes.
- **Sandbox package boundary:** `workload/`, `sim/`, `rules/`, `controllers/`, and `evolve/` must remain pure Python + `numpy` only (no `scipy`, `pymupdf`, `fitz`, or `pydantic`; enforced by AST unit tests).

## 2. Phase Status
- **Step 0 (Done):** Live API probes (`docs/API_NOTES.md`), FastAPI+SSE backend (`server/app.py`), Vite 8 + React 19 + TS 7 + Tailwind v4 + shadcn/ui frontend (`frontend/`), `config/models.yaml`, `Makefile`, `scripts/demo_check.py`.
- **Phase 1 (Done + checked):** `workload/synth.py`, IEEE 1977 FBM 6-mass shaft (`sim/shaft.py`, max mode error `0.0143% <= 2%`, all 5 strain-energy dominant sections distinct: `LPA-LPB`, `GEN-EXC`, `IP-LPA`, `LPB-GEN`, `HP-IP`), energy-conserving plant (`sim/plant.py`, max residual `1.181e-12 MWh` across fast 20 s & slow 30 min runs, mean eval `18.6 ms`, max `36.8 ms < 500 ms`), pure-Python checker (`rules/`), `docs/VALIDATION.md`.
- **Phase 2 (Done):** `reader/extract.py` (`gemini-4-neon`, native PDF slicing + Pydantic schema + `entity` + window ranges + cross-page dedup + entity-scoped conflicts), `reader/verify.py` (`gemini-decision-1`, 1 batched call per cited page + parallel `page_readable` gate, 0 output tokens), `eval/degrade.py`, `eval/extraction.py`, `eval/gold/` (**all 6 PDFs now human-labeled**; `R=1.000`, `P=0.679` on pre-Phase-3b caches), `scripts/label.py` (with `--checkable-only` and `[CONFLICT]` stripping), `scripts/fetch_specs.py`, `FAILURES.md`.
- **Phase 3 + Phase 3b (Done — Evaluation Validity Fixes Complete):**
  - **Window validity & dual scenario families:** Traces `< 2x` rule window (or bands above Nyquist / `< 2` cycles) return `status="not_evaluable"` (`passed=None`) — never pass or fail. Unwindowed per-minute ramp rates assume a `60 s` rolling window (recorded on `Constraint.assumptions` & certificates). Added **slow scenario family** (`30 min = 1800 s @ 0.5 s`, 3 workloads: idle, job start, checkpoints, job stop) and lengthened **fast family** to `20 s @ 0.002 s` (`2x` largest `10 s` fast window). Eval speed: `mean 18.6 ms`, `max 36.8 ms` fast / `10.9 ms` slow (`<< 500 ms`).
  - **Honest idle initial condition:** Every trace starts at facility idle (`idle_mw`), prepended by `rules/checker.py` for `t < 0` so `t = 0` jumps count against ramp/step/range rules. Removed the `0.72 * rated_mw` cheat from `evolve/loop.py`.
  - **Fair baselines & 3-seed reliability (`seeds=[101, 202, 303]`):**
    - `B0`: true end-to-end **full raw PDF in -> controller out** (`gemini-4-neon`, no page hints, no verified constraints, no checker): **32 / 33 (97.0%)** preset runs (`atc` seed 101 failed), **10 / 11** on unseen `Rand20` (`miso_held_out` failed `9/10`).
    - `B3`: non-LLM LPF + battery policy tuned by seeded random search with the **exact same simulator evaluation budget** as GridSafe: **33 / 33 (100%)** presets (`~3.1 s`), **10 / 11** on unseen `Rand20` (`lipa` failed `15/20`), consistently higher TP loss & overhead than GridSafe on every binding ramp profile.
    - `GridSafe` (`gemini-4-hydrogen` evolve): **33 / 33 (100%)** preset runs (`27–54 s/run`), **10 / 11** on unseen `Rand20` (`lipa` `19/20`, `-4.85%` margin), lowest OH (`2.3–4.0%`) and TP loss (`9.7–17.5%` combined; `0.2–1.6%` on 30-min slow runs) on all 9 binding ramp/step profiles. (Note: `caiso` `20 MW/min` over `600 s` = `200 MW/10 min` is physically non-binding at `100 MW`, so Noop & B1 pass `caiso`; `lipa` seed 101 used `fallback_conservative_floor`.)
  - **Unseen random workload suite (`eval/random_suite.py`):** Samples `N=20` fast+slow workloads from documented ranges using `--seed` (`make eval-final SEED=<judge_seed>`).
  - **Entity, window-range, and sub-synchronous band assumptions:** Added `entity` field + within-entity conflict detection + cross-page dedup; `4–6 s` window range -> `6.0 s` with assumption; IESO sub-synchronous band -> `5.0–55.0 Hz` with assumption shown on certificate.
  - **`57 passed`** in `make test`; `make eval` and `make eval-final SEED=20261121` pass cleanly.
- **Phase 4 (Next — SPEC.md already updated, NOT built yet):** Managed agents `gridsafe-engineer` (`gemini-4-hydrogen`, skills `read-rulebook`, `evolve-controller`, `certify`) and `gridsafe-hardener` (`gemini-4-carbon`, skills `red-team`, `analyze-failure`) communicating via custom-function `message_agent` bridge + logged JSON threads in `eval/agent_threads/`, background hardening run (`eval/attacks/`), and Baseline B2.
- **Phases 5–7 (Not started):** Phase 5 UI flow + canvas shaft/spectrum + thread viewer; Phase 6 "Break the grid" judge sliders (`eval/live/`); Phase 7 `make eval-final`, `RESULTS.md`, `docs/DEMO.md`, `README.md` polish.

## 3. Repo Map
- `AGENTS.md` — Always-applied rule forbidding agent commits/pushes.
- `SPEC.md` — Full hackathon build spec (Phase 4 already updated for multi-agent `message_agent` protocol).
- `FAILURES.md` — Real failure log F1–F12 with before/after measurements (including Phase 3b regressions/honest findings).
- `Makefile` — Targets: `test`, `eval`, `eval-final SEED=<seed>`, `run`, `replay`, `demo-check`, `build-ui`, `fetch-specs`.
- `config/` — `models.yaml` (model IDs + calibrated verifier thresholds) and `fbm.yaml` (user-supplied IEEE FBM parameters).
- `docs/` — `API_NOTES.md`, `VALIDATION.md`, `HANDOFF.md`.
- `workload/synth.py` — Deterministic cluster trace generator + fast presets (`20 s @ 0.002 s`) + slow presets (`30 min @ 0.5 s`), all starting at idle.
- `sim/` — `shaft.py` (6-mass IEEE FBM FFT modal solver) and `plant.py` (time-stepped GPU + battery + energy ledger + `idle_mw` initial condition).
- `rules/` — `schema.py` (`Constraint` with `entity` & `assumptions`, `ComplianceReport` with `not_evaluable`), `units.py` (`is_per_minute_ramp_unit`, `parse_window_range_s`), `checker.py` (`2x` window gate, idle prepending, Nyquist checks, `5–55 Hz` sub-sync band), `profiles.py` (11 profiles: 7 `dev` + 4 `held_out`).
- `reader/` — `pdf_utils.py` (`pymupdf`, backend-only), `extract.py` (`gemini-4-neon`, explicit `entity`, entity-scoped conflicts, cross-page dedup), `verify.py` (`gemini-decision-1` batched per page).
- `controllers/seed.py` — 38-line pure-numpy EMA seed controller (starts at idle).
- `evolve/` — `sandbox.py` (AST validator + subprocess runner + dual-family aggregation), `_runner.py`, `loop.py` (seeded parallel Hydrogen evolution, idle-start aware), `baselines.py` (Noop, B1, full-PDF B0, matched-budget random-search B3).
- `eval/` — `run_eval.py`, `random_suite.py` (seeded `N=20` unseen workloads), `degrade.py`, `extraction.py`, `gold/` (all 6 human-labeled JSONs + `TEMPLATE.json`), `cache/` (`phase3_results.json` schema `phase3b`, `extractions/`, `dev_calibration.json`).
- `scripts/` — `fetch_specs.py`, `run_specs_reader.py`, `calibrate_dev_verifier.py`, `label.py` (`--checkable-only`), `run_phase3_baselines.py`, `generate_validation_doc.py`, `demo_check.py`.
- `tests/` — `test_scaffold.py`, `test_phase1_physics.py`, `test_phase1_rules.py`, `test_phase2_reader_verifier.py`, `test_phase3_evolve.py` (57 tests total).

## 4. Common Commands
- **Unit tests (57 tests, ~33 s):** `make test`
- **Dev eval (Phase 1 fast+slow gates + 6 gold docs + dev Phase 3b table):** `make eval`
- **Final eval with judging-time random seed:** `make eval-final SEED=20261121`
- **Sample N unseen workloads:** `.venv/bin/python -m eval.random_suite --seed <SEED> --n 20`
- **Fetch missing PDFs from `specs/SOURCES.md`:** `make fetch-specs`
- **Run Phase 3b multi-seed benchmark:** `.venv/bin/python scripts/run_phase3_baselines.py --seeds 101 202 303`
- **Human gold labeling CLI (user-operated only):** `.venv/bin/python scripts/label.py --checkable-only`
- **Build UI / run server:** `make build-ui`, `make run`, `make replay`, `make demo-check`

## 5. Decisions, Thresholds & Gotchas
- **Calibrated Verifier thresholds (`config/models.yaml`):** `confidence_threshold: 0.85`, `readability_threshold: 0.60` (selected strictly on dev split; do NOT re-tune on `held_out`).
- **Documented checker assumptions (always shown on certificates):**
  - Per-minute ramp limit with no stated window -> `60 s` rolling window.
  - Step/ramp window range (e.g. Southern `"4–6 second interval"`) -> upper bound (`6.0 s`) + logged range.
  - IESO sub-synchronous oscillation without Hz bounds -> `5.0–55.0 Hz` (60 Hz SSTI/SSCI band covering IEEE FBM modes `15.7–47.5 Hz`).
- **Cached live runs:** `eval/cache/phase3_results.json` (`schema_version: "phase3b"`) holds all 11 profiles x 3 seeds x (Noop, B1, B0 full-PDF, B3 random search, GridSafe) + unseen `Rand20` results.
- **No native subagent nesting:** Implement Phase 4 agent-to-agent dialogue via custom function `message_agent` bridging `gridsafe-engineer` <-> `gridsafe-hardener` via `client.interactions.create` with persistent `previous_interaction_id` + `environment` per agent.

## 6. Known Issues & Prime Phase 4 Hardening Targets
- **Phase 4 ready to build:** Create `.agents/AGENTS.md`, `.agents/skills/{read-rulebook,evolve-controller,certify,red-team,analyze-failure}/SKILL.md`, `.agents/hooks.json`, managed agent provisioning/orchestrator (`gridsafe-engineer` + `gridsafe-hardener` + `message_agent` bridge + independent backend checker verification), `eval/agent_threads/`, `eval/attacks/`, background hardening run, and Baseline B2.
- **Prime Phase 4 hardener targets:**
  1. `lipa`: GridSafe's best evolved controller failed `1/20` unseen random workloads (`fluctuation_band_25` `0.1–5 Hz`, `-4.85%` margin), and seed `101` fell back to conservative floor (`41.4%` OH).
  2. `southern_dev`: still leaves higher shaft torque on resonant excitation because its rulebook has no explicit Hz band — ideal red-team target for `gridsafe-hardener`.
- **CAISO at 100 MW:** `20 MW/min` over `600 s` (`200 MW/10 min`) is physically non-binding at `100 MW` (only binds `> 235 MW`). Keep disclosed honestly in `RESULTS.md` and UI.
- **Gitignored PDFs:** `specs/*.pdf` are gitignored (all 6 are currently fetched locally via `make fetch-specs`). Note arXiv filename is `specs/2601.12686.pdf` (handled automatically by `evolve.baselines.resolve_pdf_path`).
