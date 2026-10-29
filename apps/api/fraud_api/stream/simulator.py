"""Deterministic replay / simulation of transaction events into ``POST /api/v1/events``.

    # Replay historical events (labeled source=replay)
    FRAUD_SIM_PASSWORD=... uv run python -m fraud_api.stream.simulator \
        --email svc@demo.example.com --mode replay \
        --start 2020-06-21T12:14:00+00:00 --limit 20000 --rate 0 --chunk 500
    # Synthetic events (labeled source=simulated), reproducible from --seed
    ... --mode simulated --n 5000 --customers 200 --seed 7 --rate 200

Pacing: event *i* is due at ``max(i / rate, (event_time_i - event_time_0) / speed)`` seconds
after start (``rate=0``: no rate cap; ``speed=0``: ignore event-time gaps). Events are sent in
chunks of at most ``--chunk``; a partial chunk is flushed before waiting for a future event.
Pause/resume: ``SIGUSR1`` pauses, ``SIGUSR2`` resumes, or write ``pause`` / ``run`` into
``--control-file``. Paused time is excluded from the schedule.

Determinism: the event list and its order are a pure function of the inputs (data window, or
seed + counts); the report's ``sequence_sha256`` covers every sent transaction ID in order. Wall
clock timing naturally varies between runs.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from fraud_ml.domains.card_payments import MERCHANT_CATEGORIES

Event = dict[str, Any]
Sender = Callable[[list[Event]], dict[str, Any]]


@dataclass(frozen=True)
class SimConfig:
    rate: float = 0.0  # events per second, 0 = unlimited
    speed: float = 0.0  # event-time seconds per wall second, 0 = ignore event-time gaps
    chunk: int = 100


def replay_events(data: Path, start: datetime, limit: int) -> list[Event]:
    import pandas as pd

    df = pd.read_parquet(data)
    df = df[df["event_time"] >= start].sort_values(["event_time", "event_id"], kind="mergesort")
    return [
        {
            "transaction_id": r["event_id"],
            "customer_id": r["customer_id"],
            "merchant_id": r["merchant_id"],
            "amount": f"{r['amount']:.2f}",
            "currency": r["currency"],
            "category": r["category"],
            "event_time": r["event_time"].isoformat(),
            "region": r["customer_region"],
            "source": "replay",
        }
        for r in df.head(limit).to_dict("records")
    ]


def simulated_events(n: int, customers: int, seed: int, start: datetime) -> list[Event]:
    """Synthetic events from a seeded generator. Not real traffic; labeled source=simulated."""
    rng = np.random.default_rng(seed)
    cust = [
        "cus_" + hashlib.sha256(f"sim:{seed}:{c}".encode()).hexdigest()[:16]
        for c in range(customers)
    ]
    gaps = rng.exponential(scale=20.0, size=n)  # seconds between consecutive events
    offsets = np.cumsum(gaps)
    picks = rng.integers(0, customers, size=n)
    cats = rng.choice(np.array(MERCHANT_CATEGORIES), size=n)
    amounts = np.round(rng.gamma(2.0, 35.0, size=n) + 1.0, 2)
    return [
        {
            "transaction_id": f"sim_{seed}_{i:08d}",
            "customer_id": cust[int(picks[i])],
            "merchant_id": "Simulated Merchant",
            "amount": f"{amounts[i]:.2f}",
            "currency": "USD",
            "category": str(cats[i]),
            "event_time": (start + timedelta(seconds=float(offsets[i]))).isoformat(),
            "source": "simulated",
        }
        for i in range(n)
    ]


def due_offsets(events: list[Event], cfg: SimConfig) -> list[float]:
    if not events:
        return []
    t0 = datetime.fromisoformat(events[0]["event_time"])
    out = []
    for i, e in enumerate(events):
        by_rate = i / cfg.rate if cfg.rate > 0 else 0.0
        by_speed = (
            (datetime.fromisoformat(e["event_time"]) - t0).total_seconds() / cfg.speed
            if cfg.speed > 0
            else 0.0
        )
        out.append(max(by_rate, by_speed))
    return out


class Control:
    """Pause/resume via signals or a control file containing 'pause' or 'run'."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.paused = False

    def install_signals(self) -> None:
        signal.signal(signal.SIGUSR1, lambda *_: setattr(self, "paused", True))
        signal.signal(signal.SIGUSR2, lambda *_: setattr(self, "paused", False))

    def is_paused(self) -> bool:
        if self.path is not None and self.path.exists():
            self.paused = self.path.read_text().strip().lower() == "pause"
        return self.paused


class Simulator:
    def __init__(
        self,
        events: list[Event],
        cfg: SimConfig,
        send: Sender,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        control: Control | None = None,
    ) -> None:
        self.events, self.cfg, self.send = events, cfg, send
        self.clock, self.sleep, self.control = clock, sleep, control or Control()

    def run(self) -> dict[str, Any]:
        due = due_offsets(self.events, self.cfg)
        digest = hashlib.sha256()
        buf: list[Event] = []
        totals = {"requests": 0, "accepted": 0, "rejected": 0}
        paused_s = 0.0
        start = self.clock()

        def flush() -> None:
            if not buf:
                return
            result = self.send(list(buf))
            totals["requests"] += 1
            totals["accepted"] += int(result.get("accepted", 0))
            totals["rejected"] += int(result.get("rejected", 0))
            for e in buf:
                digest.update(e["transaction_id"].encode() + b"\n")
            buf.clear()

        for i, event in enumerate(self.events):
            while self.control.is_paused():
                flush()
                t = self.clock()
                self.sleep(0.1)
                paused_s += self.clock() - t
            wait = start + paused_s + due[i] - self.clock()
            if wait > 0:
                flush()
                self.sleep(wait)
            buf.append(event)
            if len(buf) >= self.cfg.chunk:
                flush()
        flush()
        elapsed = self.clock() - start
        sources: dict[str, int] = {}
        for e in self.events:
            sources[e["source"]] = sources.get(e["source"], 0) + 1
        return {
            "events": len(self.events),
            **totals,
            "sources": sources,
            "elapsed_seconds": elapsed,
            "paused_seconds": paused_s,
            "send_rate_events_per_s": len(self.events) / elapsed if elapsed > 0 else None,
            "sequence_sha256": digest.hexdigest(),
            "event_time_range": [self.events[0]["event_time"], self.events[-1]["event_time"]]
            if self.events
            else None,
        }


def http_sender(api: str, email: str, password: str, max_backpressure_retries: int = 20) -> Sender:
    import httpx

    client = httpx.Client(base_url=api, timeout=60.0)
    token = client.post("/api/v1/auth/token", json={"email": email, "password": password})
    token.raise_for_status()
    client.headers["Authorization"] = f"Bearer {token.json()['access_token']}"

    def send(items: list[Event]) -> dict[str, Any]:
        for _ in range(max_backpressure_retries):
            r = client.post("/api/v1/events", json={"items": items})
            if r.status_code == 503:  # backpressure / Redis unavailable: wait, retry same chunk
                time.sleep(float(r.headers.get("Retry-After", "1")))
                continue
            r.raise_for_status()
            return r.json()  # type: ignore[no-any-return]
        raise RuntimeError("ingest kept returning 503")

    return send


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="fraud_api.stream.simulator")
    p.add_argument("--api", default="http://127.0.0.1:8000")
    p.add_argument("--email", required=True)
    p.add_argument("--mode", choices=["replay", "simulated"], required=True)
    p.add_argument("--data", type=Path, default=Path("ml/data/processed/card_transactions.parquet"))
    p.add_argument(
        "--start", type=datetime.fromisoformat, default=datetime(2020, 6, 21, 12, 14, tzinfo=UTC)
    )
    p.add_argument("--limit", type=int, default=1000, help="replay: number of events")
    p.add_argument("--n", type=int, default=1000, help="simulated: number of events")
    p.add_argument("--customers", type=int, default=100)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--rate", type=float, default=0.0)
    p.add_argument("--speed", type=float, default=0.0)
    p.add_argument("--chunk", type=int, default=100)
    p.add_argument("--control-file", type=Path)
    p.add_argument("--out", type=Path)
    args = p.parse_args(argv)

    if args.mode == "replay":
        events = replay_events(args.data, args.start, args.limit)
    else:
        events = simulated_events(args.n, args.customers, args.seed, args.start)
    password = os.environ.get("FRAUD_SIM_PASSWORD") or getpass.getpass("Password: ")
    control = Control(args.control_file)
    control.install_signals()
    sim = Simulator(
        events,
        SimConfig(args.rate, args.speed, args.chunk),
        http_sender(args.api, args.email, password),
        control=control,
    )
    report = {
        "mode": args.mode,
        "seed": args.seed,
        "started_at": datetime.now(UTC).isoformat(),
        **sim.run(),
    }
    text = json.dumps(report, indent=2)
    if args.out:
        args.out.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
