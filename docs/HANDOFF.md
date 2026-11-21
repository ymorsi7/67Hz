# GridSafe Agent Handoff

Fresh-agent briefing for `/Users/yusufmorsi/Documents/GitHub/67Hz` (`ymorsi7/67Hz`, MIT). Also read `SPEC.md`, `docs/API_NOTES.md`, `FAILURES.md`, `RESULTS.md`.

## Hard rules
- **User commits/pushes** (`AGENTS.md`). Never `git commit` / `git push`; leave a commit message.
- Never invent results or write `eval/gold/*.json`. Numbers only from saved runs.
- No `GEMINI_API_KEY` in sandboxes. `workload/` `sim/` `rules/` `controllers/` `evolve/` = pure Python + numpy only.
- Held-out profiles only in `make eval-final`. Prefer profile **`lipa_pseg`** (native LIPA aggregations); **`lipa`** is CAISO-straw peak_bin.

## Status (Phases 0–7 + evidence check + acceptance gate)
**Built.** Physics/checker (incl. band aggregations + `band_long`), Reader/Verifier, evolve + B0/B1/B3, managed agents, UI mount, final eval docs, demo safety.

**Do not edit** `frontend/`, `server/ui_api.py`, `server/ui_dev.py` if the UI agent still owns them.

### Backend frozen after this change
| Item | What | Key artifact |
| --- | --- | --- |
| **Fix acceptance gate** | Accept fix only if (a) CX passes, (b) every preset passes, (c) validation Rand20 **SEED=9001** ≥ current. Reject → keep current + open issue | `agents/verify.py::gate_fix`, `AgentBridge.accept_fix`, `server/agent_api.py`, `agents/harden.py` |
| Hardener timeouts | Background `interactions.create` + poll; SSE heartbeat `still working, N seconds`; red-team skill **45 s** CX deadline | `agents/bridge.py`, `.agents/skills/red-team/SKILL.md` |
| Gated harden re-score | `lipa` / `lipa_pseg` under gate; holdout **SEED=31337** | `eval/results/harden_fix_31337.json` (ungated 4242 kept) |
| `make eval-final SEED=20261121` | Presets + random suite + extraction; **42.32 s** | `eval/results/eval_final_timing.json`, summary in `eval/results/final_20261121.json` |
| Controllers cache | 3-seed Noop/B1/B0/B3/GridSafe | `eval/cache/phase3_results.json` |
| `make eval-live SEED=<n>` | Cached controllers × 20 fresh workloads; ~42 s @ 20261121 | `eval/results/eval_live_<seed>.json` |
| Report pipeline | `scripts/build_final_results.py` + `scripts/report.py` | `make report` → `RESULTS.md` + `docs/NUMBERS.md` |
| `make demo` / `demo-check` | Port 8000, **no** reload; key/models/Decisions/agents/replay/UI | `eval/tmp/replays/latest.json` (gitignored) |
| Harden pause/resume | Flag + state; auto-pause on timeout | `make harden-pause` / `harden-resume`; `eval/attacks/HARDEN_PAUSE`, `hardening_state.json` |

### Evidence check done
| Item | Result | File |
| --- | --- | --- |
| B2 run1 (thin prompt) | **0/11** — mostly harness (`getattr`/`hasattr`/NameError) | `eval/results/b2_budget_20261121_run1_harness.json`, `b2_failure_analysis_run1.json` |
| B2 run2 (B0-aligned prompt, still no skills/hooks/checker) | **6/10** pass; 4 rule-breaks; `lipa` skipped (budget) | `eval/results/b2_budget_20261121_run2_fair_prompt.json` |
| Hardener ungated | `lipa` 19→13/20 @4242 (**hurt**); `lipa_pseg` 18→20/20 (**helped**) | `eval/results/harden_fix_4242.json` |
| Hardener gated | Both gate-rejected (`counterexample_still_breaks`); current kept; holdout @31337 unchanged (`lipa` 20→20, `lipa_pseg` 19→19); rejected lipa candidate would have been **18/20** | `eval/results/harden_fix_31337.json` |
| Background harden tally | 19 rounds, **20 timeouts**, 2 breaks, 2 fixes (pre-background-poll era) | `eval/attacks/hardening_tally.jsonl`, `eval/attacks/harden_*.json` |
| Extraction FPs | 9 total: **1** invented/wrong-type (IESO 600 MW load-loss as `max_step`), **8** quotes/dupes | `eval/results/fp_breakdown.json`; FAILURES F17 |

**Quote carefully:** GridSafe presets **33/33** = B3 **33/33**. Random all-pass GridSafe **9/11** vs B0 **11/11**. Do **not** quote B2 as 0/11 without saying run1 was harness-biased. Do **not** claim ungated hardener always helps — `lipa` @4242 regressed.

## Result file map (`eval/results/`)
- `final_20261121.json` — Wilson pass rates, family cost, degradation, hardening summary
- `eval_final_timing.json`, `eval_live_20261121.json`
- `b2_budget_20261121_run{1_harness,2_fair_prompt}.json` (+ analysis JSONs)
- `harden_fix_4242.json` (ungated), `harden_fix_31337.json` (gated), `fp_breakdown.json`
- Also: `eval/cache/phase3_results.json`, `eval/cache/extractions/`, `eval/gold/`, `eval/agent_threads/`, `RESULTS.md`, `docs/NUMBERS.md`
- Open rejected fixes: `eval/attacks/open_issues.jsonl`

## Agent bridge & `server/agent_api.py`
- **Bridge:** `agents/bridge.py` — host `message_agent`; no subagent nesting; checker re-checks every claim; **`accept_fix` gate** (CX + presets + SEED=9001); hardener turns use **background=True + poll** with heartbeats; threads → `eval/agent_threads/`.
- **Agents:** `gridsafe-engineer` (Hydrogen) / `gridsafe-hardener` (Carbon); provision `agents/provision.py`.
- **Mounted** from `server/app.py` with UI router. `make demo` / `make run` = one port.
- **API:** `POST /api/agents/harden` (SSE: status incl. `still working, N seconds`, agent_message, backend_check, verdict; reject stream: `fix rejected: it made things worse on N other workloads`); `GET /api/agents/tally`; `GET /api/agents/threads[/{id}]`.

## Commands
`make test` · `make eval` · `make eval-final SEED=20261121` · `make eval-live SEED=<n>` · `make report` · `make demo` · `make demo-check` · `make b2-budget` · `make harden` / `harden-pause` / `harden-resume` · `make agent-job PROFILE=lipa_pseg` · `python scripts/run_harden_fix_eval.py` (gated, SEED=31337)

## Open issues
1. **UI / Break-the-grid (Phases 5–6):** polish may still be on the UI agent; don’t touch their files unless ownership ends.
2. **`lipa` vs `lipa_pseg`:** straw profile still in eval; demo/native claims should use `lipa_pseg`.
3. **Hardener holdout mixed (ungated):** `lipa` hurt @4242; gated path now keeps current on reject (F16).
4. **IESO 600 MW typed as `max_step`:** Verifier confirms value-on-page, not rule kind (F17).
5. **B2 incomplete fair suite:** run2 skipped `lipa` (8‑min budget); re-run with higher budget if quoting all 11.
6. **`eval/tmp/` gitignored:** replay/demo assets are local-only; `make demo-check` needs a replay present on the laptop.
