from fastapi.testclient import TestClient

from neuralcloud.api import create_app
from neuralcloud.contracts import Correction, SourceEvent, SourceEventV2


def test_private_caller_gets_only_live_authored_text_across_conversations(store):
    peer, bot, group = "10002", "10001", "group:onebot:10001:10009"
    private = f"c2c:onebot:{bot}:{peer}"

    def add(key, sender=peer, conversation=group, version=2, **extra):
        fields = dict(
            character_id="misty-test",
            source_instance="onebot:" + bot,
            session_id="s",
            conversation_id=conversation,
            event_id=key,
            occurred_at=10,
            content="摄影",
            participants=[peer],
        )
        if version == 2:
            fields["channel"] = dict(
                platform="qq",
                account_id=bot,
                sender_id=sender,
                message_id=key,
                conversation_id=conversation,
                mentions=[peer],
            )
        fields.update(extra)
        return store.ingest((SourceEventV2 if version == 2 else SourceEvent)(schema_version=version, **fields))

    # Other senders mention this user and outrank the long actual post in a global keyword search.
    for i in range(55):
        add("other-" + str(i), sender="10003")
    group_message = add("group", content="很长的前文" * 600 + "这次的摄影展是晴天。")
    private_message = add("private", conversation=private, version=1, content="之前的摄影作品。")
    add("wrong-private-peer", conversation="c2c:onebot:10001:10003")
    add("members", version=1, participants=[peer, "10003"])
    add("writer", source_instance="notes")
    add("qqbot", channel=dict(platform="qqbot", account_id=bot, sender_id=peer, message_id="1", conversation_id=group))
    add("sensitive", privacy="sensitive")
    add("caption", perception=dict(source_uri="doc:m", coverage="frames", observed_fraction=0.5))
    add("self", kind="self_expression")
    add("alias-only", content="散步", aliases=["摄影"])
    add("same-turn", occurred_at=11)
    path = "/v1/recall/qq-self-history"
    query = {"account_id": peer, "query": "摄影", "before": 11, "limit": 2}
    with TestClient(create_app(store, "x" * 32)) as client:
        assert client.post(path, json=query).status_code == 401
        client.headers["Authorization"] = "Bearer " + "x" * 32
        assert client.get("/v1/health").json()["qq_self_history"] is True
        for invalid in (dict(account_id="小明"), dict(account_id="qqbot:10002"), dict(query="  "), dict(before="11")):
            assert client.post(path, json=query | invalid).status_code == 422
        data = client.post(path, json=query).json()
        assert (data["character_id"], data["account_id"]) == ("misty-test", peer)
        items = {item["record_id"]: item for item in data["items"]}
        assert items.keys() == {group_message["record_id"], private_message["record_id"]}
        assert items[group_message["record_id"]]["origin"] == "qq_group"
        assert items[group_message["record_id"]]["excerpt"].endswith("这次的摄影展是晴天。")
        assert items[private_message["record_id"]]["origin"] == "qq_private"
        assert client.post(path, json=query | {"query": "摄影 素描"}).json()["items"] == []
        store.correct(
            Correction(
                character_id="misty-test",
                source_instance="editor",
                event_id="remove",
                record_id=group_message["record_id"],
                expected_revision=1,
                reason="撤回",
                retract=True,
            )
        )
        assert client.post(path, json=query).json()["items"] == [items[private_message["record_id"]]]
