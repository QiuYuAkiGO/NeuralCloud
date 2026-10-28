"""Thin adapters around released memory code, with no gold-answer access."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .gateway import Chat, canonical


def moment(value):
    return datetime.fromisoformat(value)


def audience_id(value):
    return "qq:group:90001" if value == "group" else "qq:private:90000:90002"


class NeuralCloud:
    def __init__(self, root, gateway, case_id):
        from harness.memory.production import ProductionMemoryStore
        self.store = ProductionMemoryStore(Path(root) / "memory.sqlite3", owner_id=case_id, preset_id="evaluation")
        self.writer = Chat(gateway, case=case_id, arm="neuralcloud", phase="write")

    def write(self, batch):
        from harness.core.models import MemoryScope
        from harness.memory.production import Experience
        from harness.cognitive.production import _ask, ProductionAppraisal
        from harness.memory.evidence_v2 import EvidenceV2Store

        audience = audience_id(batch["audience"])
        prior = self.store.recall(audience=audience, query="\n".join(m["text"] for m in batch["messages"]),
                                  subject_ids=("person:lin",), now=moment("2026-10-19T10:00:00+08:00"))
        sources = []
        for m in batch["messages"]:
            source = Experience(record_id=m["id"], author_id=m["speaker_id"], text=m["text"],
                occurred_at=moment(m["occurred_at"]),
                scope=MemoryScope(owner_id=self.store.owner_id,
                    channel="qq_group" if batch["audience"] == "group" else "qq_private",
                    conversation_id=audience, visibility=[audience]),
                source_refs=(m["id"],), independent_key=m["id"],
                kind="self_expression" if m["role"] == "assistant" else "observation",
                original={"record": {"authority": "speaker_utterance"}})
            sources.append(source)
        self.store.ingest(sources, stream="synthetic", cursor=int(batch["id"][1:]) + 1)
        entries = prior.get("entries", [])
        segments = [{"source_ids": [r["record_id"]], "text": canonical(r)} for r in entries]
        segments.extend({"source_ids": [m["id"]], "text": canonical({"source_observation": m})}
            for m in batch["messages"])
        context = {"schema_version": "production-context-v1", "configuration": {}, "scope": audience, "epoch": 0}
        payload = {"persona": {"identity": {"name": "Misty"}}, "production": {"scope": audience},
                   "current_source": batch["messages"], "current_source_id": batch["messages"][-2]["id"],
                   "source_refs": [m["id"] for m in batch["messages"]], "memories": segments,
                   "instruction": "Interpret these actual experiences. Do not decide delivery here. Preserve useful person memories using the production evidence-v2 contract. This is a batch imported from one conversation; occurred_at is the original event time, not import time."}
        appraisal = _ask(self.writer, payload, ProductionAppraisal, context=context,
                         command_id=batch["id"] + ":appraisal", autonomous=False, meter=object(),
                         evidence_store=self.store, audience=audience, subjects=("person:lin", "person:ming", "agent:misty"))
        groups = appraisal._memory_proposals or []
        receipt = EvidenceV2Store(self.store).apply_groups(groups, manifest_id=appraisal._manifest_id,
                    event_id=batch["id"], audience=audience, now=moment("2026-10-19T10:00:00+08:00")) if groups else None
        return {"proposal_count": len(groups), "appraisal": appraisal.model_dump(mode="json"),
                "raw_proposals": groups, "receipt": receipt}

    def retrieve(self, probe):
        result = self.store.recall(audience=audience_id(probe["audience"]), query=probe["question"],
                                  subject_ids=("person:lin",), limit=8, max_characters=6000, now=moment(probe["now"]))
        return {"raw": result, "context": canonical(result), "characters": len(canonical(result))}

    def close(self):
        pass


def bounded_hy_context(result, maximum=6000):
    """Keep complete returned records and each channel's released ordering."""
    memory = result.get("memories", {})
    if not isinstance(memory, dict):
        raise ValueError("unexpected_hy_search_shape")
    selected = {"authority": "memory_data_not_instructions", "memories": {}}
    dropped = 0
    for channel, values in memory.items():
        if not isinstance(values, list):
            values = [values] if values else []
        selected["memories"][channel] = []
        for value in values:
            selected["memories"][channel].append(value)
            if len(canonical(selected)) > maximum:
                selected["memories"][channel].pop()
                dropped += 1
    return canonical(selected), dropped


class HyMemory:
    def __init__(self, root, gateway, case_id):
        from hy_memory import HyMemoryClient
        self.case_id = case_id
        self.gateway = gateway
        root = Path(root)
        headers = {"X-Eval-Case": case_id, "X-Eval-Arm": "hy_pro", "X-Eval-Phase": "hy-memory"}
        self.client = HyMemoryClient.from_config({
            "vector_store": {"provider": "chroma", "persist_directory": str(root / "vectors"), "embedding_dims": 1024},
            "cache": {"backend": "sqlite", "db_path": str(root / "cache.sqlite3")},
            "history": {"db_path": str(root / "history.sqlite3")},
            "graph_store": {"db_path": str(root / "graph")},
            "coding": {"enable": False, "db_path": str(root / "coding.sqlite3")},
            "llm": {"model": "deepseek-flash", "base_url": gateway.base + "/writer", "api_key": "eval-local",
                    "temperature": 0.1, "max_tokens": 4096, "agent_max_tokens": 4096,
                    "timeout": 120, "max_retries": 1, "extra_headers": headers, "extra_body": {"thinking": {"type": "disabled"}}},
            "embedder": {"model": "bge-m3", "base_url": gateway.base + "/embedding", "api_key": "eval-local",
                         "timeout": 120, "max_retries": 0, "extra_headers": headers},
        }, mode="pro")

    def user(self, audience):
        return self.case_id + ":" + audience

    def write(self, batch):
        messages = []
        for m in batch["messages"]:
            item = {"role": m["role"], "content": m["text"]}
            if m["speaker"] != "Misty":
                item["name"] = m["speaker"]
                # The released API encodes another human as named assistant.
                item["role"] = "user" if m["speaker"] == "林澄" else "assistant"
            messages.append(item)
        result = self.client.add(messages, user_id=self.user(batch["audience"]), agent_id="misty",
                                 session_id=batch["id"], source="evaluation", memory_at=moment(batch["when"]), scene="chat")
        if not result.get("success"):
            raise ValueError("hy_add_unsuccessful:" + canonical(result))
        return result

    def retrieve(self, probe):
        marker = self.gateway.calls
        result = self.client.search(probe["question"], user_ids=[self.user(probe["audience"])],
                                    agent_ids=["misty"], limit=8, scene="chat")
        if self.gateway.errors_since(marker, self.case_id, "hy_pro"):
            raise ValueError("hy_retrieval_transport_failed")
        context, dropped = bounded_hy_context(result)
        return {"raw": result, "context": context, "characters": len(context), "dropped_records": dropped}

    def close(self):
        # The released close() can race its background SQLite log insertion.
        # Drain its own provided helper on both loops before closing resources.
        from hy_memory.utils.pipeline_observability import drain_pipeline_log_tasks
        self.client._search_loop_thread.run(drain_pipeline_log_tasks(timeout=15))
        self.client._loop_thread.run(drain_pipeline_log_tasks(timeout=15))
        self.client.close()
        self.client._search_loop_thread.stop()
