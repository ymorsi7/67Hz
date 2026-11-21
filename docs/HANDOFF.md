# GridSafe Agent Handoff

Fresh-agent briefing for `/Users/yusufmorsi/Documents/GitHub/67Hz` (public repo `ymorsi7/67Hz`, MIT). Read `SPEC.md`, `docs/API_NOTES.md`, and `FAILURES.md` alongside this file before starting Phase 4.

## 1. Hard Rules & User Workflow (Non-Negotiable)
- **User makes ALL commits and pushes (`AGENTS.md`):** NEVER run `git commit` or `git push`. End every completed phase with a one-line commit message for the user and wait for `"next phase"`.
- **Never invent results or gold labels:** Every metric, table, and claim must come from an executed run in this repo. Never write `eval/gold/*.json` entries yourself (user creates them via `scripts/label.py` with `"labeled_by": "human"`).
- **Never leak secrets into sandboxes:** Keep `.env` (`GEMINI_API_KEY`) strictly on the host backend. Never pass API keys into `evolve/_runner.py` or Antigravity remote sandboxes.
- **Sandbox package boundary:** `workload/`, `sim/`, `rules/`, `controllers/`, and `evolve/` must remain pure Python + `numpy` only (no `scipy`, `pymupdf`, `fitz`, or `pydantic`; enforced by AST unit tests).

## 2. Phase Status
- **Step 0 (Done):** Live API probes (`docs/API_NOTES.md`), FastAPI+SSE backend (`server/app.py`), Vite 8 + React 19 + TS 7 + Tailwind v4 + shadcn/ui frontend (`frontend/`), `config/models.yaml`, `Makefile`, `scripts/demo_check.py`.
- **Phase 1 (Done + checked):** `workload/synth.py`, IEEE 1977 FBM 6-mass shaft (`sim/shaft.py`, max mode error `0.0143% <= 2%`, all 5 strain-energy dominant sections distinct: `LPA-LPB`, `GEN-EXC`, `IP-LPA`, `LPB-GEN`, `HP-IP`), energy-conserving plant (`sim/plant.py`, non-vacuous residual `7.985e-13 MWh`, mean eval `~14 ms < 500 ms`), pure-Python checker (`rules/`), `docs/VALIDATION.md`.
- **Phase 2 (Done):** `reader/extract.py` (`gemini-4-neon`, native PDF slicing + Pydantic schema), `reader/verify.py` (`gemini-decision-1`, 1 batched call per cited page + parallel `page_readable` gate, 0 output tokens), `eval/degrade.py`, `eval/extraction.py`, `eval/gold/TEMPLATE.json`, `scripts/label.py`, `scripts/fetch_specs.py`, `FAILURES.md`. All 6 PDFs in `specs/` processed & cached (`dev`: CAISO, ESIG, PSEG-LIPA; `held_out`: arXiv 2601.12686, IESO, MISO LLWG).
- **Phase 3 (Done):** `controllers/seed.py` (38 lines), `evolve/sandbox.py` + `evolve/_runner.py` (AST bans + isolated subprocess + timeout), `evolve/loop.py` (6 parallel `gemini-4-hydrogen` workers/gen, 75 s hard budget, guaranteed fallback/refusal), `evolve/baselines.py` (Noop, B1 90% floor, B0 single-shot Neon), `rules/profiles.py` (7 `dev` + 4 `held_out` utility profiles). Live benchmark cached in `eval/cache/phase3_results.json`: GridSafe **11/11 PASS** (2.5–3.7% OH, 0.0% TP loss, 26–55 s), beating B1 (0/11 PASS, 29.6% OH) and B0 (10/11 PASS, 15.9–21.9% TP loss; **B0 failed 0/4 on held-out `ieso_held_out`**). `51 passed` in `make test`.
- **Phase 4 (Next — SPEC.md already updated, NOT built yet):** Managed agents `gridsafe-engineer` (`gemini-4-hydrogen`, skills `read-rulebook`, `evolve-controller`, `certify`) and `gridsafe-hardener` (`gemini-4-carbon`, skills `red-team`, `analyze-failure`) communicating via custom-function `message_agent` bridge + logged JSON threads in `eval/agent_threads/`, background hardening run (`eval/attacks/`), and Baseline B2.
- **Phases 5–7 (Not started):** Phase 5 UI flow + canvas shaft/spectrum + thread viewer; Phase 6 "Break the grid" judge sliders (`eval/live/`); Phase 7 `make eval-final`, `RESULTS.md`, `docs/DEMO.md`, `README.md` polish.

## 3. Repo Map
- `AGENTS.md` — Always-applied rule forbidding agent commits/pushes.
- `SPEC.md` — Full hackathon build spec (Phase 4 already updated for multi-agent `message_agent` protocol).
- `FAILURES.md` — Real failure log F1–F8 with before/after measurements from live runs.
- `Makefile` — Targets: `test`, `eval`, `eval-final`, `run`, `replay`, `demo-check`, `build-ui`, `fetch-specs`.
- `config/` — `models.yaml` (model IDs + calibrated verifier thresholds) and `fbm.yaml` (user-supplied IEEE FBM parameters).
- `docs/` — `API_NOTES.md` (verified Gemini 4 / Decisions / Managed Agents APIs), `VALIDATION.md` (Phase 1 physics report), `HANDOFF.md`.
- `workload/synth.py` — Deterministic cluster trace generator + 4 presets (`standard_training`, `ssr_mode1_attack`, `ssr_mode2_attack`, `abrupt_checkpoint_drop`).
- `sim/` — `shaft.py` (6-mass IEEE FBM FFT modal solver) and `plant.py` (time-stepped GPU + battery + energy ledger).
- `rules/` — `schema.py`, `units.py`, `checker.py` (deterministic O(N) compliance checker), `profiles.py` (per-utility `dev`/`held_out` profile loader).
- `reader/` — `pdf_utils.py` (`pymupdf`, backend-only), `extract.py` (`gemini-4-neon`), `verify.py` (`gemini-decision-1` batched per page).
- `controllers/seed.py` — 38-line pure-numpy EMA seed controller.
- `evolve/` — `sandbox.py` (AST validator + subprocess runner), `_runner.py` (restricted-builtins child worker), `loop.py` (parallel Hydrogen evolution), `baselines.py` (Noop, B1, B0).
- `server/app.py` — FastAPI backend (`/api/health`, `/api/stream/ping`, serves `frontend/dist/`).
- `frontend/` — Vite 8 + React 19 + TS 7 + Tailwind v4 + shadcn/ui scaffold (`public/favicon.png`, `public/thumbnail.png`).
- `specs/` — `SOURCES.md` (source URLs + `[dev]`/`[held_out]` tags); `specs/*.pdf` are gitignored (6 PDFs currently local).
- `eval/` — `run_eval.py`, `degrade.py`, `extraction.py`, `gold/` (2 human-labeled JSONs + `TEMPLATE.json`), `cache/` (live extraction/calibration/Phase 3 JSON caches).
- `scripts/` — `fetch_specs.py`, `run_specs_reader.py`, `calibrate_dev_verifier.py`, `label.py`, `run_phase3_baselines.py`, `generate_validation_doc.py`, `demo_check.py`.
- `tests/` — `test_scaffold.py`, `test_phase1_physics.py`, `test_phase1_rules.py`, `test_phase2_reader_verifier.py`, `test_phase3_evolve.py` (51 tests total).

## 4. Common Commands
- **Unit tests (51 tests, ~30 s):** `make test` (runs `.venv/bin/pytest -q`)
- **Dev eval (Phase 1 gates + gold extraction + dev Phase 3 table):** `make eval`
- **Final eval (includes `held_out` profiles):** `make eval-final`
- **Fetch missing PDFs from `specs/SOURCES.md`:** `make fetch-specs`
- **Run Reader + Verifier on newly added `specs/*.pdf` only:** `.venv/bin/python scripts/run_specs_reader.py --new-only`
- **Run Phase 3 baselines + Hydrogen Designer (new profiles only / all):** `.venv/bin/python scripts/run_phase3_baselines.py --new-only` (omit `--new-only` to rerun all 11)
- **Human gold labeling CLI (user-operated only):** `.venv/bin/python scripts/label.py`
- **Build UI / run server:** `make build-ui`, `make run`, `make replay`, `make demo-check`

## 5. Decisions, Thresholds & Gotchas (Not in SPEC.md / API_NOTES.md)
- **Calibrated Verifier thresholds (`config/models.yaml`):** `confidence_threshold: 0.85`, `readability_threshold: 0.60`, selected strictly from original `dev` split (`n=50`, CAISO + ESIG) + CAISO p.13 negative/degradation sweeps cached in `eval/cache/dev_calibration.json`. Do NOT re-tune on `held_out`.
- **Designer thinking level (`config/models.yaml`):** Set `designer.thinking_level: low` (not `medium`). With `medium`, 2 generations took `84.8 s` (> 75 s budget); with `low` + `as_completed(timeout=remaining_budget)` in `evolve/loop.py`, all 11 profiles finish in `26–55 s`.
- **Cached live runs:** Do not needlessly re-spend API quota/time re-running already cached PDFs or profiles. Live caches live at:
  - `eval/cache/extractions/<pdf_stem>.json` (all 6 PDFs, scored at `0.85` / `0.60`)
  - `eval/cache/dev_calibration.json` (CAISO p.1/p.13 controls & Sev 1–4 degradation sweep)
  - `eval/cache/phase3_results.json` (11 profiles x 4 workloads for Noop, B1, B0, GridSafe + winning code)
- **New `specs/` PDF handling:** `scripts/run_specs_reader.py` defaults any unlisted PDF to `held_out`. `rules/profiles.py` excludes dev-duplicate utilities from held-out comparative decks (keeping `2601.12686v1.pdf` Southern explicitly held out) so `held_out` stays leak-free (`ieso_held_out`, `miso_held_out`, `southern_held_out`, `spp_held_out`).
- **PyMuPDF import:** Package `pymupdf==2.1` is imported as `import pymupdf` (not `fitz`) inside `reader/` and `eval/degrade.py`. Pytest emits 5 harmless upstream SwigPy `DeprecationWarning`s from it.
- **No native subagent nesting:** Verified directly in `custom-agents.md` (*Limitations*: `"No subagent nesting: Subagent delegation is not yet supported."`). Implement Phase 4 agent-to-agent dialogue through the custom function `message_agent` bridging `gridsafe-engineer` <-> `gridsafe-hardener` via `client.interactions.create` with persistent `previous_interaction_id` + `environment` per agent.

## 6. Known Issues & Open Items
- **Phase 4 ready to build:** Create `.agents/AGENTS.md`, `.agents/skills/{read-rulebook,evolve-controller,certify,red-team,analyze-failure}/SKILL.md`, `.agents/hooks.json`, managed agent provisioning/orchestrator (`gridsafe-engineer` + `gridsafe-hardener` + `message_agent` bridge + independent backend checker verification on every controller and counterexample), `eval/agent_threads/`, `eval/attacks/`, background hardening run, and Baseline B2.
- **Disclosed `t=0` pre-conditioning trade-off (`FAILURES.md` F5):** GridSafe (`~65–72 MW` initial target) and B1 (`90 MW` floor) treat `t=0` as a pre-conditioned steady-state operating point (achieving `0.0%` throughput loss), whereas B0 anchored at `p_raw[0]=15 MW` idle and incurred `15.9–21.9%` curtailment. Keep this disclosed in `RESULTS.md` / UI.
- **Prime Phase 4 hardener target (`southern_dev`):** GridSafe's winning `southern_dev` controller passed all 4 workloads (`2.5%` OH) while leaving `0.166 pu` peak shaft torque on `ssr_mode1_attack` (because `southern_dev` only constrains 60 s ramp + 10 MW step, not 15.71 Hz FFT) — great target for `gridsafe-hardener` red-teaming.
- **Partial human gold coverage:** 2 of 6 PDFs have human gold labels in `eval/gold/` (`2601.12686v1.json`, `LL-Performance-Requirements-Framework-and-Methodology_v2.json`; `P=1.0, R=1.0, H=0.0`); the remaining 4 await user labeling via `scripts/label.py` if desired before Phase 7.
- **Gitignored PDFs:** `specs/*.pdf` are gitignored; a fresh clone or remote sandbox must run `make fetch-specs` (or use cached `eval/cache/extractions/*.json`) if raw PDFs are needed.
