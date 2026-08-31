.PHONY: install up down migrate test demo lint

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

up:
	docker compose up -d

down:
	docker compose down

migrate:
	cd backend && .venv/bin/alembic upgrade head

revision:
	cd backend && .venv/bin/alembic revision --autogenerate -m "$(m)"

test:
	cd backend && .venv/bin/pytest -q

demo:
	cd backend && .venv/bin/python demo.py

lint:
	cd backend && .venv/bin/ruff check .
