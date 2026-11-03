.PHONY: install test lint typecheck cov check run seed init-db eval-offline

PY ?= python

install:
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest

eval-offline:
	$(PY) scripts/eval_offline.py

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

typecheck:
	$(PY) -m mypy

# Coverage report for app/; fails below [tool.coverage.report] fail_under.
cov:
	$(PY) -m pytest --cov=app --cov-report=term-missing:skip-covered --cov-report=xml

# Everything CI runs, in one target.
check: lint typecheck cov

run:
	$(PY) -m uvicorn app.main:app --host 127.0.0.1 --port 8000

seed:
	$(PY) -m app.cli seed

init-db:
	$(PY) -m app.cli init-db
