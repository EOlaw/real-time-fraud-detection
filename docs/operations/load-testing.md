# Concurrent load testing (Phase 9)

Tool: `scripts/load_test.py` (asyncio + httpx). This is a **concurrent** load test: many clients
in flight at once. The Phase 1 sequential replay (`scripts/replay_score.py`, one client) measures
latency, not capacity, and is **not** a load test.

> **These are synthetic-data, single-laptop numbers.** They show how this code behaves and where
> it saturates on one machine. They are **not** production capacity, latency or SLO figures. See
> [Synthetic vs production](#synthetic-vs-production).

## Method

| | |
|---|---|
| `--mode burst` | Closed loop: `--concurrency` clients send back to back. Measures maximum throughput and saturation. |
| `--mode steady` | Open loop: requests are **scheduled** at `--rate`/s, and latency is measured **from the scheduled time**, so queueing under overload counts (no coordinated omission). `--concurrency` caps in-flight requests. |
| Events | Real held-out events from `--start` (default `2020-07-01`), each with a run-unique transaction id `lt<tag>_<event_id>` and `source=simulated` |
| Duplicates | `--dup-rate` re-sends that share of events with an identical payload, concurrently with the original |
| Database check | After the run, via `psql`, counting only that run's tag: exactly one transaction and one assessment per unique id, no duplicate rows, nothing missing |
| Resources | Sampled every second in a background thread (so the sampler cannot delay the event loop): process CPU/RSS (`--pid`), container CPU (`--container`), PostgreSQL commits, rollbacks, deadlocks and connections (`--db-url`) |

**Scenarios:**
- `single`: `POST /fraud/score`.
- `batch`: `POST /fraud/batch`.
- `ingest`: `POST /events`, then wait for the workers to drain. End-to-end latency runs from send
  until the assessment appears on a listening SSE feed. Peak backlog and dead letters are read
  from `/stream/status`.
- `sse`: N SSE clients connected while `single` runs in steady mode. Delivery is measured per
  client and per event.

```bash
FRAUD_LOAD_PASSWORD=... make load-test ARGS="--api http://127.0.0.1:8000 \
  --email service@load.example.com --admin-email admin@load.example.com \
  --viewer-email viewer@load.example.com --scenario single --mode steady --rate 150 \
  --concurrency 64 --events 9000 --dup-rate 0.05 --db-url postgresql://... \
  --pid <api pid> --container real-time-fraud-detection-postgres-1 --out report.json"
```

**Run it only against a throwaway database and Redis DB.** It writes real transactions and
assessments (for its own tenant only; see [Safety](#safety)).

## Environment (2026-10-28)

| | |
|---|---|
| Host | Apple M4 Pro, 14 cores, 24 GB, macOS 26.6.1. Client, API, workers and Docker share it. |
| Docker | 29.4.3, 14 CPUs / 8 GB. PostgreSQL 17.6 and Redis 7.4.2 containers, default configs. |
| API | **One** uvicorn process (no `--workers`), Python 3.12.13, `OMP_NUM_THREADS=1`, SQLAlchemy pool 10 (+ default overflow 10) |
| Workers | 2 processes: w1 for shards 0–3, w2 for shards 4–7 |
| Data | Throwaway database `fraud_phase9_check` and Redis DB 11 (dropped afterwards). The load tenant `load9` had 30 days of history (83,971 transactions) imported before 2020-07-01, so features see realistic lookbacks. |
| Model | `card-fraud-xgb@1.0.0` with its bound policy |

## Results

Every run: **0 deadlocks, 0 duplicate rows, 0 missing, exactly one assessment per unique id**.
Latency is client-side, in milliseconds.

| Run | Load | Throughput | p50 | p95 | p99 | Errors | Resources (mean) |
|---|---|---|---|---|---|---|---|
| single, burst | 32 clients, 6,000 events + 5% dups (6,300 req) | **260 req/s** | 118 | 162 | 190 | 0 | API 122% CPU, PG 40%, 23 connections |
| single, steady | 150/s scheduled, 9,450 req | 150 req/s | **8.7** | **11.8** | **17.0** | 0 | API 48%, PG 19% |
| single, overload | 400/s scheduled, 9,450 req | saturates at **240 req/s** | 9,044 | 15,165 | 15,747 | 0 | API 116%, PG 42% (max 108%) |
| batch, burst | 8 clients × 100 events, 12,000 + 2% dups | **454 events/s** | 1,757 per batch | 1,812 | 1,825 | 0 | API 129%, PG 35% |
| batch, steady | 120 events/s (100/batch) | 120 events/s | 377 per batch | 594 | 631 | 0 | API 48%, PG 14% |
| ingest, burst | 20,000 events + 2% dups queued in 0.7 s | **215 events/s** end to end (2 workers) | 45,250 e2e | 86,183 | 90,471 | 0 backpressure, 0 DLQ | workers 39–41% each, PG 33%; peak backlog 20,312; drain 92.5 s |
| ingest, steady | 150 events/s | 150 events/s | **34** e2e | **62** | **72** | 0 | workers 28% each; backlog ≤ 10 |
| SSE fan-out | 50 clients + 50 scores/s steady for 60 s | 150,000 deliveries, every client got all 3,000 | −3.2 | −0.7 | 3.8 (max 65) | 0 | API 59% |
| crash recovery | 10,000 ingest; worker w1 `kill -9` at 8 s, restarted 5 s later | 164 events/s e2e | 30,291 e2e | 55,843 | 59,608 | 0 DLQ | all 10,000 on the feed, backlog 0 |

**Reading the table:**
- **Overload:** p50 9 s and p99 16 s are **queueing time from the scheduled send**. Requests
  waited for the single process; none failed. The DB pool grows to its overflow limit.
- **Stream ingest burst:** end-to-end latency is mostly waiting in the stream. Workers drain at
  about 215 events/s together (each process scores sequentially). At a steady 150/s the backlog
  stays at about 10 and e2e p99 is 72 ms.
- **SSE delivery** is the time from the scoring client reading its HTTP response to the SSE
  client receiving the event. It is **negative** when the event arrives before the response is
  read (the feed is published right after commit). It bounds the fan-out delay; it is not a
  publish-to-receive time.
- **Rollbacks** in the PostgreSQL counters are sessions that end without a write (SQLAlchemy
  rolls back on close; for example, auth and history reads). No deadlocks occurred, and the
  database check found no failed or partial writes.

### XGBoost threads

At 32 concurrent clients, `OMP_NUM_THREADS=1` against the default (all cores):

| | default OMP | OMP=1 |
|---|---|---|
| Throughput | 227 req/s | **260 req/s** |
| p50 / p99 | 135 / 218 ms | 118 / 190 ms |
| API CPU | 170% | 122% |

Each prediction is one row, so XGBoost's thread pool only adds contention between request threads.
Predictions are identical. The Docker image now sets `OMP_NUM_THREADS=1`.

### Database overload behaviour

An earlier overload run, before the fix, produced 38 HTTP **500** errors from connection-pool
timeouts. Pool and operational errors now return **503 `database_unavailable`** with
`Retry-After: 2` and count `fraud_db_errors_total`. A rerun with a deliberately small pool gave
20 × 503 and 20 missing assessments, which is expected: those requests were refused, not
half-written. `test_database_overload_is_503_with_retry_after_and_counted` covers it.

### Invalidated runs

The first set of runs is **invalid and not reported** above. In those runs, the resource sampler
called `docker stats` (1–2 s) on the event loop, which delayed every client and inflated p95 to
about 2 s. The sampler now runs in a thread, and all runs were redone. Raw reports for the final
runs are kept outside the repo.

## Saturation and bottlenecks

- **The single API process is the ceiling:** 240–260 scores/s at about 120–140% CPU (event loop
  plus thread pool for synchronous DB and model work). PostgreSQL was below 45% mean and Redis
  below 5%.
- **Scaling:** more API processes or containers would scale this. The Docker image runs
  `--workers 2`, but this was not measured, and its metrics are per process (see
  [monitoring.md](monitoring.md#limitations)).
- **Stream workers:** about 107 events/s per worker process at about 40% CPU. Scoring per message
  is sequential and DB-round-trip bound; more workers or shards scale it.
- **Batch amortizes HTTP:** about 1.75× single-event throughput.

## Unresolved: one SSE stall

The first 50-client SSE run **stalled**:
- **Symptom:** every client received the first 54 events, then nothing more.
- **API log:** 50 `redis.exceptions.TimeoutError: Timeout reading from localhost:6380` at the
  same instant (16:26:20Z).
- **Scoring side:** 2,985 of 3,000 scoring requests succeeded. The other 15 were not stored; their
  client-side error was not captured.

**Not found:**
- Redis `SLOWLOG` had no command over 25 ms.
- No host sleep was recorded at that time.

An identical rerun was clean: all 50 clients received all 3,000 events. The root cause is **not
confirmed**.

**What the clients saw:** the feed's Redis read timed out and the connections ended. Clients that
reconnect with `Last-Event-ID` resume, but the load tool does not reconnect, so delivery stopped.

**Treat this as an open risk** for SSE under concurrent Redis reads.

## Lease losses at idle

During idle periods the workers occasionally reported `lease_lost` (4 at a time) and recovered.
The host logged 2,203 sleep/wake cycles since boot (macOS Maintenance Sleep). The likeliest cause
is a paused process missing its lease renewal. The design handles it: another worker reclaims the
messages and idempotency prevents double effects. It was not seen during active load runs.

## Safety

The load tool authenticates as users of **its own tenant**. A test
(`test_load_tool_only_adds_its_own_tenant_data`) proves that a run against a database containing
another tenant leaves that tenant's data unchanged:
- transactions, assessments and scores;
- decisions, cases and labels;
- model registry state and the policy file.

The tool never promotes or changes models or rules.

## Synthetic vs production

- **Data:** the events are synthetic (Sparkov) and **reused**. Re-sending held-out events with new
  ids after their originals means some arrive "late" relative to customer history, and
  `late_arrival` counts rise. Feature values follow synthetic distributions.
- **Shared host:** client, API, workers, PostgreSQL and Redis share one laptop with no network
  hops, TLS or load balancer. Production adds network latency and isolates resources differently.
- **Database size:** PostgreSQL held under 200k transactions for the load tenant. A real tenant's history
  tables and indexes would be far larger, so history-query time will differ.
- **No production SLO is implied.** The useful findings are relative:
  - where it saturates (one process);
  - that idempotency, duplicate handling and crash recovery hold under concurrency;
  - that overload degrades to queueing or 503, not to errors or duplicate effects.
