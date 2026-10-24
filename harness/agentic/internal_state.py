"""Read-only, authenticated Runtime observations for native cognition."""
from __future__ import annotations

from copy import deepcopy
import os

import httpx

from harness.agentic.context import local_endpoint
from harness.agentic.store import canonical


def runtime_endpoint():
    url = os.environ.get("NC_RUNTIME_URL", "http://127.0.0.1:8770").rstrip("/")
    if not local_endpoint(url):
        raise ValueError("agentic_runtime_requires_local_endpoint")
    token = os.environ.get("NC_SERVICE_TOKEN", "")
    if not token:
        raise ValueError("agentic_runtime_authentication_unavailable")
    return url, {"Authorization": "Bearer " + token}


def read_runtime_state():
    try:
        url, headers = runtime_endpoint()
        # Local snapshot reads never wait for a model or a remote body provider.
        with httpx.Client(timeout=.2) as client:
            response = client.get(url + "/v1/internal/agentic/state", headers=headers)
            response.raise_for_status()
            result = response.json()
        if result.get("schema_version") != "native-internal-state-v1":
            raise ValueError("invalid_internal_state")
        return result
    except (ValueError, httpx.HTTPError):
        return {"mode": "unknown", "running": False, "body": {"status": "unavailable"}}


class TurnState:
    def __init__(self, service, conversation, runtime):
        self.service, self.conversation = service, conversation
        self.runtime = runtime
        self.frozen = self.read()

    def read(self, *, causes=False, subject_id=None):
        affect = self.service.affect.snapshot(self.conversation, causes=causes, subject_id=subject_id)
        return {"type": "internal_state", "body": self.runtime.get("body", {"status": "unknown"}),
            "rest": self.runtime.get("rest", {"resting": False}), "affect": affect,
            "note": "这是功能状态与可修正的感受，不是人格指令或外界事实。用自然语言表达，不主动解释内部字段或量表。未知项不作推断；没有可披露原因不等于没有原因，私密原因不得跨受众透露。"}

    def compact(self, counter):
        value = deepcopy(self.frozen)
        value['affect']['objects'] = [{k:v for k,v in obj.items() if k != 'relationship'} for obj in value['affect']['objects']]
        limit = self.service.settings.agentic_regulation.state_tokens
        if counter.text(canonical(value)) > limit:
            value['affect']['objects'] = []
        if counter.text(canonical(value)) > limit:
            value['body'].pop('psychological', None)
        if counter.text(canonical(value)) > limit:
            value.pop('note', None)
        if counter.text(canonical(value)) > limit:
            raise ValueError("internal_state_exceeds_budget")
        return value

    def propose(self, proposal, memory):
        memory.validate()
        return self.service.affect.submit(self.conversation, proposal, shown=memory.shown)
