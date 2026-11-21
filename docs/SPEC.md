# GridSafe build spec
One-day hackathon build (Gemini 4 NYC Hackathon, Google DeepMind judges). Build window 10:30 to 15:50 ET, submission by 16:45. Laptop only.

## Hard rules (breaking one can disqualify us)
1. All code is written today in this repo. No agent frameworks: write our own evolve loop. Libraries are allowed and listed in README under "Not built by us".
2. No Streamlit. The product is an agent flow, not a dashboard.
3. Never invent results. Every number shown comes from a run in this repo.
4. The live path accepts inputs nobody prepared. Replay is a labeled fallback only.
5. The evaluation may not consist only of examples we wrote.
6. Public repo, MIT license, .env ignored, no keys committed.
7. Label anything simulated, recorded, simplified, or visually exaggerated on screen.
8. If a result looks too good, assume a bug. Ask me before any decision that changes what we claim.

## Product (Problem Statement One, "5 Sigma")
Task agents fail at today: turn a utility's large-load interconnection rulebook (long, messy, often scanned PDFs) into a power-smoothing controller for an AI training cluster that verifiably complies with it. GridSafe reads the document, verifies each extracted limit against its source page, evolves a controller against a deterministic checker, attacks its own result, and returns a compliant controller with a certificate or an explicit refusal. It never returns an unverified answer.
Input is documents and camera, not a text box: drop a PDF, photograph a printed page, or handwrite a rule on paper and hold it to the webcam.

## Models, one job each (config/models.yaml, one-line swaps)
- gemini-4-neon: Reader. Documents, page images, and photos in; typed constraints with page citations out.
- gemini-decision-1 via the Decisions API: Verifier. One calibrated yes or no per constraint against the cited page image. Below threshold, abstain and mark "Needs human review".
- gemini-4-hydrogen: Designer. Fast parallel controller mutations, and the model for the managed agent.
- gemini-4-carbon: Red team and hardener, background only. If unavailable, Neon takes this job.
Never drop below two Gemini 4 models with distinct jobs.

## STEP 0 (10:30 to 11:00): docs and scaffold
Read before writing any API code:
- https://aistudio.google.com/llms.txt
- https://aistudio.google.com/docs/coding-agents.md
- https://aistudio.google.com/docs/custom-agents
- https://ai.google.dev/gemini-api/docs/antigravity-agent
- The current shadcn/ui install guide for Vite.
Record in docs/API_NOTES.md: model IDs, Interactions API call shape, structured output, PDF and image input, document slicing, thinking levels, Decisions API request and response, managed agents (agents.create, base environment from a repository, AGENTS.md, skills, custom functions, hooks, budget, background). Follow the docs where they differ from this spec. If the Decisions API is unavailable on our key, use Hydrogen with a strict yes or no schema and label it a fallback everywhere.
Scaffold:
- Backend: Python 3.11, FastAPI, uvicorn, numpy, scipy, PyMuPDF, pytest, google-genai. Server-sent events for streaming.
- Frontend: Vite + React + TypeScript, shadcn/ui on Tailwind, Motion for transitions, lucide-react icons, Geist Sans and Geist Mono. Built assets served by FastAPI.
- Makefile: test, eval, eval-final, run, replay, demo-check.
- If the React scaffold is not running by 11:00, fall back to one index.html with vanilla JS and continue.
- Applied Step 0 decisions: (1) two saved agents (`gridsafe-engineer` on `gemini-4-hydrogen`, `gridsafe-hardener` on `gemini-4-carbon`); (2) Neon Reader and `gemini-decision-1` Verifier run on backend and are exposed as custom functions `read_rulebook` / `verify_constraint` (camera photos may also pass inline); (3) repo mounted at `/workspace/repo`; (4) sandbox code (`src/workload/`, `src/sim/`, `src/rules/`, `src/controllers/`, `src/evolve/` or legacy top-level twins) is pure Python + `numpy` only (no `scipy`; PyMuPDF backend-only; `pytest` installed once in forked base env); (5) hooks are a second layer only-backend always re-runs checker and tests before showing compliance; (6) Vite/TS7/shadcn tooling adjustments accepted.

## PHASE 1 (11:00 to 11:45): physics and checker, no AI
- workload/synth.py: training load traces from parameters (period, duty cycle, depth, jitter, checkpoints, job start and stop), scaled to a cluster size.
- sim/shaft.py: multi-mass turbine-generator shaft, IEEE First Benchmark Model. Parameters and published mode frequencies come from config/fbm.yaml, which I supply. If it is missing, ask me. Never invent values. If I say skip, use one resonant mode labeled "illustrative".
- sim/plant.py: time-stepped controller plus battery (power limit, energy limit, efficiency, wear proxy) giving net grid power, then frequency-domain shaft torque.
- rules/schema.py: ramp_rate, max_step, fluctuation_band, dynamic_range. Fields: value, unit, page, snippet, checkable, confidence.
- rules/units.py: normalize to MW, seconds, Hz.
- rules/checker.py: deterministic, pure Python, pass or fail with margin.
- Gates: mode frequencies within 2 percent of published values (docs/VALIDATION.md). One evaluation under 0.5 seconds.
- Tests: energy conservation, determinism, hand-computed checker cases including values exactly at the limit, unit conversions (kW and MW, per second and per minute, percent of rated load).

## PHASE 2 (11:45 to 12:30): Reader and Verifier
- reader/extract.py: PDF, image, or camera frame to constraints via strict schema. Uncheckable constraints are kept with checkable=false.
- reader/verify.py: Decisions API call per constraint; store confidence; abstain below threshold.
- eval/degrade.py: messy copies of pages (rotation, blur, low resolution, skew, partial crop) at 4 severities.
- eval/extraction.py: precision, recall, hallucination rate, abstention rate against eval/gold/*.json. If no gold files exist, report "no gold labels" and never fabricate them.
- Tests: page with no numeric limits yields zero constraints; conflicting limits are flagged; unreadable photo abstains; one offline smoke test on recorded API responses.
- Start FAILURES.md now: each failure category, an example, what changed, number before and after.

## PHASE 3 (12:30 to 13:15): Designer and baselines
- controllers/seed.py: def control(obs) -> action, under 60 lines, numpy only. Actions: GPU power floor, power cap, battery setpoint.
- evolve/loop.py: 6 to 8 parallel Hydrogen mutations per generation, sandboxed subprocess with timeout, static ban on file, network, and import tricks, hard gate on the checker, then score on energy overhead, throughput loss, battery wear. Feed back the failing constraint and margin. 75 second budget, streamed.
- Guaranteed outcome: a compliant controller, else the conservative fixed-floor controller if it passes, else an explicit refusal with the reason.
- Baselines: B0 one Neon call, document in, controller out. B1 fixed 90 percent GPU power floor.
- Tests: a known-bad controller fails the gate; banned code is rejected.

## PHASE 4 (13:15 to 14:05): managed agent backbone
- Two saved managed agents with distinct skills mounted at `.agents/skills/<name>/SKILL.md`:
  - `gridsafe-engineer` (base `antigravity-preview-09-2026`, model `gemini-4-hydrogen`): skills `read-rulebook`, `evolve-controller`, `certify`.
  - `gridsafe-hardener` (base `antigravity-preview-09-2026`, model `gemini-4-carbon`): skills `red-team`, `analyze-failure`.
  - Both use a remote base environment with this GitHub repo mounted at `/workspace/repo` and `pytest` pre-installed once into the forked base environment.
- Sandbox package & secret boundaries: everything the sandbox runs under `src/` (`workload/`, `sim/`, `rules/`, `controllers/`, `evolve/`; legacy top-level layout also ok) is pure Python plus `numpy` only (no `scipy`; PyMuPDF stays backend-only). Never place `GEMINI_API_KEY` inside any sandbox.
- .agents/AGENTS.md: plan checklist (read, verify, evolve, attack, certify); the deterministic checker is ground truth; refuse instead of guessing.
- Document/vision boundary: agents never read PDFs themselves. Neon (Reader) and `gemini-decision-1` (Verifier) run on our backend and are exposed to `gridsafe-engineer` as custom functions `read_rulebook` and `verify_constraint` (alongside `report_progress`, `request_human_review`, and `message_agent`). Camera photos may also pass as inline images.
- Multi-agent communication via Gemini Interactions API (`message_agent` bridge): official Managed Agents docs (`custom-agents.md`) explicitly state `"No subagent nesting: Subagent delegation is not yet supported."` Therefore, agents communicate through a custom function `message_agent`. When `gridsafe-engineer` calls `message_agent`, our backend creates/continues an interaction with `gridsafe-hardener` through the Gemini Interactions API (`client.interactions.create`), preserving each agent's stateful thread via `previous_interaction_id` and `environment`, and returns the hardener's reply as the `function_result`.
- Adversarial review protocol: `gridsafe-engineer` submits a candidate controller plus the verified constraints to `gridsafe-hardener`. `gridsafe-hardener` attacks it inside its own isolated sandbox and replies with counterexample workloads as structured JSON, or `"no break within budget"`. `gridsafe-engineer` fixes the controller and resubmits. Stop after 2 clean rounds or when the token/time budget ends.
- Auditability & UI: every message between agents is structured JSON, logged to `eval/agent_threads/`, shown in the UI as a visible agent-to-agent thread, and summarized on the certificate (rounds, attacks, fixes).
- Ground-truth verification: hooks are a second layer only (blocking controllers that fail the checker/tests inside the sandbox). Our backend independently re-runs the deterministic checker and tests on every submitted controller AND every claimed counterexample workload before returning or displaying compliance. An agent's claim is never trusted without it.
- Token budget per job. Jobs run in the background and stream steps.
- Job run: one forked run per rulebook, outputting controller, certificate, checked-off plan, and logged agent thread.
- Hardening run: start in Phase 4 and leave running until freeze on `gridsafe-hardener` (`gemini-4-carbon`). It attacks, logs verified breaks to `eval/attacks/`, coordinates fixes, and reruns eval with a timestamped tally.
- Live path: time one job run. Under 2 minutes, the judge's input goes to the agent. Slower, the local loop answers in 75 seconds while the agent runs the same job in the background and the UI shows whether they agree.
- Baseline B2: stock Antigravity agent, same document, no skills, hooks, functions, or checker.

## PHASE 5 (14:05 to 14:45): the flow
- Horizontal stepper: Input, Read, Verify, Evolve, Attack, Certify. Current step large, finished steps collapse to one line. One primary action per screen. First screen is Input.
- Read and Verify: page image beside constraints, each Verified with confidence or Needs human review (styled as a feature).
- Evolve: streaming code diffs, rejected candidates marked at the gate, score against B1.
- Result: canvas 2D shaft that twists and reddens with torque (label "visual scale") and calms when the controller is on; power before and after; spectrum with forbidden bands shaded. Canvas for anything streaming.
- Certify: each rule, page citation, margin, attacks survived, which model did which job and the Gemini 4 capability it relies on.
- Side panel: agent step log and hardening tally. Results panel reachable from the stepper: baselines, compliance with interval, failures.
- Look: dark theme; green for safe, red for violation, one neutral accent; tabular numerals; body text 16px minimum, key numbers 48px or more; readable on a projector at 1920x1080.
- States: streaming progress on every async step, designed empty, error, and abstain states, Retry or Replay on API failure.
- Keys: Space advances, C camera, R Replay (labeled "recorded"), Escape resets in under 1 second.
- Footer: "Built today at the Gemini 4 NYC Hackathon", commit count, first commit time.

## PHASE 6 (14:45 to 15:15): Break the grid
Judge sets three sliders (rhythm, depth, cluster size) to attack the controller. Show attacks survived. If it breaks, show the failure and run the fix live. Save every judge input to eval/live/ with a separate on-screen tally.

## PHASE 7 (15:15 to 15:50): final eval and docs
- make eval-final: cases are rulebook x workload from real documents in specs/, degraded copies, red-team workloads, and live inputs. Held-out split includes one utility never used in development.
- RESULTS.md: end-to-end compliance with 95 percent Wilson interval for GridSafe local, GridSafe agent, B0, B1, B2; extraction metrics; compliance versus degradation severity; attack survival before and after hardening; one ablation with the Verifier off; energy overhead versus B1. If time allows, rerun Reader and Designer with gemini-3.8-flash and report the difference.
- README: one-command run, "Built today" versus "Not built by us", model-to-job table, results, simulator limits.
- docs/DEMO.md: 3 minute live script and 1 minute video script.
- make demo-check: key, model access, camera, agent reachable, Replay works.

## Cut order if behind
1. Extra ablations and the older-model comparison. 2. Multi-mass shaft (single mode). 3. Carbon (Neon takes its job). 4. Hooks (enforce in certify skill). 5. Visual polish beyond the flow.
Never cut: the agent job run, the hardening run, the Verifier with abstention, B0, B1, B2, the held-out eval, FAILURES.md, live PDF or camera input, Replay.
