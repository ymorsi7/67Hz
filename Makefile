PYTHON := .venv/bin/python
PYTEST := .venv/bin/pytest
UVICORN := .venv/bin/uvicorn
PORT ?= 8000

.PHONY: test eval eval-final run replay demo-check build-ui

build-ui:
	cd frontend && npm run build

test:
	$(PYTEST) -q

eval:
	$(PYTHON) -m eval.run_eval --mode dev

eval-final:
	$(PYTHON) -m eval.run_eval --mode final

run: build-ui
	$(UVICORN) server.app:app --host 127.0.0.1 --port $(PORT)

replay: build-ui
	GRIDSAFE_MODE=replay $(UVICORN) server.app:app --host 127.0.0.1 --port $(PORT)

demo-check:
	$(PYTHON) scripts/demo_check.py
