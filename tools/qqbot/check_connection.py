"""Bounded official gateway check; no sends, model calls, or message content output."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from harness.qq.official_client import OfficialQQClient  # noqa: E402


async def check(path, app_id, seconds):
    counts = {"group_events": 0, "lifecycle_events": 0}

    async def incoming(_):
        counts["group_events"] += 1

    async def lifecycle(_):
        counts["lifecycle_events"] += 1

    # This check requires an empty send allowlist, even when a real credential
    # is supplied. It cannot consume production inboxes or prepare replies.
    client = OfficialQQClient(path, app_id=app_id, send_groups=set(), on_event=incoming, on_lifecycle=lifecycle)
    result = {
        "app_id": app_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "mode": "gateway_read_only",
        "model_calls": 0,
        "send_calls": 0,
        "ready": False,
    }
    try:
        await client.open()
        result["ready"] = client.connected
        client.enabled.set()
        if seconds:
            try:
                await asyncio.wait_for(client.closed.wait(), timeout=seconds)
            except TimeoutError:
                pass
        result["connected_at_end"] = client.connected
        result["last_error_type"] = client.last_error_type
    except Exception as error:
        result["error_type"] = type(error).__name__
        result["last_error_type"] = client.last_error_type
    finally:
        await client.close()
    return result | counts | {"completed_at": datetime.now(timezone.utc).isoformat()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", required=True, type=Path)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--seconds", type=int, default=0, choices=range(0, 41))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    # Reserve the output before any authentication, never overwrite evidence.
    with args.output.open("x", encoding="utf-8") as target:
        result = asyncio.run(check(args.credentials, args.app_id, args.seconds))
        json.dump(result, target, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ready"] and result.get("connected_at_end") else 1


if __name__ == "__main__":
    raise SystemExit(main())
