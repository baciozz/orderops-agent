.PHONY: install test lint run seed init-db

PY ?= python

install:
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

run:
	$(PY) -m uvicorn app.main:app --host 127.0.0.1 --port 8000

seed:
	$(PY) -m app.cli seed

init-db:
	$(PY) -m app.cli init-db
