"""Stream gauges read from Redis at scrape time (no background polling).

* ``fraud_stream_backlog{shard}``: queued + in-flight messages (processed ones are deleted).
* ``fraud_stream_pending{shard}``: delivered to a worker, not yet acknowledged.
* ``fraud_stream_consumer_lag_seconds{shard}``: age of the oldest message still in the shard,
  i.e. how far behind the workers are (0 when the shard is empty). Taken from the Redis stream ID
  (enqueue time in ms), so it measures queue time, not event time.
* ``fraud_stream_dead_letters``: entries in the dead-letter stream.
* ``fraud_stream_up``: 1 if Redis answered during this scrape, else 0.

``shard`` is bounded by ``FRAUD_STREAM_SHARDS`` (≤ 256).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import redis
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from fraud_api.observability.metrics import REDIS_ERRORS
from fraud_api.stream.keys import StreamKeys
from fraud_api.stream.ops import stream_status


class StreamCollector(Collector):
    def __init__(self, client: redis.Redis, keys: StreamKeys) -> None:
        self.client, self.keys = client, keys

    def collect(self) -> Iterator[GaugeMetricFamily]:
        up = GaugeMetricFamily("fraud_stream_up", "Redis reachable during this scrape")
        try:
            status = stream_status(self.client, self.keys)
            pipe = self.client.pipeline(transaction=False)
            for n in range(self.keys.shards):
                pipe.xrange(self.keys.shard(n), count=1)
            oldest: list[Any] = pipe.execute()
        except redis.RedisError:
            REDIS_ERRORS.labels("api", "status").inc()
            up.add_metric([], 0)
            yield up
            return
        up.add_metric([], 1)
        yield up
        backlog = GaugeMetricFamily(
            "fraud_stream_backlog", "Queued and in-flight messages", labels=["shard"]
        )
        pending = GaugeMetricFamily(
            "fraud_stream_pending", "Delivered but unacknowledged messages", labels=["shard"]
        )
        lag = GaugeMetricFamily(
            "fraud_stream_consumer_lag_seconds",
            "Age of the oldest message still in the shard",
            labels=["shard"],
        )
        now_ms = time.time() * 1000
        for s, first in zip(status["shards"], oldest, strict=True):
            shard = str(s["shard"])
            backlog.add_metric([shard], s["backlog"])
            pending.add_metric([shard], s["pending"])
            age = 0.0
            if first:
                age = max(0.0, (now_ms - int(str(first[0][0]).split("-")[0])) / 1000)
            lag.add_metric([shard], age)
        yield backlog
        yield pending
        yield lag
        dead = GaugeMetricFamily("fraud_stream_dead_letters", "Entries in the dead-letter stream")
        dead.add_metric([], status["dead_letters"])
        yield dead
