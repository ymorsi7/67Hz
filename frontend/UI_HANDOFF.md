# GridSafe UI handoff

For a fresh agent. UI owns **local-loop** demo flow + agent attack panel + Results panel. Read `docs/SPEC.md` Phase 5–6 for product intent; this file is what actually shipped.

## Ownership (no exceptions)
- **Write:** `frontend/**`, `server/ui_api.py`, `server/ui_dev.py` only.
- **Read-only:** everything else. Do not run formatters that rewrite outside ownership. User makes all commits (`AGENTS.md`).

## Repo layout note
Engine packages live under `src/` (Python imports unchanged). Product spec is `docs/SPEC.md`.

## How to run
```bash
.venv/bin/uvicorn server.app:app --host 127.0.0.1 --port 8000 --reload
cd frontend && npm run dev   # proxies /api → 8000
```
Open `http://127.0.0.1:5173/`. Keys: **Space** / **C** / **R** / **E** (explainer if present) / **Esc**.

**Caveat:** `--reload` wipes in-memory sessions. Prefer **R** (replay) after a backend edit. Parallel agents editing `server/*.py` will drop live demos.

**Demo safety:** A built copy may be serving on port 8000. Do not stop/restart that process, do not run `make demo`, and do not run `vite build` unless the presenter asks.

## Branding
Header logo is `/favicon.png` (rounded 40×40), not a Lucide shield. Matches the dark page.

## Controller cost (UI round 4)
Traces include:
- `metrics.from_30min` / `span_label` / primary `extra_energy_pct` + `training_slowed_pct` from **slow family 1800 s**
- `metrics.cold_start.training_slowed_pct` from a real **20 s** cold-start sim
UI: “Over a 30-minute run: …” with cold-start line underneath. Chart workload stays short/fine so frequency-band rules stay checkable (no Nyquist jargon).

## Human confirm
`POST /api/ui/session/{id}/confirm-constraint` → `human_confirmed`, builds use the rule, badge/certificate: “Confirmed by a person, not by the Verifier”.

## Add another rule
- Camera/image → append + Reader/Verifier; empty photo → “No rule found in this photo” + Retake.
- **Another PDF page** → thumbnail picker for the current PDF.

## Results panel
Header **Results** → `GET /api/ui/docs/results` (reads `RESULTS.md` + `FAILURES.md` fresh each time). Client `MarkdownDoc` (no new npm libs). Missing file → “not generated yet”.

## Agents panel (UI round 5)
On **Break it**:
- Default: `GET /api/ui/agent-threads/featured` — most recent completed `eval/agent_threads/` conversation that reached a verdict, preferring a break. Labeled **Recorded today at HH:MM**. Rejection copy is reason-specific from thread data only: `counterexample_still_breaks` / validation worse-on-N / presets failed — never invent a reason.
- Live button: **Run a new round now (takes about 5 minutes)** → `POST /api/agents/harden` SSE. Heartbeat shows `still working, N seconds`. Sliders stay usable while it runs. Tally line kept.

## Explainer (UI round 5)
If `frontend/public/explainer.mp4` exists (or is served as `/explainer.mp4`), Rulebook shows **Watch the 18-second explainer**; **E** does the same. Full-screen, muted; ends or Esc → back to Rulebook. Missing file → no button.

## Waiting (UI round 5)
During Read / Verify / Build: live elapsed-seconds counter + one plain line naming the model (“Gemini 4 Neon is reading page 7”, “Gemini Decision 1 is checking each rule against the page”, “Gemini 4 Hydrogen is writing 6 controllers”).

## Libraries added
**None for the product.** (Pillow was used only to generate a test photo under `frontend/screenshots/_ramp_rule_input.png`; not a frontend dependency.)

## Screenshots
Under `frontend/screenshots/` from UI round 4 browser pass (1440×900 + 1920×1080 rulebook).

## UI round 5 stop (2026-11-21 ~15:25 ET)
Done: recorded Agents panel + live round rename/heartbeat, explainer hook (file optional), busy elapsed + model lines, SPEC path fix in this handoff, `tsc -b` clean.
