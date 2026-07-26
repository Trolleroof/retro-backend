.PHONY: test lint typecheck verify node-preflight deployment-preflight prepare-deployment-config worker-sync worker-types worker-dry-run dev deploy migrate-local migrate-remote apply-r2-lifecycle e2e-local

D1_DATABASE ?= retro-backend
R2_BUCKET ?= retro-gameplay-raw
R2_LIFECYCLE ?= cloudflare/r2-lifecycle.json
DEPLOY_CONFIG ?= wrangler.toml
WRANGLER_VERSION ?= 4.112.0
DEV_IP ?= 127.0.0.1
PORT ?= 8787
UV_RUN := uv run --frozen
WRANGLER := npx --yes wrangler@$(WRANGLER_VERSION)

test:
	$(UV_RUN) pytest -q

lint:
	$(UV_RUN) ruff check src tests scripts

typecheck:
	$(UV_RUN) basedpyright src tests scripts/node_preflight.py scripts/deployment_preflight.py scripts/e2e_local.py

verify: test lint typecheck
	git diff --check

node-preflight:
	$(UV_RUN) python scripts/node_preflight.py

deployment-preflight:
	$(UV_RUN) python scripts/deployment_preflight.py --config $(DEPLOY_CONFIG)

prepare-deployment-config:
	$(UV_RUN) python scripts/deployment_preflight.py --render --config wrangler.toml --output wrangler.production.toml

worker-sync: node-preflight
	$(UV_RUN) pywrangler sync

worker-types: node-preflight
	$(UV_RUN) pywrangler types

worker-dry-run: worker-sync
	$(WRANGLER) deploy --dry-run --config $(DEPLOY_CONFIG)

dev: worker-sync
	$(WRANGLER) dev --local --ip $(DEV_IP) --port $(PORT)

deploy: worker-sync deployment-preflight
	$(WRANGLER) deploy --config $(DEPLOY_CONFIG)

# Requires `make dev` running in another shell; see docs/e2e-local.md.
e2e-local:
	$(UV_RUN) python scripts/e2e_local.py --base-url http://$(DEV_IP):$(PORT) \
		--database $(D1_DATABASE) --bucket $(R2_BUCKET)

migrate-local: node-preflight
	$(WRANGLER) d1 migrations apply $(D1_DATABASE) --local

migrate-remote: node-preflight deployment-preflight
	$(WRANGLER) d1 migrations apply $(D1_DATABASE) --remote --config $(DEPLOY_CONFIG)

apply-r2-lifecycle: node-preflight deployment-preflight
	$(WRANGLER) r2 bucket lifecycle set $(R2_BUCKET) --file $(R2_LIFECYCLE)
