# Common workflows. Secrets are read from .env (copy .env.example and fill it in).
SHELL := /bin/bash
ENV := set -a; [ -f .env ] && source .env; set +a;
RAW ?= ml/data/raw/sparkov/credit_card_transaction_train.csv ml/data/raw/sparkov/credit_card_transaction_test.csv
ORG ?= demo
SOURCE ?= backfill
DATA ?= ml/data/processed/card_transactions.parquet

.PHONY: install data ingest import train experiment mlflow-ui link-baseline promote-check promote db-up migrate api web test test-unit test-integration lint typecheck check redis-up stream-init worker simulate stream-status monitoring-token monitoring-up monitoring-reference monitoring-run monitoring-list load-test

install:            ## Install Python dependencies (uv, Python 3.12)
	uv sync

data:               ## Download the dataset and verify SHA-256
	./scripts/download_dataset.sh

ingest:             ## Validate, minimize and tokenize raw data -> ml/data/processed
	$(ENV) uv run python -m fraud_ml.data.ingest $(foreach f,$(RAW),--raw $(f)) --out ml/data/processed

import:             ## Import canonical parquet into PostgreSQL (ORG, SOURCE, optional START/END)
	$(ENV) uv run python -m fraud_api.cli import-transactions --org $(ORG) --data $(DATA) \
		--source $(SOURCE) $(if $(START),--start $(START)) $(if $(END),--end $(END))

train:              ## Train/compare models, write artifact + evaluation report
	uv run python -m fraud_ml.training.train --config ml/configs/train_card_v1.yaml

experiment:         ## Phase 3: tuning, imbalance comparison, SHAP, anomaly baseline, MLflow
	uv run python -m fraud_ml.training.experiment --config ml/configs/experiment_card_v2.yaml

mlflow-ui:          ## Browse local MLflow runs at http://127.0.0.1:5000
	MLFLOW_DISABLE_AGENT_HINT=1 uv run mlflow ui --backend-store-uri sqlite:///ml/mlruns/mlflow.db \
		--host 127.0.0.1 --port 5000

link-baseline:      ## First-time setup: serve the Phase 1 baseline (policy is bound to it)
	ln -sfn card-fraud-xgb-1.0.0 ml/artifacts/current

CANDIDATE ?= card-fraud-xgb-1.1.0
promote-check:      ## Run promotion gates for CANDIDATE (changes nothing)
	uv run python -m fraud_api.cli promote-model --artifact ml/artifacts/$(CANDIDATE) \
		--policy ml/configs/policies/$(CANDIDATE).yaml

promote:            ## Fully gated, audited promotion: CANDIDATE= START= END= OPERATOR=
	$(ENV) uv run python -m fraud_api.monitoring.cli promote --artifact ml/artifacts/$(CANDIDATE) \
		--policy ml/configs/policies/$(CANDIDATE).yaml --start $(START) --end $(END) \
		--operator "$(OPERATOR)" --apply

db-up:              ## Start PostgreSQL
	docker compose up -d --wait postgres

migrate:            ## Apply database migrations
	$(ENV) uv run alembic -c apps/api/alembic.ini upgrade head

api:                ## Run the API locally on :8000
	$(ENV) uv run uvicorn fraud_api.main:app --port 8000

web:                ## Run the analyst dashboard on :3000 (proxies to the API on :8000)
	cd apps/web && npm install --no-audit --no-fund && npm run dev

test-unit:
	uv run pytest tests/unit

test-integration:   ## Requires `make db-up`
	uv run pytest tests/integration

test:
	uv run pytest

lint:
	uv run ruff check . && uv run ruff format --check .

typecheck:
	uv run mypy ml/fraud_ml apps/api/fraud_api

check: lint typecheck test

# ---- Phase 5: event streaming ------------------------------------------------------------
WORKER ?= w0
SHARDS ?= all
EMAIL ?= svc@demo.example.com
LIMIT ?= 2000

redis-up:           ## Start Redis (streams) on 127.0.0.1:6380
	docker compose up -d --wait redis

stream-init:        ## Create consumer groups for every shard (idempotent)
	$(ENV) uv run python -m fraud_api.stream.cli init

worker:             ## Run a stream worker: make worker WORKER=w0 SHARDS=0-3
	$(ENV) uv run python -m fraud_api.stream.worker --worker-id $(WORKER) --shards $(SHARDS)

simulate:           ## Replay held-out events through the stream (FRAUD_SIM_PASSWORD required)
	$(ENV) uv run python -m fraud_api.stream.simulator --email $(EMAIL) --mode replay \
		--limit $(LIMIT) --chunk 200

stream-status:      ## Backlog, pending, leases and dead letters
	$(ENV) uv run python -m fraud_api.stream.cli status

# ---- Phase 9: observability, drift monitoring, load tests
monitoring-token:   ## Copy FRAUD_METRICS_TOKEN from .env into the file Prometheus reads
	@$(ENV) test -n "$$FRAUD_METRICS_TOKEN" || { echo "set FRAUD_METRICS_TOKEN in .env"; exit 1; }
	@$(ENV) umask 077 && printf '%s' "$$FRAUD_METRICS_TOKEN" > infrastructure/monitoring/secrets/metrics_token
	@echo "wrote infrastructure/monitoring/secrets/metrics_token (git-ignored)"

monitoring-up: monitoring-token  ## Prometheus (127.0.0.1:9090) and Grafana (127.0.0.1:3001)
	@$(ENV) test -n "$$GRAFANA_ADMIN_PASSWORD" -a "$$GRAFANA_ADMIN_PASSWORD" != "change-me" || \
		{ echo "set GRAFANA_ADMIN_PASSWORD in .env"; exit 1; }
	docker compose --profile monitoring up -d prometheus grafana

monitoring-reference:  ## Reference profile for the serving model (validation window)
	$(ENV) uv run python -m fraud_api.monitoring.cli build-reference

monitoring-run:     ## Drift + label-matured monitoring: make monitoring-run START=... END=...
	$(ENV) uv run python -m fraud_api.monitoring.cli run --start $(START) --end $(END) --all-orgs

monitoring-list:    ## Recent monitoring runs
	$(ENV) uv run python -m fraud_api.monitoring.cli list

load-test:          ## make load-test ARGS="--api ... --email ... --scenario single --mode burst"
	$(ENV) uv run python scripts/load_test.py $(ARGS)
