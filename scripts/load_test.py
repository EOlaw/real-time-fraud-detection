"""Concurrent load test for scoring, batch, ingestion + stream workers, and SSE fan-out.

This is a load test, not a replay: many concurrent clients send at once.

* ``--mode burst``: closed loop, ``--concurrency`` clients send back to back as fast as the system
  answers (maximum throughput and saturation).
* ``--mode steady``: open loop, requests are *scheduled* at ``--rate`` per second and latency is
  measured from the scheduled time, so queueing under overload is counted (no coordinated
  omission); ``--concurrency`` caps in-flight requests.

Scenarios (``--scenario``):
  single   POST /fraud/score, one event per request
  batch    POST /fraud/batch, ``--batch-size`` events per request
  ingest   POST /events, then wait until the stream is drained; end-to-end (send → scored and on
           the SSE feed) latency per event from a listening SSE client
  sse      ``--sse-clients`` concurrent SSE connections while ``single`` runs in steady mode;
           delivery latency (score response → event received) per connection

Every event is a real held-out event with a run-unique transaction id (``lt<tag>_<event_id>``),
labeled ``source=simulated`` because the ids are synthetic. ``--dup-rate`` re-sends that share of
events (identical payload) concurrently to check duplicate effects; afterwards the database is
checked: exactly one transaction and one assessment per unique id.

Resources: process CPU/RSS (``--pid``), Docker container CPU/memory (``--container``), PostgreSQL
commits, rollbacks, deadlocks and connections (``--db-url``), sampled every second.

    FRAUD_LOAD_PASSWORD=... uv run python scripts/load_test.py --api http://127.0.0.1:8009 \\
        --email service@load.example.com --scenario single --mode burst --concurrency 32 \\
        --events 5000 --db-url postgresql://... --pid 1234 \\
        --container real-time-fraud-detection-postgres-1
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import random
import re
import statistics
import subprocess
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd

DATA = Path("ml/data/processed/card_transactions.parquet")


def pct(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"n": 0, "p50": None, "p95": None, "p99": None, "max": None, "mean": None}
    a = np.asarray(values)
    return {
        "n": len(values),
        "p50": round(float(np.percentile(a, 50)), 2),
        "p95": round(float(np.percentile(a, 95)), 2),
        "p99": round(float(np.percentile(a, 99)), 2),
        "max": round(float(a.max()), 2),
        "mean": round(float(a.mean()), 2),
    }


def load_events(n: int, start: str, tag: str) -> list[dict[str, Any]]:
    df = pd.read_parquet(DATA)
    df = df[df["event_time"] >= pd.Timestamp(start)].head(n)
    return [
        {
            "transaction_id": f"lt{tag}_{e.event_id}"[:128],
            "customer_id": e.customer_id,
            "merchant_id": e.merchant_id,
            "amount": f"{e.amount:.2f}",
            "currency": "USD",
            "category": e.category,
            "event_time": pd.Timestamp(str(e.event_time)).isoformat(),
            "region": e.customer_region,
            "source": "simulated",
        }
        for e in df.itertuples()
    ]


@dataclass
class Stats:
    latencies_ms: list[float] = field(default_factory=list)
    status: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    items: Counter[str] = field(default_factory=Counter)
    replays: int = 0
    started: float = 0.0
    finished: float = 0.0

    def report(self, unit: str = "requests") -> dict[str, Any]:
        elapsed = max(self.finished - self.started, 1e-9)
        ok = sum(n for s, n in self.status.items() if s.startswith("2"))
        return {
            unit: sum(self.status.values()),
            "ok": ok,
            "elapsed_s": round(elapsed, 2),
            "throughput_per_s": round(ok / elapsed, 1),
            "latency_ms": pct(self.latencies_ms),
            "status_codes": dict(self.status),
            "errors": dict(self.errors),
            "item_statuses": dict(self.items),
            "idempotent_replays": self.replays,
        }


class Sampler:
    """Samples process, container and database resources once per second."""

    def __init__(self, pids: list[int], containers: list[str], db_url: str | None) -> None:
        self.pids, self.containers, self.db_url = pids, containers, db_url
        self.samples: list[dict[str, Any]] = []
        self._stop = asyncio.Event()
        self.db_start: dict[str, int] | None = None
        self.db_end: dict[str, int] | None = None

    def _db(self) -> dict[str, int] | None:
        if not self.db_url:
            return None
        q = (
            "select xact_commit, xact_rollback, deadlocks, (select count(*) from pg_stat_activity "
            "where datname = current_database()) from pg_stat_database where datname = "
            "current_database()"
        )
        out = subprocess.run(
            ["psql", self.db_url, "-Atc", q], capture_output=True, text=True, check=False
        ).stdout.strip()
        if not out:
            return None
        c, r, d, conns = (int(x) for x in out.split("|"))
        return {"commits": c, "rollbacks": r, "deadlocks": d, "connections": conns}

    def _procs(self) -> dict[int, dict[str, float]]:
        out = subprocess.run(
            ["ps", "-o", "pid=,%cpu=,rss=", "-p", ",".join(map(str, self.pids))],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return {
            int(p): {"cpu_pct": float(c), "rss_mb": round(int(r) / 1024, 1)}
            for p, c, r in (line.split() for line in out.strip().splitlines())
        }

    def _containers(self) -> dict[str, dict[str, Any]]:
        out = subprocess.run(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}",
                *self.containers,
            ],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return {
            n: {"cpu_pct": float(c.rstrip("%")), "mem": m.split(" / ")[0]}
            for n, c, m in (line.split("|") for line in out.strip().splitlines() if line)
        }

    async def run(self) -> None:
        # Every probe runs in a worker thread: `docker stats` alone takes 1-2 s and must never
        # block the event loop that drives (and times) the load.
        self.db_start = await asyncio.to_thread(self._db)
        while not self._stop.is_set():
            sample: dict[str, Any] = {"t": time.time()}
            if self.pids:
                sample["procs"] = await asyncio.to_thread(self._procs)
            if self.containers:
                sample["containers"] = await asyncio.to_thread(self._containers)
            db = await asyncio.to_thread(self._db)
            if db:
                sample["db_connections"] = db["connections"]
            self.samples.append(sample)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), 1.0)
        self.db_end = await asyncio.to_thread(self._db)

    def stop(self) -> None:
        self._stop.set()

    def report(self) -> dict[str, Any]:
        out: dict[str, Any] = {"samples": len(self.samples)}
        procs: dict[int, list[dict[str, float]]] = {}
        conts: dict[str, list[float]] = {}
        for s in self.samples:
            for p, v in s.get("procs", {}).items():
                procs.setdefault(p, []).append(v)
            for n, v in s.get("containers", {}).items():
                conts.setdefault(n, []).append(v["cpu_pct"])
        out["processes"] = {
            str(p): {
                "cpu_pct_mean": round(statistics.mean(x["cpu_pct"] for x in v), 1),
                "cpu_pct_max": max(x["cpu_pct"] for x in v),
                "rss_mb_max": max(x["rss_mb"] for x in v),
            }
            for p, v in procs.items()
        }
        out["containers"] = {
            n: {"cpu_pct_mean": round(statistics.mean(v), 1), "cpu_pct_max": max(v)}
            for n, v in conts.items()
        }
        conns = [s["db_connections"] for s in self.samples if "db_connections" in s]
        if conns:
            out["db_connections_max"] = max(conns)
        if self.db_start and self.db_end:
            out["db_delta"] = {
                k: self.db_end[k] - self.db_start[k] for k in ("commits", "rollbacks", "deadlocks")
            }
        return out


async def token(client: httpx.AsyncClient, email: str, password: str) -> str:
    r = await client.post("/api/v1/auth/token", json={"email": email, "password": password})
    r.raise_for_status()
    return str(r.json()["access_token"])


async def drive(
    send: Any, units: list[Any], mode: str, concurrency: int, rate: float, stats: Stats
) -> None:
    """Burst: closed loop with ``concurrency`` workers. Steady: open-loop schedule at ``rate``."""
    stats.started = time.perf_counter()
    if mode == "burst":
        queue: asyncio.Queue[Any] = asyncio.Queue()
        for u in units:
            queue.put_nowait(u)

        async def worker() -> None:
            while not queue.empty():
                u = queue.get_nowait()
                t0 = time.perf_counter()
                await send(u, t0)

        await asyncio.gather(*(worker() for _ in range(concurrency)))
    else:
        sem = asyncio.Semaphore(concurrency)
        start = time.perf_counter()

        async def one(i: int, u: Any) -> None:
            due = start + i / rate
            await asyncio.sleep(max(0.0, due - time.perf_counter()))
            async with sem:
                await send(u, due)  # latency from the scheduled time

        await asyncio.gather(*(one(i, u) for i, u in enumerate(units)))
    stats.finished = time.perf_counter()


def with_duplicates(events: list[dict[str, Any]], rate: float, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    out = list(events)
    dups = rng.sample(events, int(len(events) * rate)) if rate > 0 else []
    for d in dups:  # re-send concurrently: inserted near the original
        out.insert(min(len(out), out.index(d) + rng.randint(0, 3)), d)
    return out


async def scenario_single(
    client: httpx.AsyncClient,
    tok: str,
    events: list[dict[str, Any]],
    args: argparse.Namespace,
    on_scored: Any = None,
) -> dict[str, Any]:
    stats = Stats()
    h = {"Authorization": f"Bearer {tok}"}

    async def send(e: dict[str, Any], t0: float) -> None:
        try:
            r = await client.post("/api/v1/fraud/score", json=e, headers=h)
            stats.status[str(r.status_code)] += 1
            if r.status_code == 200:
                body = r.json()
                stats.replays += int(body["idempotent_replay"])
                if on_scored:
                    on_scored(body["assessment_id"], time.perf_counter())
        except httpx.HTTPError as exc:
            stats.errors[type(exc).__name__] += 1
        stats.latencies_ms.append((time.perf_counter() - t0) * 1000)

    await drive(send, events, args.mode, args.concurrency, args.rate, stats)
    return stats.report()


async def scenario_batch(
    client: httpx.AsyncClient, tok: str, events: list[dict[str, Any]], args: argparse.Namespace
) -> dict[str, Any]:
    stats = Stats()
    h = {"Authorization": f"Bearer {tok}"}
    chunks = [events[k : k + args.batch_size] for k in range(0, len(events), args.batch_size)]

    async def send(chunk: list[dict[str, Any]], t0: float) -> None:
        try:
            r = await client.post(
                "/api/v1/fraud/batch", json={"items": chunk}, headers=h, timeout=300
            )
            stats.status[str(r.status_code)] += 1
            if r.status_code == 200:
                for it in r.json()["items"]:
                    stats.items[it["status"]] += 1
                    if it.get("result") and it["result"]["idempotent_replay"]:
                        stats.replays += 1
        except httpx.HTTPError as exc:
            stats.errors[type(exc).__name__] += 1
        stats.latencies_ms.append((time.perf_counter() - t0) * 1000)

    await drive(send, chunks, args.mode, args.concurrency, args.rate / args.batch_size, stats)
    rep = stats.report("batches")
    rep["events_per_s"] = round(
        sum(stats.items.values()) / max(stats.finished - stats.started, 1e-9), 1
    )
    return rep


async def sse_listener(
    client: httpx.AsyncClient,
    tok: str,
    seen: dict[str, float],
    stop: asyncio.Event,
    ready: asyncio.Event,
    errors: Counter[str],
) -> None:
    h = {"Authorization": f"Bearer {tok}", "Accept": "text/event-stream"}
    try:
        async with client.stream(
            "GET", "/api/v1/stream/assessments", headers=h, timeout=httpx.Timeout(None, connect=10)
        ) as r:
            ready.set()
            data = None
            async for line in r.aiter_lines():
                if stop.is_set():
                    break
                if line.startswith("data: "):
                    data = line[6:]
                elif line == "" and data:
                    try:
                        body = json.loads(data)
                        if "transaction_id" in body:
                            seen.setdefault(body["transaction_id"], time.perf_counter())
                            seen.setdefault("aid:" + body["assessment_id"], time.perf_counter())
                    except ValueError:
                        errors["bad_json"] += 1
                    data = None
    except httpx.HTTPError as exc:
        errors[type(exc).__name__] += 1
        ready.set()


async def scenario_ingest(
    client: httpx.AsyncClient,
    tok: str,
    admin_tok: str,
    viewer_tok: str,
    events: list[dict[str, Any]],
    args: argparse.Namespace,
) -> dict[str, Any]:
    stats = Stats()
    h = {"Authorization": f"Bearer {tok}"}
    sent_at: dict[str, float] = {}
    seen: dict[str, float] = {}
    stop, ready, sse_errors = asyncio.Event(), asyncio.Event(), Counter[str]()
    listener = asyncio.create_task(sse_listener(client, viewer_tok, seen, stop, ready, sse_errors))
    await asyncio.wait_for(ready.wait(), 10)
    chunks = [events[k : k + args.batch_size] for k in range(0, len(events), args.batch_size)]
    backpressure = 0

    async def send(chunk: list[dict[str, Any]], t0: float) -> None:
        nonlocal backpressure
        for _ in range(20):
            try:
                r = await client.post("/api/v1/events", json={"items": chunk}, headers=h)
            except httpx.HTTPError as exc:
                stats.errors[type(exc).__name__] += 1
                return
            stats.status[str(r.status_code)] += 1
            if r.status_code == 503:
                backpressure += 1
                await asyncio.sleep(float(r.headers.get("Retry-After", "1")))
                continue
            if r.status_code == 202:
                now = time.perf_counter()
                for e in chunk:
                    sent_at.setdefault(e["transaction_id"], t0 if args.mode == "steady" else now)
            break
        stats.latencies_ms.append((time.perf_counter() - t0) * 1000)

    await drive(send, chunks, args.mode, args.concurrency, args.rate / args.batch_size, stats)
    ingest = stats.report("requests")
    ah = {"Authorization": f"Bearer {admin_tok}"}
    drain_start = time.perf_counter()
    peak_backlog = 0
    lag_series = []
    unique = {e["transaction_id"] for e in events}
    while True:
        st = (await client.get("/api/v1/stream/status", headers=ah)).json()
        peak_backlog = max(peak_backlog, st["backlog_total"])
        lag_series.append(st["backlog_total"])
        received = sum(1 for t in unique if t in seen)
        if st["backlog_total"] == 0 and st["pending_total"] == 0 and received >= len(unique):
            break
        if time.perf_counter() - drain_start > args.drain_timeout:
            break
        await asyncio.sleep(0.5)
    stop.set()
    listener.cancel()
    e2e = [(seen[t] - sent_at[t]) * 1000 for t in unique if t in seen and t in sent_at]
    total_s = max(seen.values(), default=time.perf_counter()) - stats.started
    return {
        "ingest": ingest,
        "backpressure_503": backpressure,
        "events_unique": len(unique),
        "events_on_feed": sum(1 for t in unique if t in seen),
        "end_to_end_ms": pct(e2e),
        "drain_after_ingest_s": round(time.perf_counter() - drain_start, 2),
        "events_per_s_end_to_end": round(len(e2e) / max(total_s, 1e-9), 1),
        "peak_backlog": peak_backlog,
        "dead_letters": st["dead_letters"],
        "sse_errors": dict(sse_errors),
    }


async def scenario_sse(
    client: httpx.AsyncClient,
    tok: str,
    viewer_tok: str,
    events: list[dict[str, Any]],
    args: argparse.Namespace,
) -> dict[str, Any]:
    seen = [dict[str, float]() for _ in range(args.sse_clients)]
    stop = asyncio.Event()
    readies = [asyncio.Event() for _ in range(args.sse_clients)]
    errors: Counter[str] = Counter()
    tasks = [
        asyncio.create_task(sse_listener(client, viewer_tok, seen[i], stop, readies[i], errors))
        for i in range(args.sse_clients)
    ]
    await asyncio.wait_for(asyncio.gather(*(r.wait() for r in readies)), 30)
    scored_at: dict[str, float] = {}
    load = await scenario_single(
        client, tok, events, args, on_scored=lambda aid, t: scored_at.setdefault(aid, t)
    )
    deadline = time.perf_counter() + 30
    while time.perf_counter() < deadline and any(
        sum(1 for a in scored_at if "aid:" + a in s) < len(scored_at) for s in seen
    ):
        await asyncio.sleep(0.5)
    stop.set()
    for t in tasks:
        t.cancel()
    delivery = [
        (s["aid:" + a] - t) * 1000 for s in seen for a, t in scored_at.items() if "aid:" + a in s
    ]
    per_client = [sum(1 for a in scored_at if "aid:" + a in s) for s in seen]
    return {
        "scoring_load": load,
        "sse_clients": args.sse_clients,
        "assessments": len(scored_at),
        "delivered_per_client": {"min": min(per_client), "max": max(per_client)},
        "delivery_ms": pct(delivery),
        "sse_errors": dict(errors),
    }


def verify_db(db_url: str | None, tag: str, unique: int) -> dict[str, Any] | None:
    if not db_url:
        return None
    if not re.fullmatch(r"[a-z0-9]{1,16}", tag):
        raise ValueError("tag must be 1-16 lowercase letters or digits")
    # The tag is passed as a psql variable (:'tag'), never formatted into the SQL text.
    q = (
        "select count(*), count(distinct t.external_id), count(ra.id), count(distinct ra.id) "
        "from transactions t left join risk_assessments ra on ra.transaction_id = t.id "
        "where t.external_id like 'lt' || :'tag' || '\\_%';"
    )
    out = subprocess.run(
        ["psql", db_url, "-At", "-v", f"tag={tag}"],
        input=q,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    rows, distinct, assessments, distinct_assessments = (int(x) for x in out.split("|"))
    return {
        "unique_events_sent": unique,
        "transactions": rows,
        "distinct_ids": distinct,
        "assessments": assessments,
        "duplicate_rows": rows - distinct,
        "missing": unique - distinct,
        "one_assessment_each": assessments == distinct_assessments == distinct,
    }


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    password = os.environ["FRAUD_LOAD_PASSWORD"]
    tag = args.tag or uuid.uuid4().hex[:8]
    events = load_events(args.events, args.start, tag)
    sendable = with_duplicates(events, args.dup_rate, seed=7)
    limits = httpx.Limits(
        max_connections=args.concurrency + args.sse_clients + 10,
        max_keepalive_connections=args.concurrency + args.sse_clients + 10,
    )
    sampler = Sampler(args.pid or [], args.container or [], args.db_url)
    async with httpx.AsyncClient(base_url=args.api, timeout=60, limits=limits) as client:
        tok = await token(client, args.email, password)
        sampling = asyncio.create_task(sampler.run())
        if args.scenario == "single":
            result = await scenario_single(client, tok, sendable, args)
        elif args.scenario == "batch":
            result = await scenario_batch(client, tok, sendable, args)
        elif args.scenario == "ingest":
            result = await scenario_ingest(
                client,
                tok,
                await token(client, args.admin_email, password),
                await token(client, args.viewer_email, password),
                sendable,
                args,
            )
        else:
            result = await scenario_sse(
                client, tok, await token(client, args.viewer_email, password), sendable, args
            )
        sampler.stop()
        await sampling
    return {
        "scenario": args.scenario,
        "mode": args.mode,
        "concurrency": args.concurrency,
        "rate_per_s": args.rate if args.mode == "steady" else None,
        "events": len(events),
        "dup_rate": args.dup_rate,
        "tag": tag,
        "dataset_window_start": args.start,
        "started_at": datetime.now(UTC).isoformat(),
        "result": result,
        "resources": sampler.report(),
        "database_check": verify_db(args.db_url, tag, len(events)),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--api", required=True)
    p.add_argument("--email", required=True, help="service account (scoring/ingest)")
    p.add_argument("--admin-email", help="admin account (stream status)")
    p.add_argument("--viewer-email", help="viewer account (SSE)")
    p.add_argument("--scenario", choices=["single", "batch", "ingest", "sse"], required=True)
    p.add_argument("--mode", choices=["burst", "steady"], default="burst")
    p.add_argument("--concurrency", type=int, default=16)
    p.add_argument("--rate", type=float, default=100.0, help="steady: requests (events) per s")
    p.add_argument("--events", type=int, default=2000)
    p.add_argument("--batch-size", type=int, default=100)
    p.add_argument("--dup-rate", type=float, default=0.0)
    p.add_argument("--sse-clients", type=int, default=0)
    p.add_argument("--start", default="2020-07-01T00:00:00Z", help="held-out events from here")
    p.add_argument("--drain-timeout", type=float, default=300)
    p.add_argument("--pid", type=int, action="append")
    p.add_argument("--container", action="append")
    p.add_argument("--db-url")
    p.add_argument("--tag")
    p.add_argument("--out", type=Path)
    args = p.parse_args()
    if args.scenario == "ingest" and not (args.admin_email and args.viewer_email):
        sys.exit("ingest needs --admin-email (stream status) and --viewer-email (SSE)")
    if args.scenario == "sse" and (not args.viewer_email or args.sse_clients < 1):
        sys.exit("sse needs --viewer-email and --sse-clients >= 1")
    report = asyncio.run(main_async(args))
    text = json.dumps(report, indent=2, default=str)
    print(text)
    if args.out:
        args.out.write_text(text)


if __name__ == "__main__":
    main()
