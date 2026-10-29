"""Stream message envelope (version 1).

The tenant (``org``) and actor come from the authenticated principal at ingest time, never from
the request body. ``payload`` is the validated ``ScoreRequest`` as canonical JSON, so the worker
scores exactly what was accepted.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from fraud_api.scoring.schemas import ScoreRequest

ENVELOPE_VERSION = "1"


class MalformedMessage(ValueError):
    """The envelope itself is unusable (missing/invalid fields)."""


class InvalidPayload(ValueError):
    """The envelope is fine but the payload fails ScoreRequest validation."""


@dataclass(frozen=True)
class Envelope:
    organization_id: uuid.UUID
    actor_id: uuid.UUID
    correlation_id: str
    ingested_at: datetime
    payload: dict[str, Any]
    allow_late: bool = False
    dlq_replay_of: str | None = None

    def fields(self) -> dict[str, str]:
        return {
            "v": ENVELOPE_VERSION,
            "org": str(self.organization_id),
            "actor": str(self.actor_id),
            "cid": self.correlation_id,
            "ingested_at": self.ingested_at.isoformat(),
            "payload": json.dumps(self.payload, sort_keys=True, separators=(",", ":")),
            "allow_late": "1" if self.allow_late else "0",
            "dlq_replay_of": self.dlq_replay_of or "",
        }

    def request(self) -> ScoreRequest:
        try:
            return ScoreRequest.model_validate(self.payload)
        except ValidationError as exc:
            raise InvalidPayload(
                "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in exc.errors())
            ) from exc


def make_envelope(
    organization_id: uuid.UUID, actor_id: uuid.UUID, correlation_id: str, req: ScoreRequest
) -> Envelope:
    return Envelope(
        organization_id=organization_id,
        actor_id=actor_id,
        correlation_id=correlation_id,
        ingested_at=datetime.now(UTC),
        payload=req.model_dump(mode="json"),
    )


def parse_envelope(fields: dict[str, str]) -> Envelope:
    try:
        if fields.get("v") != ENVELOPE_VERSION:
            raise MalformedMessage("unsupported envelope version")
        payload = json.loads(fields["payload"])
        if not isinstance(payload, dict):
            raise MalformedMessage("payload is not an object")
        return Envelope(
            organization_id=uuid.UUID(fields["org"]),
            actor_id=uuid.UUID(fields["actor"]),
            correlation_id=fields.get("cid", "")[:64],
            ingested_at=datetime.fromisoformat(fields["ingested_at"]),
            payload=payload,
            allow_late=fields.get("allow_late") == "1",
            dlq_replay_of=fields.get("dlq_replay_of") or None,
        )
    except MalformedMessage:
        raise
    except (KeyError, ValueError, TypeError) as exc:
        # Only the exception type: field values must not leak into DLQ reasons or logs.
        raise MalformedMessage(f"malformed envelope ({type(exc).__name__})") from exc
