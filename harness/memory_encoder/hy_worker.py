"""Isolated Hy v1.0.4 read bridge. stdout is exclusively the JSON protocol."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import subprocess
import sys

REVISION = "688878171e9d98ade3871fecf7eca603c7c43a76"
FIELDS = ("node_id", "user_id", "agent_id", "session_id", "layer", "content", "source",
          "valid_from", "valid_until", "memory_at", "gmt_modified", "is_latest", "status",
          "supersedes", "superseded_by", "speculate", "evidence_chain", "source_type", "custom")


def read_only_environment():
    # Released search otherwise schedules L2/L4 dedup DELETIONS in the background.
    os.environ["MEMORY_SEARCH_DEDUP_ENABLED"] = "false"
    os.environ["MEMORY_RECALL_ACCESS_TRACKING"] = "false"
    os.environ["MEMORY_PIPELINE_TRACE_ENABLED"] = "false"


def harden_client(client):
    # Explicit config values can override env defaults; enforce these again.
    client._config.recall.access_tracking_enabled = False
    client._config.history.record_searches = False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--checkout", required=True)
    args = parser.parse_args()
    checkout = Path(args.checkout).resolve(strict=True)
    head = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"],
                          text=True, capture_output=True, check=True).stdout.strip()
    if head != REVISION:
        raise ValueError("Hy checkout must match the pinned release")
    if subprocess.run(["git", "-C", str(checkout), "diff", "--quiet", "HEAD", "--", "hy_memory"],
                      capture_output=True, check=False).returncode:
        raise ValueError("Hy SDK source has uncommitted changes")
    request = json.load(sys.stdin)
    if request.get("op") not in {"search", "get", "get_many"}:
        raise ValueError("read operations only")
    sys.path.insert(0, str(checkout))
    read_only_environment()
    with redirect_stdout(sys.stderr):
        from hy_memory import HyMemoryClient
        settings = json.loads(Path(args.config).read_text(encoding="utf-8"))
        if settings.get("mode", "pro") != "pro":
            raise ValueError("the read adapter supports pinned Hy pro mode only")
        sdk_config = settings["sdk_config"]
        sdk_config.setdefault("coding", {})["enable"] = False
        client = HyMemoryClient.from_config(sdk_config, mode="pro")
        harden_client(client)
        try:
            def get_record(identifier):
                public = client.get(identifier)
                if not public:
                    return None
                # Public get() omits session and validity. The pinned SDK stores
                # these on MemoryNode; hydrate them before admitting any content.
                async def hydrate():
                    if public["source"] == "vdb":
                        return await client._vector_store.get_by_id(identifier)
                    return await client._graph_store.get_node(identifier)
                node = client._loop_thread.run(hydrate())
                if node is None:
                    return None
                raw = node.to_dict()
                return {field: raw.get(field) for field in FIELDS}

            if request["op"] == "get":
                result = get_record(request["record_id"])
            elif request["op"] == "get_many":
                if not isinstance(request["record_ids"], list) or len(request["record_ids"]) > 100:
                    raise ValueError("invalid validation batch")
                result = [get_record(identifier) for identifier in request["record_ids"]]
            else:
                if not request["user_ids"] or not request["agent_ids"]:
                    raise ValueError("unscoped search forbidden")
                response = client.search(request["query"], scene="chat", reader="legacy",
                    user_ids=request["user_ids"], agent_ids=request["agent_ids"],
                    session_ids=request["session_ids"], limit=request["limit"],
                    schema_limit=request["limit"], intention_limit=0)
                memories = response.get("memories", {})
                groups = memories.values() if isinstance(memories, dict) else [memories]
                identifiers = []
                for group in groups:
                    if not isinstance(group, list):
                        continue
                    for item in group:
                        identifier = item.get("memory_id") or item.get("node_id")
                        if identifier and identifier not in identifiers:
                            identifiers.append(identifier)
                result = [row for identifier in identifiers[:request["limit"] * 3]
                          if (row := get_record(identifier)) is not None]
        finally:
            from hy_memory.utils.pipeline_observability import drain_pipeline_log_tasks
            client._search_loop_thread.run(drain_pipeline_log_tasks(timeout=15))
            client._loop_thread.run(drain_pipeline_log_tasks(timeout=15))
            client.close()
            client._search_loop_thread.stop()
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
