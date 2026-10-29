"""Stream operations CLI.

uv run python -m fraud_api.stream.cli init                 # create consumer groups
uv run python -m fraud_api.stream.cli status               # backlog, pending, leases, DLQ
uv run python -m fraud_api.stream.cli dlq-list [--count 50]
uv run python -m fraud_api.stream.cli dlq-replay --ids 1730000000000-0 [--allow-late]
uv run python -m fraud_api.stream.cli dlq-replay --all
uv run python -m fraud_api.stream.cli dlq-delete --ids 1730000000000-0
"""

from __future__ import annotations

import argparse
import json

import redis

from fraud_api.core.config import get_settings
from fraud_api.stream.keys import GROUP, StreamKeys
from fraud_api.stream.ops import dlq_delete, dlq_list, dlq_replay, stream_status


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="fraud_api.stream.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("status")
    q = sub.add_parser("dlq-list")
    q.add_argument("--count", type=int, default=100)
    q = sub.add_parser("dlq-replay")
    g = q.add_mutually_exclusive_group(required=True)
    g.add_argument("--ids", nargs="+")
    g.add_argument("--all", action="store_true")
    q.add_argument(
        "--allow-late",
        action="store_true",
        help="score even if older than the late-arrival tolerance",
    )
    q = sub.add_parser("dlq-delete")
    q.add_argument("--ids", nargs="+", required=True)
    args = p.parse_args(argv)

    settings = get_settings()
    if settings.redis_url is None:
        raise SystemExit("FRAUD_REDIS_URL is not set")
    client = redis.Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    keys = StreamKeys(settings.stream_prefix, settings.stream_shards)
    out: object
    if args.cmd == "init":
        created = 0
        for n in range(keys.shards):
            try:
                client.xgroup_create(keys.shard(n), GROUP, id="0", mkstream=True)
                created += 1
            except redis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
        out = {"shards": keys.shards, "groups_created": created}
    elif args.cmd == "status":
        out = stream_status(client, keys)
    elif args.cmd == "dlq-list":
        out = dlq_list(client, keys, args.count)
    elif args.cmd == "dlq-replay":
        out = dlq_replay(client, keys, None if args.all else args.ids, args.allow_late)
    else:
        out = {"deleted": dlq_delete(client, keys, args.ids)}
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
