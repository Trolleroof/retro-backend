.PHONY: dev down migrate api token test lint typecheck verify

dev:
	docker compose up --build

down:
	docker compose down

migrate:
	uv run alembic upgrade head

api:
	uv run uvicorn retro_backend.main:app --reload

token:
	uv run python -m retro_backend.token_cli --user-id "$(USER_ID)"

test:
	uv run pytest

lint:
	uv run ruff check .

typecheck:
	uv run basedpyright

verify: test lint typecheck
