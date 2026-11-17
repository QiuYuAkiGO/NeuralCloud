"""Offline review workflow and isolation tests. No live model calls."""
from __future__ import annotations

import copy
import json
import os
import re
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from .review import DIMENSIONS, analyze, export_review, import_ratings
from .server import create_server


def make_fixture(run_dir: Path, *, all_failed: bool = False) -> Path:
    """Build an explicitly synthetic run for tests or browser QA."""
    (run_dir / "private").mkdir(parents=True, exist_ok=True)
    models = [{"id": "mock-model-a", "provider": "mock", "reasoning_policy": "synthetic fixture"},
              {"id": "mock-model-b", "provider": "mock", "reasoning_policy": "synthetic fixture"}]
    scenarios = [
        {"id": "emotion", "label": "情绪支持", "user_text": "@Misty 我努力准备了很久的比赛，结果第一轮就输了，真的很难受。"},
        {"id": "stance", "label": "立场表达", "user_text": "@Misty 你更看重长寿还是活得有意思？先说你选哪边。"},
    ]
    manifest = {"schema_version": "test", "run_id": "mock-review-ui-only", "manifest_sha256": "mock-manifest",
                "persona": {"name": "小雾", "core_traits": ["温柔", "慢热", "情绪细腻"],
                            "speech_style": "语气轻柔自然，像在认真和熟悉的人说话；情绪表达克制，不使用客服腔。"},
                "models": models, "scenarios": scenarios}
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    examples = ["【页面验收用合成文本，不是模型结果】\n准备了那么久，第一轮就结束了……会难受也是当然的。\n现在不用急着把它变成什么道理。",
                "【页面验收用合成文本，不是模型结果】\n我选活得有意思。时间长短很珍贵，但让我真正想留住的，还是那些认真喜欢过的瞬间。"]
    rows = [{"model_id": model["id"], "scenario_id": scene["id"],
             "status": "failed" if all_failed else "complete", "response_text": "" if all_failed else examples[n],
             "response_model": model["id"], "finish_reason": "stop", "usage": {"total_tokens": 88},
             "elapsed_ms": 234, "request_sha256": "mock-request"}
            for model in models for n, scene in enumerate(scenarios)]
    (run_dir / "private" / "results.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
    return run_dir


def make_ratings(packet: dict) -> dict:
    return {"schema_version": "chinese-reply-ratings-v1", "run_id": packet["run_id"],
            "packet_sha256": packet["packet_sha256"], "rater_id": "human-01", "calibrated": True,
            "ratings": [{"sample_id": sample["sample_id"], "response_sha256": sample["response_sha256"],
                         "scores": {dimension["id"]: 4 for dimension in DIMENSIONS}, "issues": [], "evidence": ""}
                        for sample in packet["samples"]], "submitted_at": "2026-09-16T12:00:00+08:00"}


def test_packet_is_anonymous_stable_and_waits_for_human(tmp_path: Path):
    run_dir = make_fixture(tmp_path)
    packet = export_review(run_dir)
    assert export_review(run_dir) == packet
    encoded = json.dumps(packet)
    assert "mock-model" not in encoded and "elapsed_ms" not in encoded and "reasoning_policy" not in encoded
    assert len(packet["samples"]) == 4
    assert len({s["sample_id"] for s in packet["samples"]}) == 4
    report = analyze(run_dir)
    assert report["status"] == "waiting_for_human" and not report["models"]
    assert "textContent=sample.response_text" in (run_dir / "review" / "index.html").read_text(encoding="utf-8")


def test_cannot_freeze_partial_or_changed_results(tmp_path: Path):
    run_dir = make_fixture(tmp_path)
    path = run_dir / "private" / "results.jsonl"
    original = path.read_text(encoding="utf-8")
    path.write_text("\n".join(original.splitlines()[:-1]), encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete"):
        export_review(run_dir)
    path.write_text(original, encoding="utf-8")
    export_review(run_dir)
    path.write_text(original.replace("234", "999"), encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        export_review(run_dir)


@pytest.mark.parametrize("mutation", ["missing", "bool", "hash", "evidence", "uncalibrated", "duplicate"])
def test_rejects_invalid_or_incomplete_ratings(tmp_path: Path, mutation: str):
    run_dir = make_fixture(tmp_path)
    ratings = make_ratings(export_review(run_dir))
    if mutation == "missing":
        ratings["ratings"].pop()
    elif mutation == "bool":
        ratings["ratings"][0]["scores"]["naturalness"] = True
    elif mutation == "hash":
        ratings["ratings"][0]["response_sha256"] = "wrong"
    elif mutation == "evidence":
        ratings["ratings"][0]["scores"]["naturalness"] = 2
    elif mutation == "uncalibrated":
        ratings["calibrated"] = False
    elif mutation == "duplicate":
        ratings["ratings"][1] = ratings["ratings"][0]
    with pytest.raises(ValueError):
        import_ratings(run_dir, ratings)
    assert not (run_dir / "private" / "ratings" / "first-seal.json").exists()


def test_seal_is_immutable_and_report_uses_original(tmp_path: Path):
    run_dir = make_fixture(tmp_path)
    ratings = make_ratings(export_review(run_dir))
    seal = import_ratings(run_dir, ratings)
    original_bytes = Path(seal["path"]).read_bytes()
    assert import_ratings(run_dir, ratings)["ratings_sha256"] == seal["ratings_sha256"]
    revision = copy.deepcopy(ratings)
    revision["ratings"][0]["scores"]["preference"] = 3
    updated = import_ratings(run_dir, revision)
    assert updated["status"] == "revision_preserved"
    assert Path(seal["path"]).read_bytes() == original_bytes
    report = analyze(run_dir)
    assert report["status"] == "sealed" and report["ratings_sha256"] == seal["ratings_sha256"]
    assert report["revision_count"] == 1
    assert all(s["rating"]["scores"]["preference"] == 4 for m in report["models"] for s in m["scenarios"])
    assert report["models"][0]["reasoning_policy"] == "synthetic fixture"


def test_no_valid_samples_are_not_fake_review_work(tmp_path: Path):
    run_dir = make_fixture(tmp_path, all_failed=True)
    packet = export_review(run_dir)
    assert packet["samples"] == []
    report = analyze(run_dir)
    assert report["status"] == "no_valid_samples" and len(report["technical_missing"]) == 4
    with pytest.raises(ValueError):
        import_ratings(run_dir, make_ratings(packet))


def test_swapped_private_mapping_is_rejected_even_for_identical_replies(tmp_path: Path):
    run_dir = make_fixture(tmp_path)
    packet = export_review(run_dir)
    path = run_dir / "private" / "blind-map.json"
    mapping = json.loads(path.read_text(encoding="utf-8"))
    # Fixture models intentionally return the same response within each scene.
    scene_samples = [sample for sample in packet["samples"] if sample["scenario_id"] == "emotion"]
    left, right = [sample["sample_id"] for sample in scene_samples]
    mapping["samples"][left], mapping["samples"][right] = mapping["samples"][right], mapping["samples"][left]
    path.write_text(json.dumps(mapping), encoding="utf-8")
    with pytest.raises(ValueError, match="commitment"):
        import_ratings(run_dir, make_ratings(packet))
    assert not (run_dir / "private" / "ratings" / "first-seal.json").exists()


def test_interrupted_packet_publication_recovers_original_shuffle(tmp_path: Path, monkeypatch):
    run_dir = make_fixture(tmp_path)
    real_link = os.link

    def fail_packet(source, target):
        if Path(target).name == "samples.json":
            raise OSError("simulated interrupted publication")
        return real_link(source, target)

    monkeypatch.setattr(os, "link", fail_packet)
    with pytest.raises(OSError, match="interrupted"):
        export_review(run_dir)
    envelope = json.loads((run_dir / "private" / "export-envelope.json").read_text(encoding="utf-8"))
    assert not (run_dir / "review" / "samples.json").exists()
    monkeypatch.setattr(os, "link", real_link)
    assert export_review(run_dir) == envelope["packet"]


def test_interrupted_first_seal_publication_can_be_retried(tmp_path: Path, monkeypatch):
    run_dir = make_fixture(tmp_path)
    ratings = make_ratings(export_review(run_dir))
    real_link = os.link

    def fail_pointer(source, target):
        if Path(target).name == "first-seal.json":
            raise OSError("simulated interrupted publication")
        return real_link(source, target)

    monkeypatch.setattr(os, "link", fail_pointer)
    with pytest.raises(OSError, match="interrupted"):
        import_ratings(run_dir, ratings)
    assert not (run_dir / "private" / "ratings" / "first-seal.json").exists()
    assert len(list((run_dir / "private" / "ratings").glob("*.json"))) == 1
    monkeypatch.setattr(os, "link", real_link)
    assert import_ratings(run_dir, ratings)["status"] == "sealed"


def test_server_isolated_and_requires_local_authenticated_submission(tmp_path: Path):
    run_dir = make_fixture(tmp_path)
    server = create_server(run_dir, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base + "/") as response:
            page = response.read().decode("utf-8")
        token = re.search(r'name="review-token" content="([^\"]+)"', page).group(1)
        for path in ["/private/blind-map.json", "/../manifest.json", "/%2e%2e/apiyi-api-key.txt", "/report"]:
            with pytest.raises(HTTPError) as caught:
                urlopen(base + path)
            assert caught.value.code in {403, 404}
        ratings = make_ratings(export_review(run_dir))
        body = json.dumps(ratings).encode("utf-8")
        with pytest.raises(HTTPError) as caught:
            urlopen(Request(base + "/submit", data=body, headers={"Content-Type": "application/json"}))
        assert caught.value.code == 403
        with pytest.raises(HTTPError) as caught:
            urlopen(Request(base + "/", headers={"Host": "malicious.example"}))
        assert caught.value.code == 403
        request = Request(base + "/submit", data=body, headers={"Content-Type": "application/json", "Origin": base, "X-Review-Token": token})
        with urlopen(request) as response:
            assert json.loads(response.read())["status"] == "sealed"
        with urlopen(base + "/report") as response:
            assert "mock-model-a" in response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


if __name__ == "__main__":
    import tempfile
    from .server import serve

    demo_dir = make_fixture(Path(tempfile.mkdtemp(prefix="chinese-review-ui-MOCK-")))
    print(f"MOCK ONLY — generated fixture: {demo_dir}", flush=True)
    serve(demo_dir, port=8796)
