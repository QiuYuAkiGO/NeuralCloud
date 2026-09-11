import asyncio
import hashlib

import httpx
import pytest

from harness.embodiment.providers.mistyai import MistyAIClient, MistyAIError


def digest(value):
    return hashlib.sha256(value).hexdigest()


def test_verified_download_is_reused_without_losing_media_validation():
    async def run():
        content = b"synthetic evidence"
        calls = []

        def handle(request):
            calls.append(request.url.path)
            return httpx.Response(200, content=content, headers={"content-type": "image/png"})

        async with httpx.AsyncClient(base_url="http://body.test", transport=httpx.MockTransport(handle)) as http:
            client = MistyAIClient("http://body.test", token="synthetic", client=http, evidence_byte_cache_bytes=64)
            key = digest(content)
            await client.verify_evidence_ref("evidence://sha256/" + key, expected_media_type="image/png")
            assert await client.get_evidence(key, expected_media_type="IMAGE/PNG; charset=binary") == content
            with pytest.raises(MistyAIError) as error:
                await client.get_evidence(key, expected_media_type="application/json")
            assert error.value.code == "evidence_media_type_mismatch"
            assert calls == ["/v1/evidence/" + key]
            await client.close()
            assert await client.get_evidence(key) == content
            assert len(calls) == 2
    asyncio.run(run())


@pytest.mark.parametrize("capacity,entries", [(4,128),(64,1)])
def test_cache_eviction_is_bounded_and_requires_a_new_verified_download(capacity, entries):
    async def run():
        content = {digest(value): value for value in (b"first", b"next")}
        calls = []

        def handle(request):
            key = request.url.path.rsplit("/",1)[1]
            calls.append(key)
            return httpx.Response(200, content=content[key], headers={"content-type":"image/png"})

        async with httpx.AsyncClient(base_url="http://body.test", transport=httpx.MockTransport(handle)) as http:
            client = MistyAIClient("http://body.test", token="synthetic", client=http,
                evidence_byte_cache_bytes=capacity, evidence_cache_entries=entries)
            for value in (b"first", b"next", b"first"):
                assert await client.get_evidence(digest(value)) == value
            assert calls == [digest(b"first"),digest(b"next"),digest(b"first")]
            assert client._evidence_content_bytes <= capacity
            assert len(client._evidence_content) <= entries
    asyncio.run(run())


def test_digest_failure_does_not_populate_cache_and_default_still_downloads():
    async def run(capacity):
        calls = []
        good = b"good"

        def handle(request):
            calls.append(request)
            return httpx.Response(200, content=b"bad" if len(calls)==1 else good,
                                  headers={"content-type":"image/png"})

        async with httpx.AsyncClient(base_url="http://body.test", transport=httpx.MockTransport(handle)) as http:
            client = MistyAIClient("http://body.test", token="synthetic", client=http, evidence_byte_cache_bytes=capacity)
            with pytest.raises(MistyAIError) as error:
                await client.get_evidence(digest(good))
            assert error.value.code == "evidence_digest_mismatch"
            assert not client._evidence_content
            assert await client.get_evidence(digest(good)) == good
            assert await client.get_evidence(digest(good)) == good
            assert len(calls) == (3 if capacity == 0 else 2)
    asyncio.run(run(0))
    asyncio.run(run(64))


def test_each_observation_is_fresh_even_when_unchanged_pixels_use_cached_bytes():
    async def run():
        from datetime import datetime, timezone
        content = b"same pixels"
        calls = []

        def handle(request):
            calls.append((request.method, request.url.path))
            if request.method == "GET":
                return httpx.Response(200, content=content, headers={"content-type":"image/png"})
            return httpx.Response(200, json={
                "schema_version":"body-contract-v1", "observation_id":f"obs-{len(calls)}",
                "body_instance_id":"body-test", "environment_revision":1,
                "captured_at":datetime.now(timezone.utc).isoformat(),
                "frame":{"evidence_ref":"evidence://sha256/"+digest(content), "sha256":digest(content),
                         "media_type":"image/png", "width":640, "height":480},
                "display":{"name":":1", "width":640, "height":480, "scale":1},
                "windows":[], "fs_events":[], "running_apps":[], "backend_health":{"screen":"healthy"}})

        async with httpx.AsyncClient(base_url="http://body.test", transport=httpx.MockTransport(handle)) as http:
            client = MistyAIClient("http://body.test", token="synthetic", client=http, evidence_byte_cache_bytes=64)
            first = await client.observe()
            assert await client.get_evidence(first.frame.sha256) == content
            second = await client.observe()
            assert await client.get_evidence(second.frame.sha256) == content
            assert first.observation_id != second.observation_id
            assert [method for method,_ in calls] == ["POST","GET","POST"]
    asyncio.run(run())
