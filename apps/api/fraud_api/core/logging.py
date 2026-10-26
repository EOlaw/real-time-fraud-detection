"""Structured JSON logging with correlation IDs and key-based redaction."""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from contextvars import ContextVar
from typing import Any

import structlog

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)

REDACTED_KEYS = frozenset(
    {
        "password",
        "authorization",
        "token",
        "access_token",
        "jwt_secret",
        "customer_id",
        "database_url",
        "cc_num",
        "pan",
    }
)


def _redact(_: Any, __: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key in list(event):
        if key.lower() in REDACTED_KEYS:
            event[key] = "[REDACTED]"
    return event


def _add_correlation_id(
    _: Any, __: str, event: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    cid = correlation_id_var.get()
    if cid:
        event.setdefault("correlation_id", cid)
    return event


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_correlation_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
