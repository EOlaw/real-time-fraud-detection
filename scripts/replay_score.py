"""Replay historical events through the scoring API (clearly labeled ``source=replay``).

This is a demonstration/verification harness, not the Phase 5 streaming simulator. Events are sent
sequentially in event-time order so each one sees the previously replayed history. Labels are read
locally for reporting only; they are never sent to the API.

    uv run python scripts/replay_score.py --email svc@demo.example.com --limit 20000 \
        --start 2020-06-21T12:14:00Z --verify-every 100

With ``--verify-every N``, every Nth response's stored features and score are compared against the
offline pipeline computed over the full dataset (training/serving skew check).
"""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import platform
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd

from fraud_ml.features.offline import compute_offline_features
from fraud_ml.features.spec import NUMERIC_FEATURES
from fraud_ml.models.artifact import load_artifact
from fraud_ml.training.train import build_matrix


def percentile(values: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(values), q))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--api", default="http://127.0.0.1:8000")
    p.add_argument("--email", required=True)
    p.add_argument("--data", type=Path, default=Path("ml/data/processed/card_transactions.parquet"))
    p.add_argument("--model-dir", type=Path, default=Path("ml/artifacts/current"))
    p.add_argument("--start", type=datetime.fromisoformat, required=True)
    p.add_argument("--limit", type=int, default=5000)
    p.add_argument("--verify-every", type=int, default=0)
    p.add_argument("--out", type=Path, default=Path("ml/reports"))
    args = p.parse_args()

    password = os.environ.get("FRAUD_REPLAY_PASSWORD") or getpass.getpass("Password: ")
    events = pd.read_parquet(args.data)
    window = events[events["event_time"] >= args.start].head(args.limit)

    verify_idx: set[int] = set()
    offline: pd.DataFrame | None = None
    offline_scores: np.ndarray | None = None
    if args.verify_every:
        verify_idx = set(window.index[:: args.verify_every])
        offline = compute_offline_features(events).loc[sorted(verify_idx)]
        offline_scores = load_artifact(args.model_dir).predict_proba(build_matrix(offline))

    client = httpx.Client(base_url=args.api, timeout=10.0)
    token = client.post("/api/v1/auth/token", json={"email": args.email, "password": password})
    token.raise_for_status()
    client.headers["Authorization"] = f"Bearer {token.json()['access_token']}"

    rows: list[dict[str, Any]] = []
    parity: list[dict[str, Any]] = []
    started = time.perf_counter()
    for idx, e in window.iterrows():
        body = {
            "transaction_id": e["event_id"],
            "customer_id": e["customer_id"],
            "merchant_id": e["merchant_id"],
            "amount": f"{e['amount']:.2f}",
            "currency": e["currency"],
            "category": e["category"],
            "event_time": e["event_time"].isoformat(),
            "region": e["customer_region"],
            "source": "replay",
        }
        t0 = time.perf_counter()
        r = client.post("/api/v1/fraud/score", json=body)
        client_ms = (time.perf_counter() - t0) * 1000
        r.raise_for_status()
        res = r.json()
        rows.append({"label": int(e["is_fraud"]), "client_ms": client_ms, **res})
        if idx in verify_idx and offline is not None and offline_scores is not None:
            detail = client.get(f"/api/v1/fraud/predictions/{res['assessment_id']}").json()
            pos = offline.index.get_loc(idx)
            max_diff = 0.0
            for name in NUMERIC_FEATURES:
                a, b = offline.loc[idx, name], detail["features"][name]
                if b is None or (isinstance(a, float) and math.isnan(a)):
                    assert b is None and math.isnan(a), (idx, name, a, b)
                    continue
                max_diff = max(max_diff, abs(float(a) - float(b)) / max(1.0, abs(float(a))))
            parity.append(
                {
                    "event_id": e["event_id"],
                    "max_rel_feature_diff": max_diff,
                    "score_diff": abs(float(offline_scores[pos]) - res["risk_score"]),
                }
            )
    elapsed = time.perf_counter() - started

    df = pd.DataFrame(rows)
    scored = df[df["status"] == "scored"]
    flagged = scored["decision"].isin(["review", "decline"])
    tp = int((flagged & (scored["label"] == 1)).sum())
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "traffic": "historical replay of the held-out test window (source=replay); not live",
        "window_start": args.start.isoformat(),
        "events": len(df),
        "degraded": int((df["status"] == "degraded").sum()),
        "decisions": df["decision"].value_counts().to_dict(),
        "risk_levels": df["risk_level"].value_counts().to_dict(),
        "labels_in_window": int(df["label"].sum()),
        "review_or_decline_precision": tp / max(int(flagged.sum()), 1),
        "review_or_decline_recall": tp / max(int(scored["label"].sum()), 1),
        "note": "Replay starts after a 30-day backfill; window is a prefix of the test period, "
        "so these figures are a smoke check, not the model evaluation.",
        "latency_ms": {
            "client_round_trip": {
                q: percentile(df["client_ms"].tolist(), v)
                for q, v in (("p50", 50), ("p95", 95), ("p99", 99))
            },
            "server_processing": {
                q: percentile(df["processing_time_ms"].tolist(), v)
                for q, v in (("p50", 50), ("p95", 95), ("p99", 99))
            },
            "mean_client": statistics.fmean(df["client_ms"]),
        },
        "throughput_rps_sequential_single_client": len(df) / elapsed,
        "parity_checks": {
            "checked": len(parity),
            "max_rel_feature_diff": max((x["max_rel_feature_diff"] for x in parity), default=None),
            "max_score_diff": max((x["score_diff"] for x in parity), default=None),
        },
        "environment": {
            "machine": platform.machine(),
            "system": platform.system(),
            "release": platform.release(),
            "python": platform.python_version(),
            "setup": "uvicorn 1 worker + PostgreSQL 17 (Docker) on the same host; "
            "sequential requests, 1 client, keep-alive",
        },
    }
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"replay_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"report: {path}")


if __name__ == "__main__":
    main()
