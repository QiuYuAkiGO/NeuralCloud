from datetime import datetime, timezone
import json

from harness.core.models import MemoryScope
from harness.memory.orientation import OrientationInterpretation, OrientationStore
from harness.memory.production import Experience, ProductionMemoryStore


def test_private_experience_changes_only_disposition_and_retraction_fences_it(tmp_path):
    memory = ProductionMemoryStore(tmp_path / "memory.sqlite3")
    now = datetime.now(timezone.utc)
    memory.ingest([Experience(record_id="private-source", author_id="qq:member:200", text="private medical secret",
        occurred_at=now, independent_key="private-original", source_refs=("private-original",),
        scope=MemoryScope(owner_id="misty", channel="qq_private", conversation_id="qq:private:100:200",
                          visibility=["local:main", "qq:private:100:200"]))])
    store = OrientationStore(memory)
    value = OrientationInterpretation(direction="repair", reason="private rationale", source_refs=["private-source"])
    assert store.revise(value, subject_id="qq:member:200", audience="qq:private:100:200", event_id="interpretation")
    projection = store.projection("qq:member:200")
    assert projection["direction"] == "repair"
    assert store.projection("qq:member:201") is None  # display-name similarity cannot bind people
    assert all(word not in json.dumps(projection) for word in ("secret", "private", "rationale", "source"))
    assert not memory.recall(audience="qq:group:300", subject_ids=("qq:member:200",)).get("entries")
    assert store.validate(projection)
    assert not store.revise(value, subject_id="qq:member:200", audience="qq:private:100:200", event_id="interpretation")
    assert OrientationStore(ProductionMemoryStore(memory.path)).projection("qq:member:200") == projection
    memory.retract_source("private-source", event_id="withdraw", reason="withdrawn")
    assert store.projection("qq:member:200") is None
    assert not store.validate(projection)


def test_new_direction_revises_without_forcing_positive_growth(tmp_path):
    memory = ProductionMemoryStore(tmp_path / "memory.sqlite3")
    now = datetime.now(timezone.utc)
    memory.ingest([Experience(record_id="source", author_id="qq:member:200", text="an encounter",
        occurred_at=now, independent_key="original", source_refs=("original",),
        scope=MemoryScope(owner_id="misty", channel="qq_group", conversation_id="qq:group:300",
                          visibility=["local:main", "qq:group:300"]))])
    store = OrientationStore(memory)
    for index, direction in enumerate(("approach", "avoidance", "neutrality", "repair")):
        store.revise(OrientationInterpretation(direction=direction, reason="a revisable choice", source_refs=["source"]),
            subject_id="qq:member:200", audience="qq:group:300", event_id=str(index))
        projection = store.projection("qq:member:200")
        assert projection["direction"] == direction
    assert store.validate(None)


def test_qq_appraisal_reads_prior_understanding_and_carries_only_private_disposition(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from harness.cognitive.production import appraise_qq
    from tests.test_production_cognition import adapter_at, command
    from tests.test_shared_memory import person
    from harness.qq.memory import QQVisualMemoryStore
    from tests.test_qq_protocol_memory import protocol_profile, protocol_observation

    async def run():
        prompts = []
        class Model:
            def complete_chat_result(self, messages, **kwargs):
                payload = json.loads(messages[-1]["content"])
                prompts.append(payload)
                value = {"orientation": {"direction": "repair", "reason": "confidential personal explanation",
                         "source_refs": [payload["current_source_id"]]}} if len(prompts) == 1 else {}
                return SimpleNamespace(finish_reason="stop", content_type="text", has_tool_calls=False,
                                       has_refusal=False, text=json.dumps(value))

        adapter = adapter_at(tmp_path, Model())
        memory = ProductionMemoryStore(tmp_path / "qq.sqlite3")
        host = SimpleNamespace(adapter=adapter, chat=SimpleNamespace(memory=adapter.pipeline.memory),
            shared=SimpleNamespace(production_store=memory))
        raw = QQVisualMemoryStore(tmp_path / "qq-raw.sqlite3")
        _, profile = protocol_profile(raw, group="private:999:200", member="200")
        private = raw.record_protocol_observation(protocol_observation(profile, text="a private secret")).observation
        public = person(group="100", suffix="public").observations[0]
        from harness.memory.production import BeliefRevision
        public_id = memory.ingest_qq(public)
        memory.revise(BeliefRevision(record_id="prior-understanding", subject_id="qq:member:200", topic="conversation",
            statement="a tentative prior understanding", revision=1, supporting_ids=(public_id,), valid_from=datetime.now(timezone.utc)),
            event_id="prior-revision")
        try:
            for index, source in enumerate((private, public)):
                audience = "qq:" + source.group_id if source.group_id.startswith("private:") else "qq:group:" + source.group_id
                owner = SimpleNamespace(preset=SimpleNamespace(persona=adapter.pipeline.memory.get_persona()),
                    llm=adapter.pipeline.llm, derived_memory=memory.recall(audience=audience, subject_ids=("qq:member:200",)))
                result = await appraise_qq(host, command("qq-" + str(index)),
                    SimpleNamespace(group_id=source.group_id, trigger_member_id="200", idle_intent=None),
                    {"source": source.model_dump(mode="json")}, owner)
            assert result["interpersonal_orientation"]["direction"] == "repair"
            shown = json.dumps(prompts[1])
            assert "a private secret" not in shown and "confidential personal explanation" not in shown
            assert prompts[1]["production"]["interpersonal_orientation"]["subject_id"] == "qq:member:200"
            assert not prompts[1]["production"].get("affect")
            assert any("a tentative prior understanding" in row["text"] for row in prompts[1]["memories"])
        finally:
            await adapter.close()
    asyncio.run(run())
