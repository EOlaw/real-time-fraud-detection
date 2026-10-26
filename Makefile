# Common workflows. Secrets are read from .env (copy .env.example and fill it in).
SHELL := /bin/bash
ENV := set -a; [ -f .env ] && source .env; set +a;
RAW ?= ml/data/raw/sparkov/credit_card_transaction_train.csv ml/data/raw/sparkov/credit_card_transaction_test.csv
ORG ?= demo
SOURCE ?= backfill
DATA ?= ml/data/processed/card_transactions.parquet

.PHONY: install data ingest import train experiment mlflow-ui link-baseline promote-check promote db-up migrate api test test-unit test-integration lint typecheck check

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

promote:            ## Gated promotion of CANDIDATE (artifact + bound policy); see docs
	uv run python -m fraud_api.cli promote-model --artifact ml/artifacts/$(CANDIDATE) \
		--policy ml/configs/policies/$(CANDIDATE).yaml --apply

db-up:              ## Start PostgreSQL
	docker compose up -d --wait postgres

migrate:            ## Apply database migrations
	$(ENV) uv run alembic -c apps/api/alembic.ini upgrade head

api:                ## Run the API locally on :8000
	$(ENV) uv run uvicorn fraud_api.main:app --port 8000

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
