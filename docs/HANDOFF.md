# GridSafe Agent Handoff

Fresh-agent briefing for `/Users/yusufmorsi/Documents/GitHub/67Hz` (public repo `ymorsi7/67Hz`, MIT). Read `SPEC.md`, `docs/API_NOTES.md`, and `FAILURES.md` alongside this file.

## 1. Hard Rules & User Workflow (Non-Negotiable)
- **User makes ALL commits and pushes (`AGENTS.md`):** NEVER run `git commit` or `git push`. End every completed phase with a commit message for the user.
- **Never invent results or gold labels.** Never write `eval/gold/*.json` as an agent.
- **Never leak secrets into sandboxes.** No `GEMINI_API_KEY` in evolve runners or Antigravity sandboxes.
- **Sandbox package boundary:** `workload/`, `sim/`, `rules/`, `controllers/`, `evolve/` = pure Python + numpy only.
- **Held-out profiles** run only in `make eval-final`.
- **Do not edit** `frontend/`, `server/ui_api.py`, or `server/ui_dev.py` while the parallel UI agent owns them (unless that ownership ends).

## 2. Phase Status
- **Phases 0–4 + 4b:** Done (band aggregations, LIPA `lipa_pseg`, agent API, UI mount).
- **Phase 7 wrap-up (this turn):** Final evidence + demo safety — **done**.
  - `make eval-final SEED=20261121`: **42.32 s** (`eval/results/eval_final_timing.json`).
  - B2 stock Antigravity: **11/11 profiles in 296.6 s** (8‑min budget); host-scored **0 passes** (`eval/results/b2_budget_20261121.json`).
  - Extraction re-score vs 6 gold: P=0.679 R=1.000 H=0.321 A=0.034.
  - `make eval-live`, `make report` → `RESULTS.md`, `docs/NUMBERS.md`, README rebuilt.
  - Hardening: **timeouts yes** (14 attack files with `APITimeoutError`); pause/resume added (`make harden-pause` / `make harden-resume`, flag `eval/attacks/HARDEN_PAUSE`, state `eval/attacks/hardening_state.json`).
  - `make demo` (no reload), `make demo-check` **passed**; clean LIPA replay at `eval/tmp/replays/latest.json`.
- **Still UI-agent side:** Phase 5–6 polish / Break-the-grid UX if not finished.

### Headline unflattering facts (quote from RESULTS.md)
- GridSafe presets **33/33** — **same as no-AI B3 33/33**.
- B2 run1 **0/11** was mostly harness (thin prompt ≠ B0); fair run2 **6/10** (all remaining fails = rule breaks; `lipa` skipped for budget).
- Hardener holdout SEED=4242: `lipa` **19→13/20** (hurt); `lipa_pseg` **18→20/20** (helped).
- Hardening: **19** rounds, **20** timeouts, **2** breaks, **2** fixes — most turns timed out.

## 3. Repo Map (Phase 7 wrap-up additions)
- `Makefile` — `eval-live`, `report`, `demo`, `b2-budget`, `harden-pause`, `harden-resume`
- `scripts/build_final_results.py` — writes `eval/results/final_<seed>.json`
- `scripts/report.py` — writes `RESULTS.md` from saved JSON only
- `scripts/eval_live.py` — judge-picked seed, <60 s pass table
- `scripts/run_b2_budget.py` — B2 within wall-clock budget
- `agents/harden.py` — pause flag + resume state
- `docs/NUMBERS.md` — 12 quoteable numbers with provenance
- `eval/results/` — final / B2 / live / timing artifacts

## 4. Agent API for the UI (`server/agent_api.py`, mounted from `server/app.py`)

Unchanged from Phase 4b. `make run` / `make demo` serve UI + agents on one port. Never invents agent messages.

### Key endpoints
- `POST /api/agents/harden` (SSE) — engineer↔hardener bridge
- `GET /api/agents/tally` — hardening counters
- `GET /api/agents/threads` / `.../{id}` — logged threads

## 5. Common Commands
- `make test` / `make eval` / `make eval-final SEED=20261121`
- `make eval-live SEED=<n>` — fresh 20-workload table from cached controllers
- `make report` — regenerate `RESULTS.md`
- `make demo` — build UI, serve port 8000 **without** reload
- `make demo-check` — key, models, Decisions API, both agents, replay, UI build
- `make harden` / `make harden-pause` / `make harden-resume`
- `make b2-budget` — stock Antigravity baseline under 8 minutes

## 6. Gotchas
- Prefer native profile id **`lipa_pseg`** for Table 4-2 aggregations; **`lipa`** is still the CAISO-straw restatement (peak_bin).
- `band_long` Nyquist is 50 Hz — enough for 0.1–5 Hz; 5–55 Hz is only partially covered there (fast family still carries high band).
- Hardener turns often hit **90 s `APITimeoutError`**; host falls back to Rand20; pause with `HARDEN_PAUSE` rather than hammering the API.
- `make eval-final` now also runs `build_final_results.py` (random-suite re-score ~40 s extra).
- Do not touch `frontend/` / `server/ui_api.py` / `server/ui_dev.py` if the UI agent is still active.
