# GridSafe UI handoff

For a fresh agent. UI owns **local-loop** demo flow + agent attack panel + Results panel. Read `SPEC.md` Phase 5–6 for product intent; this file is what actually shipped.

## Ownership (no exceptions)
- **Write:** `frontend/**`, `server/ui_api.py`, `server/ui_dev.py` only.
- **Read-only:** everything else. Do not run formatters that rewrite outside ownership. User makes all commits (`AGENTS.md`).

## How to run
```bash
.venv/bin/uvicorn server.app:app --host 127.0.0.1 --port 8000 --reload
cd frontend && npm run dev   # proxies /api → 8000
```
Open `http://127.0.0.1:5173/`. Keys: **Space** / **C** / **R** / **Esc**.

**Caveat:** `--reload` wipes in-memory sessions. Prefer **R** (replay) after a backend edit. Parallel agents editing `server/*.py` will drop live demos.

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

## Libraries added
**None for the product.** (Pillow was used only to generate a test photo under `frontend/screenshots/_ramp_rule_input.png`; not a frontend dependency.)

## Screenshots
Under `frontend/screenshots/` from UI round 4 browser pass (1440×900 + 1920×1080 rulebook).

## UI round 4 stop (2026-11-21 ~14:20 ET)
Done: favicon logo, 30-min + cold-start cost, human confirm, PDF page picker, Results endpoint/panel, jargon plain-English for not_evaluable, E2E partial with timings in chat report.  
Agents harden stream started (engineer message seen) but was cut by parallel `server/agent_api.py` reload before verdict. Fix-it not exercised after session loss. Full Fix-it + agent-to-verdict needs a stable (non-reloading) backend.
