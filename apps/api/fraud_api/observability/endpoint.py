"""``GET /metrics`` (Prometheus text format) and the HTTP metrics middleware.

Access control (fail closed):
* ``FRAUD_METRICS_TOKEN`` unset → the endpoint answers ``404`` (metrics are not exposed).
* Set → requests must send ``Authorization: Bearer <token>`` (compared in constant time);
  anything else is ``401``. Prometheus sends it with ``authorization.credentials_file``.

The token is separate from user JWTs: metrics are operator data, not tenant data, and contain no
tenant, customer, transaction, case or user identifiers (see ``observability.metrics``). Health
endpoints (``/health/*``) stay separate and unauthenticated.
"""

from __future__ import annotations

import hmac
import time
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

from fraud_api.observability.metrics import (
    HTTP_DURATION,
    HTTP_REQUESTS,
    METHODS,
    REGISTRY,
    normalize,
)
from fraud_api.observability.stream_collector import StreamCollector
from fraud_api.stream.keys import StreamKeys

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
def metrics(request: Request) -> Response:
    settings = request.app.state.settings
    token = settings.metrics_token
    if token is None:
        return Response(status_code=404)
    header = request.headers.get("authorization", "")
    expected = f"Bearer {token.get_secret_value()}"
    if not hmac.compare_digest(header.encode(), expected.encode()):
        return Response(status_code=401, headers={"WWW-Authenticate": 'Bearer realm="metrics"'})
    body = generate_latest(REGISTRY)
    if request.app.state.redis is not None:
        scrape = CollectorRegistry()
        scrape.register(
            StreamCollector(
                request.app.state.redis,
                StreamKeys(settings.stream_prefix, settings.stream_shards),
            )
        )
        body += generate_latest(scrape)
    return Response(body, media_type=CONTENT_TYPE_LATEST)


async def record_http(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Count requests by method, *route template* and status (never the concrete path)."""
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        template = getattr(route, "path", None) or "unmatched"
        method = normalize(request.method, METHODS)
        HTTP_REQUESTS.labels(method, template, str(status)).inc()
        HTTP_DURATION.labels(method, template).observe(time.perf_counter() - start)
