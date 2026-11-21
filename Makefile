PYTHON := .venv/bin/python
PYTEST := .venv/bin/pytest
UVICORN := .venv/bin/uvicorn
PORT ?= 8000
SEED ?= 20261121
RANDOM_N ?= 20
export PYTHONPATH := src:$(CURDIR)$(if $(PYTHONPATH),:$(PYTHONPATH),)

.PHONY: test eval eval-final eval-live report run replay demo demo-check build-ui fetch-specs agent-job harden harden-pause harden-resume minimal-exchange b2-budget

build-ui:
	cd frontend && npm run build

fetch-specs:
	$(PYTHON) scripts/fetch_specs.py

test:
	$(PYTEST) -q

eval:
	$(PYTHON) -m eval.run_eval --mode dev

eval-final:
	$(PYTHON) -m eval.run_eval --mode final --seed $(SEED) --random-n $(RANDOM_N)

eval-live:
	$(PYTHON) scripts/eval_live.py --seed $(SEED) --n $(RANDOM_N)

report:
	$(PYTHON) scripts/build_final_results.py --seed $(SEED) --random-n $(RANDOM_N)
	$(PYTHON) scripts/report.py

b2-budget:
	$(PYTHON) -u scripts/run_b2_budget.py --seed $(SEED) --budget-s 480

run: build-ui
	$(UVICORN) server.app:app --host 127.0.0.1 --port $(PORT)

# Demo: production-like serve, no auto-reload (stable for judges).
demo: build-ui
	$(UVICORN) server.app:app --host 127.0.0.1 --port $(PORT)

replay: build-ui
	GRIDSAFE_MODE=replay $(UVICORN) server.app:app --host 127.0.0.1 --port $(PORT)

demo-check:
	$(PYTHON) scripts/demo_check.py

minimal-exchange:
	$(PYTHON) -u scripts/run_minimal_agent_exchange.py

agent-job:
	$(PYTHON) -u scripts/run_agent_job.py --profile $(or $(PROFILE),lipa)

harden:
	$(PYTHON) -u scripts/run_hardening.py --profiles lipa southern_dev --max-cycles $(or $(MAX_CYCLES),50)

# Pause: touch flag (running loop blocks). Resume: remove flag, or restart from saved state.
harden-pause:
	@mkdir -p eval/attacks
	@touch eval/attacks/HARDEN_PAUSE
	@echo "Hardening paused (eval/attacks/HARDEN_PAUSE). Remove file or run make harden-resume."

harden-resume:
	@rm -f eval/attacks/HARDEN_PAUSE
	$(PYTHON) -u scripts/run_hardening.py --resume --profiles lipa southern_dev --max-cycles $(or $(MAX_CYCLES),50)
