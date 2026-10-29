"""Stream building blocks that need neither Redis nor PostgreSQL."""

from __future__ import annotations

import itertools
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from fraud_api.scoring.schemas import RulesSummary, ScoreRequest, ScoreResponse
from fraud_api.stream.feed import FEED_FIELDS, feed_payload
from fraud_api.stream.keys import StreamKeys
from fraud_api.stream.messages import (
    InvalidPayload,
    MalformedMessage,
    make_envelope,
    parse_envelope,
)
from fraud_api.stream.simulator import (
    Control,
    SimConfig,
    Simulator,
    due_offsets,
    simulated_events,
)
from fraud_api.stream.worker import parse_shards

ORG = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
T0 = datetime(2020, 3, 15, 12, tzinfo=UTC)


def request(**overrides: Any) -> ScoreRequest:
    body = {
        "transaction_id": "txn_1",
        "customer_id": "cus_00000000000000aa",
        "merchant_id": "Secret Merchant",
        "amount": "4321.99",
        "currency": "USD",
        "category": "travel",
        "event_time": T0.isoformat(),
        "region": "NY",
        "source": "replay",
    }
    body.update(overrides)
    return ScoreRequest.model_validate(body)


# ---------------------------------------------------------------- sharding


def test_shards_are_deterministic_tenant_scoped_and_spread() -> None:
    keys = StreamKeys("p", 8)
    a = keys.shard_for(ORG, "cus_1")
    assert all(keys.shard_for(ORG, "cus_1") == a for _ in range(10))
    other = uuid.UUID("00000000-0000-0000-0000-0000000000cc")
    tally = [0] * 8
    for i in range(4000):
        tally[keys.shard_for(ORG, f"cus_{i}")] += 1
    assert min(tally) > 350  # roughly uniform (expected 500)
    assert any(
        keys.shard_for(ORG, f"cus_{i}") != keys.shard_for(other, f"cus_{i}") for i in range(20)
    )


def test_parse_shards() -> None:
    assert parse_shards("all", 4) == [0, 1, 2, 3]
    assert parse_shards("0-1,3", 4) == [0, 1, 3]
    with pytest.raises(ValueError):
        parse_shards("2-5", 4)


# ---------------------------------------------------------------- envelope


def test_envelope_round_trip_keeps_tenant_and_label() -> None:
    env = make_envelope(ORG, ACTOR, "cid-123", request())
    parsed = parse_envelope(env.fields())
    assert parsed.organization_id == ORG and parsed.actor_id == ACTOR
    assert parsed.request() == request()
    assert parsed.payload["source"] == "replay"
    assert parsed.allow_late is False and parsed.dlq_replay_of is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda f: f.pop("org"),
        lambda f: f.update(v="9"),
        lambda f: f.update(payload="[1, 2]"),
        lambda f: f.update(payload="{not json 4321.99"),
        lambda f: f.update(org="not-a-uuid-4321.99"),
    ],
)
def test_malformed_envelopes_never_echo_values(mutate: Any) -> None:
    fields = make_envelope(ORG, ACTOR, "cid", request()).fields()
    mutate(fields)
    with pytest.raises(MalformedMessage) as exc:
        parse_envelope(fields)
    assert "4321.99" not in str(exc.value)


def test_invalid_payload_reports_locations_not_values() -> None:
    fields = make_envelope(ORG, ACTOR, "cid", request()).fields()
    payload = json.loads(fields["payload"])
    payload["category"] = "crypto-secret"
    fields["payload"] = json.dumps(payload)
    with pytest.raises(InvalidPayload) as exc:
        parse_envelope(fields).request()
    assert "category" in str(exc.value) and "crypto-secret" not in str(exc.value)


# ---------------------------------------------------------------- feed


def test_feed_payload_is_an_allowlist_without_sensitive_fields() -> None:
    response = ScoreResponse(
        assessment_id=uuid.uuid4(),
        transaction_id="txn_1",
        status="scored",
        risk_score=0.3,
        risk_level="high",
        decision="review",
        policy_decision="review",
        rules=RulesSummary(status="none", evaluated=0, matched=[], errors=[], decided_by=None),
        decision_mode="sandbox",
        reasons=["x"],
        model_name="m",
        model_version="1",
        policy_version="p",
        source="replay",
        processing_time_ms=1.0,
        scored_at=T0,
        idempotent_replay=False,
    )
    payload = feed_payload(response, "stream", T0, T0, late_arrival=False)
    assert set(FEED_FIELDS) <= set(payload)
    text = json.dumps(payload)
    for forbidden in ("customer_id", "amount", "merchant", "region", "features", "4321"):
        assert forbidden not in text
    assert payload["path"] == "stream" and payload["late_arrival"] is False
    assert payload["policy_decision"] == "review" and "rules" not in payload


# ---------------------------------------------------------------- simulator


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def test_simulated_events_are_deterministic_and_labeled() -> None:
    a = simulated_events(200, 20, seed=7, start=T0)
    b = simulated_events(200, 20, seed=7, start=T0)
    c = simulated_events(200, 20, seed=8, start=T0)
    assert a == b and a != c
    assert {e["source"] for e in a} == {"simulated"}
    times = [e["event_time"] for e in a]
    assert times == sorted(times)
    for e in a[:20]:
        ScoreRequest.model_validate(e)  # valid against the ingest contract
        assert Decimal(e["amount"]) > 0


def _run(
    events: list[dict[str, Any]], cfg: SimConfig, control: Control | None = None
) -> tuple[dict[str, Any], list[tuple[float, int]], FakeClock]:
    clock = FakeClock()
    sent: list[tuple[float, int]] = []

    def send(items: list[dict[str, Any]]) -> dict[str, Any]:
        sent.append((clock.t, len(items)))
        return {"accepted": len(items), "rejected": 0}

    report = Simulator(events, cfg, send, clock=clock.now, sleep=clock.sleep, control=control).run()
    return report, sent, clock


def test_rate_limit_pacing_and_chunking() -> None:
    events = simulated_events(100, 10, seed=1, start=T0)
    report, sent, clock = _run(events, SimConfig(rate=50, chunk=10))
    assert report["accepted"] == 100 and report["events"] == 100
    assert clock.t == pytest.approx(99 / 50, rel=1e-6)  # last event due at 99/rate seconds
    assert all(n <= 10 for _, n in sent)
    for _i, (t, _) in enumerate(sent):  # never ahead of schedule
        assert t <= clock.t


def test_speed_follows_event_time_gaps() -> None:
    events = simulated_events(30, 5, seed=2, start=T0)
    span = (datetime.fromisoformat(events[-1]["event_time"]) - T0).total_seconds() - (
        datetime.fromisoformat(events[0]["event_time"]) - T0
    ).total_seconds()
    _, _, clock = _run(events, SimConfig(speed=60.0, chunk=1))
    assert clock.t == pytest.approx(span / 60.0, rel=1e-6)
    offsets = due_offsets(events, SimConfig(speed=60.0))
    assert offsets == sorted(offsets) and offsets[0] == 0.0


def test_same_inputs_give_the_same_sequence_digest() -> None:
    events = simulated_events(50, 5, seed=3, start=T0)
    r1, _, _ = _run(events, SimConfig(rate=0, chunk=7))
    r2, _, _ = _run(events, SimConfig(rate=100, chunk=3))
    assert r1["sequence_sha256"] == r2["sequence_sha256"]  # order identical, pacing irrelevant
    r3, _, _ = _run(simulated_events(50, 5, seed=4, start=T0), SimConfig(chunk=7))
    assert r3["sequence_sha256"] != r1["sequence_sha256"]


def test_pause_and_resume_send_nothing_while_paused() -> None:
    events = simulated_events(20, 4, seed=5, start=T0)

    class ScriptedControl(Control):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def is_paused(self) -> bool:  # paused for 30 checks starting at the 6th event
            self.calls += 1
            return 6 <= self.calls < 36

    report, sent, clock = _run(events, SimConfig(rate=10, chunk=1), ScriptedControl())
    assert report["accepted"] == 20
    assert report["paused_seconds"] == pytest.approx(3.0, abs=1e-6)  # 30 checks x 0.1 s
    assert clock.t == pytest.approx(19 / 10 + 3.0, abs=1e-6)  # pause excluded from schedule
    gaps = [b[0] - a[0] for a, b in itertools.pairwise(sent)]
    assert max(gaps) == pytest.approx(3.1, abs=1e-6)  # the paused stretch had no sends


def test_control_file(tmp_path: Any) -> None:
    f = tmp_path / "control"
    c = Control(f)
    assert c.is_paused() is False
    f.write_text("pause\n")
    assert c.is_paused() is True
    f.write_text("run")
    assert c.is_paused() is False


def test_due_offsets_with_both_rate_and_speed() -> None:
    events = [
        {
            "event_time": (T0 + timedelta(seconds=s)).isoformat(),
            "transaction_id": str(i),
            "source": "simulated",
        }
        for i, s in enumerate([0, 1, 100])
    ]
    assert due_offsets(events, SimConfig(rate=1.0, speed=10.0)) == [0.0, 1.0, 10.0]
