# GridSafe UI handoff

For a fresh agent. UI owns **local-loop** demo flow + agent attack panel. Read `SPEC.md` Phase 5–6 for product intent; this file is what actually shipped.

## Ownership (no exceptions)
- **Write:** `frontend/**`, `server/ui_api.py`, `server/ui_dev.py` only.
- **Read-only:** everything else (`server/app.py`, `server/agent_api.py`, `SPEC.md`, `docs/`, `rules/`, `evolve/`, …). Do not run formatters that rewrite outside ownership. User makes all commits (`AGENTS.md`).

## How to run
```bash
# Full stack (UI router + agent API) — port 8000
.venv/bin/uvicorn server.app:app --host 127.0.0.1 --port 8000 --reload

# Vite — port 5173; proxies /api → 8000 (see vite.config.ts)
cd frontend && npm run dev
```
Open `http://127.0.0.1:5173/`. Keys: **Space** advance, **C** camera, **R** replay load, **Esc** start over.

UI-only fallback (no agents): `uvicorn server.ui_dev:app --port 8001` and temporarily point the Vite proxy at 8001.

## Frontend layout
| Path | Role |
|------|------|
| `src/App.tsx` | Full 4-step flow: Rulebook → Rules → Controller → Break |
| `src/components/Stepper.tsx` | Horizontal steps |
| `src/components/RuleBar.tsx` | Plain rule + limit tick + measured fill (Controller + Break) |
| `src/components/PowerChart.tsx` | Animated L→R power chart (MW / seconds) |
| `src/components/ShaftAnim.tsx` | Compact shaft, animated with the chart |
| `src/components/ShaftCanvas.tsx` | Spectrum (and legacy power/shaft) under evidence |
| `src/lib/api.ts` | Fetch + SSE; UI + `/api/agents/*` helpers |
| `src/lib/plain.ts` | Template plain-rule sentences |

## Controller screen (UI round 3 — rebuilt for non-experts)
Top → bottom, no jargon:
1. **Verdict** (large green check): “Follows all N rules” / refusal; then winner sentence.
2. **GridSafe OFF/ON** large switch (starts **OFF**).
3. **One bar per rule** from `traces.rule_bars` — OFF uses uncontrolled measured %, ON uses controlled. Over-limit: “N times over the limit”; under: “uses X% of the limit”. Tick at 100%.
4. **One chart**: “Power the data center pulls from the grid” (animated). Caption about training jumps.
5. **Shaft** (smaller, synced animation). Caption with real `times_lower`. Label: simulated, visual scale.
6. **Cost** one line: extra energy % + training slowed %.
7. Buttons: View controller code · Certificate · **Try to break it**.
8. **Show the evidence** (closed): spectrum, raw log, diff.

**View controller code** panel: Python + Copy + author line from real outcome  
(`Written by Gemini 4 Hydrogen…` / `Tuned filter baseline (no AI)…`).

## Rules / Rulebook (round 3)
- Rulebook: “Choose one to start” + **or** between tiles; **Back** on page picker; tiles sized for 1440×900 / 1920×1080.
- Rules: **Add another rule** (camera photo or another PDF page). Appends via `read-verify?append=true`. Cards show `source_label` (“CAISO, page 14” / “Added by camera”) + **Remove**. Duplicate limit types show “the stricter rule decides”.

## Break it + agents
- Same `RuleBar` with limit tick.
- **Let the AI agents attack it** → `POST /api/agents/harden` SSE. Conversation lines for Engineer (Hydrogen) / Hardener (Carbon); JSON behind toggle; stamps from `backend_check`.
- Tally line from `GET /api/agents/tally` (real file only). On failure: **Agents not connected** — never invent messages.

## UI API extras (`server/ui_api.py`)
| Method | Path | Notes |
|--------|------|-------|
| POST | `/session/{id}/add-image` | Append camera/page image as new page |
| POST | `/session/{id}/remove-constraint` | `{constraint_id}` — clears controller |
| POST | `/session/{id}/read-verify` | body `{pages, append?}` — append merges constraints |
| GET | `/session/{id}/traces` | includes `rule_bars[]` with `used_pct_off` / `used_pct_on` |
| SSE | evolve/fix complete | also `verdict`, `winner`, `author_line`, `outcome` |

## Session model
Unchanged in-memory `_SESSIONS`. Lost on reload → **R** / `POST /replay/load`.

## Known issues / unfinished
- In-memory sessions die on uvicorn reload; press **R**.
- “Another PDF page” auto-picks the next unread page in the same PDF; otherwise falls back to an image file picker (not a second full PDF upload).
- Agent harden can be slow / disabled via `enable_agents`; UI must show real status only.
- Long ramp windows still make attack slow.
- Parallel agents editing backend packages can briefly break imports; retry after reload.

## UI round 3 stop note (2026-11-21 ~14:20 ET)
Done: Controller rebuild, code panel, Rulebook/Rules add-remove, Vite→8000, agent attack + tally wiring.  
Main app on **8000** with live tally (proxy verified). Replay load rebuilds traces when `rule_bars` missing and refreshes certificate verdict fields.  
Verified on recorded run: OFF bar showed real “5.7 times over the limit”; ON switch, cost line, shaft `4.9×`, agent harden SSE live.
