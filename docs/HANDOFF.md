# GridSafe Agent Handoff

Fresh-agent briefing for `/Users/yusufmorsi/Documents/GitHub/67Hz` (public repo `ymorsi7/67Hz`, MIT). Read `SPEC.md`, `docs/API_NOTES.md`, and `FAILURES.md` alongside this file before starting Phase 5.

## 1. Hard Rules & User Workflow (Non-Negotiable)
- **User makes ALL commits and pushes (`AGENTS.md`):** NEVER run `git commit` or `git push`. End every completed phase with a one-line commit message for the user and wait for `"next phase"`.
- **Never invent results or gold labels:** Every metric, table, and claim must come from an executed run in this repo. Never write `eval/gold/*.json` entries yourself (user creates them via `scripts/label.py --checkable-only` with `"labeled_by": "human"`).
- **Never leak secrets into sandboxes:** Keep `.env` (`GEMINI_API_KEY`) strictly on the host backend. Never pass API keys into `evolve/_runner.py` or Antigravity remote sandboxes.
- **Sandbox package boundary:** `workload/`, `sim/`, `rules/`, `controllers/`, and `evolve/` must remain pure Python + `numpy` only (no `scipy`, `pymupdf`, `fitz`, or `pydantic`; enforced by AST unit tests).
- **Held-out profiles** run only in `make eval-final` (dev eval skips them).

## 2. Phase Status
- **Step 0–Phase 3b (Done):** See prior handoff history. Physics, Reader/Verifier, Designer, B0/B1/B3, dual families, idle-start honesty, Rand20 suite.
- **Part A quick fixes (Done, uncommitted with Phase 4):**
  1. **Gold audit (human-only):** all 6 labeled gold JSONs + `TEMPLATE.json` have `labeled_by=human`. Agents did not author gold constraint entries; files were created via human `scripts/label.py` / user commits (see Part A report in chat).
  2. **Energy ledger:** absolute `1e-9 MWh` gate replaced by relative residual `< 1e-12` (`ENERGY_LEDGER_REL_TOL`); non-vacuity mutation test kept; recorded as **F13**.
  3. **Random suite:** every profile gets **20 evaluable** workloads via `sample_random_suite_for_ruleset` (single-family profiles get 20 from that family alone).
  4. **B3 ladder:** Designer starts from tuned LPF+battery B3; outcome ladder is evolved (must not score worse than B3) → B3 → conservative floor → refusal.
- **Phase 4 (Done enough to continue — agents live, hardening running):**
  - Saved agents: `gridsafe-engineer` (`gemini-4-hydrogen`) + `gridsafe-hardener` (`gemini-4-carbon`) with skills under `.agents/skills/*/SKILL.md`, `.agents/AGENTS.md`, `.agents/hooks.json`.
  - `message_agent` bridge via Interactions API (`agents/bridge.py`); backend re-checks controllers/counterexamples (`agents/verify.py`); threads in `eval/agent_threads/`.
  - **Minimal exchange latency (measured):** hardener hop **10.1 s**; full engineer→`message_agent`→hardener→engineer **26.7 s** (`eval/agent_threads/minimal_exchange_*`).
  - **LIPA F11 cycle:** hardener packaged break (24.0 s), engineer `fallback_b3` fix (25.3 s), backend confirmed presets+Rand20 pass and CX fixed; logged `eval/agent_threads/lipa_f11_20261121T182024Z*`.
  - **Job run:** `scripts/run_agent_job.py` / `make agent-job` (local evolve path smoke: lipa `evolved_compliant` in 34.6 s). Full agent-attack job uses `enable_agents` flag.
  - **Hardening run:** `scripts/run_hardening.py` / `make harden` started; tally at `eval/attacks/hardening_tally.jsonl`.
  - **Baseline B2:** `evolve/b2.py` + `evolve.baselines.run_b2_baseline` (stock Antigravity, no skills/hooks/functions/checker; host scores after).
  - Feature flag: `config/models.yaml` → `managed_agent.enable_agents` (set `false` to keep local evolve only).
- **Phases 5–7 (Not started):** UI flow, Break-the-grid, final eval docs/`RESULTS.md`.

## 3. Repo Map (Phase 4 additions)
- `.agents/` — `AGENTS.md`, `hooks.json`, skills (`read-rulebook`, `evolve-controller`, `certify`, `red-team`, `analyze-failure`).
- `agents/` — `client.py`, `tools.py`, `sources.py`, `provision.py`, `bridge.py`, `verify.py`, `thread_log.py`, `job.py`, `harden.py`.
- `evolve/b2.py` — Baseline B2.
- `scripts/run_minimal_agent_exchange.py`, `run_lipa_harden_cycle.py`, `run_agent_job.py`, `run_hardening.py`.
- `eval/agent_threads/`, `eval/attacks/`, `eval/jobs/`.
- `Makefile` targets: `minimal-exchange`, `agent-job`, `harden`.

## 4. Common Commands
- `make test` / `make eval` / `make eval-final SEED=20261121`
- `make agent-job PROFILE=lipa`
- `make harden` (leave running; watch `eval/attacks/hardening_tally.jsonl`)
- `.venv/bin/python -u scripts/run_lipa_harden_cycle.py`
- `.venv/bin/python -u scripts/run_minimal_agent_exchange.py`

## 5. Decisions & Gotchas
- Interactions with saved agents require `environment='remote'` (or prior `environment_id`) on the first turn.
- Follow-up turns: `tools` / `system_instruction` are interaction-scoped and **must match** `previous_interaction_id` (F14 ack bug).
- Full hardener red-team with long code_execution can exceed client read timeouts — prefer short JSON attack proposals + host checker (F14).
- Gold files are human-only. Do not edit `eval/gold/*.json` as an agent.
- `enable_agents: false` keeps job/harden behind local evolve if the agent path degrades.

## 6. Next (Phase 5)
- Horizontal stepper UI, agent thread viewer, certificate surface, canvas shaft/spectrum.
- Wire live job SSE to `agents.job.run_job`.
- Keep hardening tally visible in the side panel.
