.PHONY: test lint typecheck verify node-preflight worker-sync worker-types worker-dry-run dev deploy migrate-local migrate-remote

D1_DATABASE ?= retro-backend
DEV_IP ?= 127.0.0.1
PORT ?= 8787
UV_RUN := uv run --frozen

test:
	$(UV_RUN) pytest -q

lint:
	$(UV_RUN) ruff check src tests scripts

typecheck:
	$(UV_RUN) basedpyright src tests scripts/node_preflight.py

verify: test lint typecheck
	git diff --check

node-preflight:
	$(UV_RUN) python scripts/node_preflight.py

worker-sync: node-preflight
	$(UV_RUN) pywrangler sync

worker-types: node-preflight
	$(UV_RUN) pywrangler types

worker-dry-run: node-preflight
	$(UV_RUN) pywrangler deploy --dry-run

dev: node-preflight
	$(UV_RUN) pywrangler dev --local --ip $(DEV_IP) --port $(PORT)

deploy: node-preflight
	$(UV_RUN) pywrangler deploy

migrate-local: node-preflight
	$(UV_RUN) pywrangler d1 migrations apply $(D1_DATABASE) --local

migrate-remote: node-preflight
	$(UV_RUN) pywrangler d1 migrations apply $(D1_DATABASE) --remote
