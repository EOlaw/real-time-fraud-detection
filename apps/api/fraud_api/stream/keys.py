"""Redis key layout and customer sharding for the event stream.

  {prefix}:events:{n}        shard stream n (consumer group ``scorers``, consumer ``shard-{n}``)
  {prefix}:lease:{n}         worker lease for shard n (one active consumer per shard)
  {prefix}:attempts:{n}      hash: message id -> processing attempts (survives worker crashes)
  {prefix}:dlq               dead-letter stream (all shards)
  {prefix}:feed:{org_id}     persisted-assessment feed of one tenant (SSE source)

All of one customer's events (within one organization) hash to the same shard. Exactly one worker
consumes a shard at a time (lease), and it processes the shard strictly in stream order, so a
customer's events are scored in the order they were ingested.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

GROUP = "scorers"


@dataclass(frozen=True)
class StreamKeys:
    prefix: str
    shards: int

    def shard_for(self, organization_id: uuid.UUID, customer_id: str) -> int:
        digest = hashlib.sha256(f"{organization_id}:{customer_id}".encode()).digest()
        return int.from_bytes(digest[:8], "big") % self.shards

    def shard(self, n: int) -> str:
        return f"{self.prefix}:events:{n}"

    def lease(self, n: int) -> str:
        return f"{self.prefix}:lease:{n}"

    def attempts(self, n: int) -> str:
        return f"{self.prefix}:attempts:{n}"

    @staticmethod
    def consumer(n: int) -> str:
        return f"shard-{n}"

    @property
    def dlq(self) -> str:
        return f"{self.prefix}:dlq"

    def feed(self, organization_id: uuid.UUID) -> str:
        return f"{self.prefix}:feed:{organization_id}"

    def shard_index(self, key: str) -> int:
        return int(key.rsplit(":", 1)[1])
