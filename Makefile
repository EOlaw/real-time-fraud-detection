# Common workflows. Secrets are read from .env (copy .env.example and fill it in).
SHELL := /bin/bash
ENV := set -a; [ -f .env ] && source .env; set +a;
RAW ?= ml/data/raw/sparkov/credit_card_transaction_train.csv ml/data/raw/sparkov/credit_card_transaction_test.csv
MODEL ?= card-fraud-xgb-1.0.0

.PHONY: install data ingest train promote db-up migrate api test test-unit test-integration lint typecheck check

install:            ## Install Python dependencies (uv, Python 3.12)
	uv sync

data:               ## Download the dataset and verify SHA-256
	./scripts/download_dataset.sh

ingest:             ## Validate, minimize and tokenize raw data -> ml/data/processed
	$(ENV) uv run python -m fraud_ml.data.ingest $(foreach f,$(RAW),--raw $(f)) --out ml/data/processed

train:              ## Train/compare models, write artifact + evaluation report
	uv run python -m fraud_ml.training.train --config ml/configs/train_card_v1.yaml

promote:            ## Point ml/artifacts/current at MODEL (policy must be bound to it)
	ln -sfn $(MODEL) ml/artifacts/current

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
