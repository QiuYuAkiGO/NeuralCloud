"""Authenticated UI preview uses the production scope and never adopts memories."""
import asyncio

import pytest

from tests.test_qq_private_worker import PRIVATE, FRIEND_PRIVATE, install_records, retained, setup_host


def test_preview_uses_owner_grant_without_mutation(tmp_path, monkeypatch):
    async def run():
        host = setup_host(tmp_path, monkeypatch)
        install_records(host, retained("group", conversation="100"), retained("local"),
                        retained("friend", conversation=FRIEND_PRIVATE))
        host.qq_memory.configure({"owner_private_full_recall_enabled": True})
        before = host.chat.library.load("misty").model_dump()
        try:
            payload = {"group_id": PRIVATE, "query": "今天遇见的人"}
            for _ in range(2):
                preview = await host.control("qq_memory.preview", payload)
                assert preview["entries"]
                assert preview["coverage"] == "selected_owner_authorized_cross_conversation_memories"
            narrow = await host.control("qq_memory.preview", {"group_id": FRIEND_PRIVATE, "query": "所有"})
            assert [entry["record_id"] for entry in narrow["entries"]] == ["friend"]
            assert host.chat.library.load("misty").model_dump() == before
            assert all(event.hit_count == 0 for event in host.chat.memory.stores.episodic.list_all())
        finally:
            await host.close()
    asyncio.run(run())


@pytest.mark.parametrize("payload", [{}, {"group_id": PRIVATE, "query": "all", "include_owner_local": True},
                                    {"group_id": PRIVATE, "query": 123}])
def test_preview_cannot_accept_scope_overrides(tmp_path, monkeypatch, payload):
    async def run():
        host = setup_host(tmp_path, monkeypatch)
        try:
            with pytest.raises(ValueError, match="preview_parameters_invalid"):
                await host.control("qq_memory.preview", payload)
        finally:
            await host.close()
    asyncio.run(run())
