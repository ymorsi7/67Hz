# GridSafe UI handoff

For a fresh agent. UI owns **local-loop** demo flow (not managed agents). Read `SPEC.md` Phase 5–6 for product intent; this file is what actually shipped.

## Ownership (no exceptions)
- **Write:** `frontend/**`, `server/ui_api.py`, `server/ui_dev.py` only.
- **Read-only:** everything else (`server/app.py`, `SPEC.md`, `docs/`, `rules/`, `evolve/`, …). Do not run formatters that rewrite outside ownership. User makes all commits (`AGENTS.md`).

## How to run
```bash
# API (UI router only) — port 8001
.venv/bin/uvicorn server.ui_dev:app --host 127.0.0.1 --port 8001 --reload

# Vite — port 5173; proxies /api → 8001 (see vite.config.ts)
cd frontend && npm run dev
```
Open `http://127.0.0.1:5173/`. To serve from main app later, mount once in `server/app.py`:  
`from server.ui_api import ui_router` then `app.include_router(ui_router)` and flip the Vite proxy to `8000`.

## Frontend layout
| Path | Role |
|------|------|
| `src/App.tsx` | Full 4-step flow, keys, certificate panel |
| `src/components/Stepper.tsx` | Steps: Rulebook → Rules → Controller → Break |
| `src/components/ShaftCanvas.tsx` | Power / spectrum / 6-mass shaft canvases |
| `src/lib/api.ts` | Fetch + SSE (`streamPost`) helpers |
| `src/lib/plain.ts` | Client-side plain-rule templates + confirm badges |
| `src/components/ui/*` | shadcn button/badge/slider |

Keys: **Space** advance, **C** camera, **R** replay load, **Esc** start over.

## Design decisions
- **4 steps**, not 6. Certificate is a panel button on Controller/Break (not its own step).
- **Plain English first**; internal ids/outcomes (`fluctuation_band_0`, `fallback_b3`, `not_evaluable`) never on the main UI. Logs/diffs behind a closed **Details** toggle.
- Rule copy is **template-built** from typed fields (`plain.ts` / `plain_rule_sentence`), not an LLM.
- Attack evaluates **each rule on its own long-enough trace** (≥2× window; dt for Nyquist / ramp), never a fixed 2–6 s run.

## Session model (`UISession` in `ui_api.py`)
In-memory dict `_SESSIONS`. Fields: `session_id`, `source_name/kind`, `pdf_bytes`/`pdf_path`, `page_count`, `selected_pages`, `page_images`, `rated_mw`, `constraints[]`, `verification`, `reader_model`, `verifier_model`, `controller_code`, `evolve_result`, `traces`, `certificate`. Lost on process restart → use replay load.

## Endpoints (`/api/ui/...`)
| Method | Path | Body / notes | Response highlights |
|--------|------|--------------|---------------------|
| GET | `/health` | — | `status`, `git.{commit_count,first_commit_time}` (oldest commit) |
| POST | `/upload` | multipart `file`, `rated_mw` | `{session_id, page_count, source_name}` |
| POST | `/camera` | multipart image | `{session_id, page_count:1, thumbnail_url}` |
| GET | `/session/{id}/thumbnail/{page}` | `?dpi=` | PNG |
| POST | `/session/{id}/select-pages` | `{pages:[int]}` | `{selected_pages}` |
| POST | `/session/{id}/read-verify` | `{pages?}` **SSE** | events: `status`, `constraint`, `read_complete`, `verify_complete`, `done`/`error` |
| POST | `/session/{id}/evolve` | `{budget_s, population_size, seed}` **SSE** | `evolve` ticks; `evolve_complete` with `headline`, metrics (`extra_energy_pct`, `training_slowed_pct`, `battery_wear`), `best_code` |
| GET | `/session/{id}/traces` | after evolve | `power`, `spectrum.limit_bands`, `shaft` (masses/twists), `metrics` |
| GET | `/session/{id}/state` | — | constraints + evolve summary + certificate + flags |
| POST | `/session/{id}/attack` | `{pulse_rate, depth, cluster_mw}` (`rhythm` deprecated=period) | `{held, banner, n_rules, results[{plain, used_pct_of_limit, passed, cannot_check_reason?}], elapsed_ms}` |
| POST | `/session/{id}/fix-attack` | attack fields + `budget_s` **SSE** | redesign with `judge_attack` workload; `fix_complete` |
| GET | `/agent-threads` | — | `{threads:[{file,data}]}` from `eval/agent_threads/` (`.json`/`.jsonl`) |
| GET | `/replay/latest` | — | `{replay, label:"recorded"}` |
| POST | `/replay/load` | — | new session hydrated from `eval/tmp/replays/latest.json` |
| POST | `/session/{id}/save-replay` | — | writes under `eval/tmp/replays/` (gitignored) |

Constraint SSE/UI status: Verified / Needs human review / Uncheckable. Evolve outcomes mapped to headlines: Gemini winner / tuned filter baseline / safe expensive fallback / refused.

## Known issues / unfinished
- In-memory sessions die on uvicorn reload; press **R** or `POST /replay/load`.
- Long ramp windows (e.g. 600 s) make attack slow; UI shows “Checking…” / prefers slider **release** if >300 ms.
- `Fix it` is wired but lightly demoed under time pressure—budget ~60 s.
- Main app may not yet `include_router(ui_router)`; Vite currently targets **8001**.
- Parallel agents editing `rules/` can briefly break imports; retry after their reload.
- Projector/short viewports: Rulebook tiles may need scroll inside the main panel.
