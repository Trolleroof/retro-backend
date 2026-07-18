.PHONY: test lint typecheck verify dev deploy migrate-local migrate-remote

D1_DATABASE ?= retro-backend
PORT ?= 8787

test:
	uv run pytest -q

lint:
	uv run ruff check src tests scripts

typecheck:
	uv run basedpyright src tests

verify: test lint typecheck
	git diff --check

dev:
	uv run pywrangler dev --local --port $(PORT)

deploy:
	uv run pywrangler deploy

migrate-local:
	uv run pywrangler d1 migrations apply $(D1_DATABASE) --local

migrate-remote:
	uv run pywrangler d1 migrations apply $(D1_DATABASE) --remote
