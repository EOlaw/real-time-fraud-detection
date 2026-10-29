# Scoring API image. The model artifact is NOT baked in: it is mounted read-only at /app/model so
# a model version can be promoted or rolled back without rebuilding the image.
FROM python:3.12.13-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.12 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY ml/fraud_ml ml/fraud_ml
COPY apps/api apps/api
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12.13-slim-bookworm
# xgboost needs the OpenMP runtime.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home /app app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY apps/api/alembic.ini apps/api/alembic.ini
COPY apps/api/migrations apps/api/migrations
COPY ml/configs ml/configs
# OMP_NUM_THREADS=1: one XGBoost thread per prediction. With many request threads, the default
# (all cores) oversubscribes the CPU; measured in Phase 9: 260 vs 227 scores/s, p50 118 vs 135 ms,
# 122 % vs 170 % CPU at 32 concurrent clients, identical predictions (docs/operations/load-testing.md).
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 OMP_NUM_THREADS=1
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live').status==200 else 1)"
# --timeout-graceful-shutdown: open SSE feeds would otherwise hold a restart for up to 30 min;
# clients reconnect with Last-Event-ID. Note: with --workers 2, each scrape of /metrics reaches
# one worker process (Prometheus multiprocess mode is not configured; see docs/operations).
CMD ["sh", "-c", "alembic -c apps/api/alembic.ini upgrade head && exec uvicorn fraud_api.main:app --host 0.0.0.0 --port 8000 --workers 2 --timeout-graceful-shutdown 10"]
