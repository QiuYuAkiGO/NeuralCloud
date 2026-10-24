"""Observe group identifiers for operator-confirmed onboarding; never auto-grant a group."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from harness.qq.official_client import OfficialQQClient  # noqa: E402
from harness.qq.official_identity import identity  # noqa: E402


async def observe(args, output):
    count = 0

    async def record(item):
        nonlocal count
        if count >= 100:
            return
        raw_group = item.get("data", {}).get("group_openid")
        if not raw_group:
            return
        group = identity(args.app_id, "group", raw_group)
        kind = item.get("event_type")
        if kind not in {
            "GROUP_ADD_ROBOT",
            "GROUP_DEL_ROBOT",
            "GROUP_MSG_REJECT",
            "GROUP_MSG_RECEIVE",
            "GROUP_MESSAGE_CREATE",
            "GROUP_AT_MESSAGE_CREATE",
        }:
            return
        # Store only metadata, never chat body, names, image URLs, or credentials.
        output.write(
            json.dumps(
                {
                    "app_id": args.app_id,
                    "group_id": group,
                    "event_type": kind,
                    "received_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            + "\n"
        )
        output.flush()
        count += 1
        print(json.dumps({"metadata_event": kind, "count": count}), flush=True)

    client = OfficialQQClient(
        args.credentials, app_id=args.app_id, send_groups=set(), on_event=record, on_lifecycle=record
    )
    try:
        await client.open()
        print(json.dumps({"ready": True, "send_allowlist_count": 0, "model_calls": 0}), flush=True)
        client.enabled.set()
        try:
            await asyncio.wait_for(client.closed.wait(), timeout=args.seconds)
        except TimeoutError:
            pass
    finally:
        await client.close()
    print(json.dumps({"completed": True, "metadata_events": count, "send_calls": 0, "model_calls": 0}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, choices=range(1, 601), default=600)
    args = parser.parse_args()
    with args.output.open("x", encoding="utf-8") as output:
        try:
            asyncio.run(observe(args, output))
        except Exception as error:
            print(json.dumps({"error_type": type(error).__name__, "send_calls": 0, "model_calls": 0}), flush=True)
            sys.exit(1)
