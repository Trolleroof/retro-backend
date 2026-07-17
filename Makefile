.PHONY: dev down migrate api worker test lint typecheck verify

dev:
	docker compose up --build

down:
	docker compose down

migrate:
	uv run alembic upgrade head

api:
	uv run uvicorn retro_backend.main:app --reload

worker:
	uv run python -m retro_backend.worker

test:
	uv run pytest

lint:
	uv run ruff check .

typecheck:
	uv run basedpyright

verify: test lint typecheck
