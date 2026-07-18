.PHONY: test lint typecheck verify dev deploy migrate-local migrate-remote

D1_DATABASE ?= retro-backend
DEV_IP ?= 127.0.0.1
PORT ?= 8787
UV_RUN := uv run --frozen

test:
	$(UV_RUN) pytest -q

lint:
	$(UV_RUN) ruff check src tests scripts

typecheck:
	$(UV_RUN) basedpyright src tests

verify: test lint typecheck
	git diff --check

dev:
	$(UV_RUN) pywrangler dev --local --ip $(DEV_IP) --port $(PORT)

deploy:
	$(UV_RUN) pywrangler deploy

migrate-local:
	$(UV_RUN) pywrangler d1 migrations apply $(D1_DATABASE) --local

migrate-remote:
	$(UV_RUN) pywrangler d1 migrations apply $(D1_DATABASE) --remote
