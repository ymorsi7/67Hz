# GridSafe Agent Handoff

Fresh-agent briefing for `/Users/yusufmorsi/Documents/GitHub/67Hz` (public repo `ymorsi7/67Hz`, MIT). Read `SPEC.md`, `docs/API_NOTES.md`, and `FAILURES.md` alongside this file.

## 1. Hard Rules & User Workflow (Non-Negotiable)
- **User makes ALL commits and pushes (`AGENTS.md`):** NEVER run `git commit` or `git push`. End every completed phase with a commit message for the user.
- **Never invent results or gold labels.** Never write `eval/gold/*.json` as an agent.
- **Never leak secrets into sandboxes.** No `GEMINI_API_KEY` in evolve runners or Antigravity sandboxes.
- **Sandbox package boundary:** `workload/`, `sim/`, `rules/`, `controllers/`, `evolve/` = pure Python + numpy only.
- **Held-out profiles** run only in `make eval-final`.
- **Do not edit** `frontend/`, `server/ui_api.py`, or `server/ui_dev.py` while the parallel UI agent owns them (Phase 4b).

## 2. Phase Status
- **Phases 0–4 + Part A:** Done (committed earlier as Phase 4 + Part A).
- **Phase 4b (this turn):**
  - **Part 1:** `server/app.py` mounts `ui_router` from `server/ui_api.py` (single-port `make run`).
  - **Part 2:** `server/agent_api.py` mounted — see **Agent API for the UI** below.
  - **Part 3:** Frequency-band aggregations (`peak_bin` / `adjacent_pair_sum` / `band_sum`), `fft_window_s`, `averaging_window_s`; `band_long` family (120 s @ 0.01 s); Reader+Verifier re-run on native LIPA PDF; **F15**.
  - **Part 4:** README “How the managed agents are used”; LIPA seed 101 no longer lands on 41% floor (see below).
- **Phases 5–7:** UI polish / Break-the-grid / RESULTS.md still open on the UI agent’s side.

### LIPA band evaluation — before vs after (Part 3)
**Before (CAISO-straw `lipa` profile, peak_bin only):**
- Rules: `0.1–5 Hz < 10 MW` and `5–55 Hz < 3.5 MW`, `window_s=None`, aggregation=`peak_bin`.
- Judged on **fast** family: **20 s @ 0.002 s** (Nyquist 250 Hz, Δf=0.05 Hz). That *can* represent 1–5 Hz content, but every aggregation type received the same peak-bin number; a true 60 s averaging window was never enforced.

**After (native `lipa_pseg` from re-read PDF):**
- `ramp_rate` 20 MW/min / 60 s; `5–55 Hz` adjacent_pair_sum ≤ 3.5 MW (10 s avg, 1 s FFT); `0.1–5 Hz` adjacent_pair_sum ≤ 10 MW (60 s avg, 10 s FFT); `0.1–5 Hz` band_sum ≤ 20 MW.
- Fast 20 s: 60 s-window rules → `not_evaluable`. New **`band_long`** (120 s @ 0.01 s) evaluates them.
- Fresh preset runs (`lipa_pseg`): Noop **FAIL**, B1 **FAIL**, B3 seed101 **PASS** OH **3.38%**, GS seed101 **`evolved_compliant`** OH **3.48%** (was conservative floor 41.4% on old `lipa` seed101). Old CAISO-straw `lipa` profile still present with peak_bin-only bands.

## 3. Repo Map (Phase 4b additions)
- `server/app.py` — mounts `ui_router` + `agent_router`
- `server/agent_api.py` — harden SSE, tally, threads
- `rules/schema.py` / `rules/checker.py` — band aggregations
- `workload/synth.py` — `band_long_preset_workloads`
- `README.md` — managed-agent sandbox vs backend table

## 4. Agent API for the UI (`server/agent_api.py`, mounted from `server/app.py`)

`make run` serves UI + agents on one port. Never invents agent messages.

### `POST /api/agents/harden` (SSE)
Request JSON:
```json
{
  "controller_code": "def control(obs): ...",
  "constraints": [{"id":"...","constraint_type":"fluctuation_band","value":10.0,"unit":"MW","page":12,"snippet":"...","checkable":true,"freq_min_hz":0.1,"freq_max_hz":5.0}],
  "rated_mw": 100.0,
  "notes": "optional",
  "profile_id": "lipa"
}
```
SSE events (`event:` / `data:`):
- `status` — `{ok, message, thread_id?}` (agents off / errors said in plain words)
- `agent_message` — `{sender, receiver, summary, payload, latency_s?}` (engineer↔hardener; `summary` is one plain line; `payload` is full JSON)
- `backend_check` — `{kind: "counterexample"|"fix"|"host_rand20_probe", summary, break_found?|passed?, detail}`
- `verdict` — `{verdict: "no_break_within_budget"|"break_found_and_fixed"|"break_found_not_fixed"|"agents_disabled"|"unavailable", message, thread_id?, fixed_controller_code?}`
- `ping` — keepalive `{t}`

### `GET /api/agents/tally`
```json
{
  "running": true,
  "running_since_utc": "...",
  "run_id": "harden_...",
  "rounds": 2,
  "attacks_tried": 2,
  "breaks_found": 0,
  "fixes_confirmed": 0,
  "last_event_utc": "...",
  "targets": ["lipa","southern_dev"],
  "message": null
}
```

### `GET /api/agents/threads`
`{ "threads": [{thread_id, created_at_utc, kind, n_events, meta}], "count": N }`

### `GET /api/agents/threads/{id}`
`{ "thread_id", "meta", "events": [...], "summary": {...}|null }`

## 5. Common Commands
- `make test` / `make eval` / `make eval-final SEED=20261121`
- `make run` — UI + agent API + frontend
- `make agent-job PROFILE=lipa` / `make harden`
- `.venv/bin/python -u scripts/run_specs_reader.py --new-only`

## 6. Gotchas
- Saved-agent first turn needs `environment="remote"`. Follow-up `tools` must match `previous_interaction_id`.
- Prefer native profile id **`lipa_pseg`** for Table 4-2 aggregations; **`lipa`** is still the CAISO-straw restatement (peak_bin).
- `band_long` Nyquist is 50 Hz — enough for 0.1–5 Hz; 5–55 Hz is only partially covered there (fast family still carries high band).
