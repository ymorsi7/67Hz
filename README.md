# GridSafe (`67Hz`)

Turns a utility large-load interconnection rulebook into a power-smoothing controller for an AI training cluster, verifies each limit against its source page, evolves and attacks the controller, and returns a certificate or a refusal.

![GridSafe](frontend/public/thumbnail.png)
## What it does

1. Read the rulebook (PDF, photo, or page) into typed interconnection limits.
2. Verify each rule against the cited page image (or abstain for human review).
3. Write a pure-numpy controller and check it with a deterministic checker.
4. Attack that controller, then fix only when the host checker accepts the fix.

## Run it

```bash
make demo
make demo-check
make eval-final SEED=20261121
```

`make demo` serves the UI + API on http://127.0.0.1:8000 (no auto-reload).
## Does it work?

Preset pass rates from [RESULTS.md](RESULTS.md) (`make eval-final` / `make report`):

| Method | Preset pass |
| --- | ---: |
| GridSafe | 33/33 (100.0%, 95% WI [89.6%, 100.0%]) |
| Tuned filter, no AI (B3) | 33/33 (100.0%, 95% WI [89.6%, 100.0%]) |
| Single Gemini call (B0 Neon) | 31/33 (93.9%, 95% WI [80.4%, 98.3%]) |
| Stock managed agent (B2, fair prompt) | 6/10 |
| No controller (Noop) | 1/11 (9.1%, 95% WI [1.6%, 37.7%]) |

### Where it fails

- On these presets, the no-AI tuned filter (B3) matches GridSafe at 33/33 ([FAILURES.md](FAILURES.md) F11; [RESULTS.md](RESULTS.md)).
- Ungated hardener fix on `lipa` hurt holdout SEED=4242: 19/20 → 13/20 (gated path keeps current on reject).
- Extraction vs 6 gold: precision 0.6785714285714286, recall 1.0; 1/9 FPs invented/wrong-type (IESO 600 MW as `max_step`).

Full tables: [RESULTS.md](RESULTS.md). Failures: [FAILURES.md](FAILURES.md). Quotes: [docs/NUMBERS.md](docs/NUMBERS.md). Long form: [docs/DETAILS.md](docs/DETAILS.md).

## Gemini 4, one job each

| Model | Job | Why that model |
| --- | --- | --- |
| `gemini-4-neon` | Reader | Interactions API + document/image input, page slicing, structured JSON out |
| `gemini-decision-1` | Verifier | Decisions API `noul` yes/no on the cited page image (zero output tokens) |
| `gemini-4-hydrogen` | Designer / Engineer | Fast parallel mutations; Antigravity agent `gridsafe-engineer` |
| `gemini-4-carbon` | Red-team / Hardener | Background Antigravity agent `gridsafe-hardener` for adversarial workloads |

Checker, shaft sim, and certificate math are plain Python + numpy — never AI. See [docs/API_NOTES.md](docs/API_NOTES.md).

## Managed agents

`gridsafe-engineer` (Hydrogen) drafts/fixes controllers; `gridsafe-hardener` (Carbon) proposes attacks.
They talk only through our host `message_agent` bridge (Interactions API); no API keys enter any sandbox.
Sandbox agents call custom functions; Reader, Verifier, evolve, certify, and checker re-checks run on our backend.
Sandbox `code_execution` is never trusted alone for compliance.
Disable with `managed_agent.enable_agents: false` in `config/models.yaml` for the local evolve path only.

## Repo map

| Folder | Role |
| --- | --- |
| `.agents/` | Managed-agent skills, hooks, AGENTS.md |
| `config/` | Model + IEEE FBM YAML |
| `docs/` | Spec, numbers, handoff, API notes, long README |
| `eval/` | Results JSON, gold labels, attacks, cache |
| `frontend/` | Vite/React UI |
| `scripts/` | Report, eval, harden helpers |
| `server/` | FastAPI app + agent/UI routes |
| `specs/` | Rulebook PDFs + sources |
| `src/` | Engine packages: agents, controllers, evolve, reader, rules, sim, workload |
| `tests/` | pytest |

## Built today

- Application code in this repo (backend, UI, evolve, checker, agents bridge, eval harness).
- Saved agents `gridsafe-engineer` / `gridsafe-hardener`, skills under `.agents/skills/`, host `message_agent` bridge.

## Not built by us

- `google-genai`, Gemini 4 models, Decisions API, Antigravity runtime.
- FastAPI, numpy/scipy, PyMuPDF, Vite/React/Tailwind, IEEE FBM params, public PDFs in `specs/SOURCES.md`.
