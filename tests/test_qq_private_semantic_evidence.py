"""Large local fact histories remain bounded through the actual private reply DTO."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from harness.cognitive.qq_memory import QQMemoryControl, QQMemoryWorker
from harness.qq import decision
from harness.qq.conversation import private_scope_id
from tests.test_qq_decision import (
    isolated_static_parser_cache as isolated_static_parser_cache, setup as setup,
)
from tests.test_qq_decision_context_budget import _gate_payload, _persona_payload
from tests.test_qq_private_memory_reply import OWNER, SELF, private_binding
from tests.test_qq_private_recall import NOW, add_episode, add_fact, qq_scope
from tests.test_qq_private_worker import install_records, setup_host


def test_owner_fact_with_41_supports_keeps_two_visible_ids_through_reply_and_adoption(setup, tmp_path, monkeypatch):
    args, binding, memory, _ = private_binding(setup, tmp_path, monkeypatch)
    key = private_scope_id(SELF, OWNER)
    supports = [add_episode(memory, f"support-{index:02}", text="已核对的一次本地交流。",
                           time=NOW + timedelta(minutes=index), importance=0.1)
                for index in range(41)]
    hidden = add_episode(memory, "hidden-support", scope=qq_scope("10002"),
                         time=NOW + timedelta(hours=1), text="未授权群中的记录。")
    fact = add_fact(memory, "many-support-fact", value="长期复盘说明我偏爱渐进式练习与每日总结",
        supporting_events=[*(row.event_id for row in reversed(supports)), hidden.event_id, "missing-support"])
    binding.settings.qq_memory_recall_max_items = 1
    owner = SimpleNamespace(chat=SimpleNamespace(settings=binding.settings, memory=memory, active_preset_id="misty"),
        shared=SimpleNamespace(qq_settings=SimpleNamespace(self_member_id=SELF, allowed_group_ids=("10001",),
                                                        allowed_private_peer_ids=(OWNER,))))
    worker = QQMemoryWorker(owner, control_path=tmp_path / "semantic-recall-control.json")
    worker.control = QQMemoryControl(recall_enabled=True, private_recall_enabled=True,
                                     owner_private_full_recall_enabled=True)
    try:
        derived = worker.recall(group_id=key, member_id=OWNER, query=fact.value)
        assert len(derived["entries"]) == 1 and derived["entries"][0]["record_id"] == fact.fact_id
        expected_ids = [row.event_id for row in supports[-2:]]
        assert derived["entries"][0]["supporting_episode_ids"] == expected_ids
        selected = replace(binding, derived_memory=derived)
        result = decision.decide_qq_reply(**{**args, "shared_owner": selected})
        assert result.outcome == "ready", result.error_type
        assert decision.QQReplyDecision.model_validate(result.model_dump()) == result
        assert _gate_payload(setup[1])["derived_memory"] == _persona_payload(setup[1])["derived_memory"] == derived
        assert result.recalled_memory_ids == [fact.fact_id]
        assert result.recalled_episode_ids == expected_ids
        assert all(row.hit_count == 0 for row in memory.stores.episodic.list_all())

        async def apply_to_durable_owner():
            monkeypatch.setattr("gui.service.probe_llm", lambda *args, **kwargs: True)
            host = setup_host(tmp_path / "durable-owner", monkeypatch, groups=("10001",), peers=(OWNER,))
            host.shared.qq_settings.self_member_id = SELF
            host.qq_memory.control = worker.control.model_copy(update={"enabled": True})
            install_records(host, *memory.stores.episodic.list_all(), semantic=memory.stores.semantic.list_all())
            try:
                host.qq_memory.note_recall(result)
                await host.qq_memory.maintain(now=result.completed_at)
                stored = host.chat.library.load("misty")
                assert stored.qq_memory_state["recalls"][result.decision_id] == expected_ids
                hits = {row.event_id: row.hit_count for row in stored.episodic}
                assert all(hits[row.event_id] == (1 if row.event_id in expected_ids else 0) for row in supports)
                assert hits[hidden.event_id] == 0
                assert next(row for row in stored.semantic if row.fact_id == fact.fact_id).confidence == fact.confidence
            finally:
                await host.close()

        asyncio.run(apply_to_durable_owner())
    finally:
        memory.close()
