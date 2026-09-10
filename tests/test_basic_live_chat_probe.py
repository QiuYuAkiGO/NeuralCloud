"""Acceptance evidence must distinguish model execution from release approval."""

from __future__ import annotations

import asyncio
import json

import pytest

from eval.basic_runtime import live_chat
from gui.persist import GuiPersistedSettings, save_gui_settings
from tests.test_basic_persona_continuity import _host


def _run(tmp_path, monkeypatch, *, rounds=20, repeat=False):
    config = tmp_path / "source.json"
    save_gui_settings(GuiPersistedSettings(mode="live", base_url="https://example.invalid", api_key="private-key"), config)
    calls = []

    def factory(*, config_path, presets_dir, state_path):
        start = 0 if not calls else 2
        calls.append(1)
        replies = ["我是小雾，19岁，5月20日生日。" + ("" if repeat else f"这是第{i}轮。") for i in range(start + 1, 21)]
        host, _ = _host(state_path.parent, monkeypatch, replies)
        return host

    monkeypatch.setattr(live_chat, "CognitiveServiceHost", factory)
    report = asyncio.run(live_chat.run_probe(
        state_dir=tmp_path / "private", out_dir=tmp_path / "evidence", config_path=config,
        manifest_path=live_chat.Path("presets/runtime/basic-runtime-v1.json"), rounds=rounds,
    ))
    assert json.loads((tmp_path / "evidence/summary.json").read_text(encoding="utf-8")) == report
    assert "private-key" not in (tmp_path / "evidence/summary.json").read_text(encoding="utf-8")
    return report


def test_twenty_rounds_still_require_prose_review_and_real_runtime(tmp_path, monkeypatch):
    report = _run(tmp_path, monkeypatch)
    assert report["status"] == "AWAITING_REVIEW"
    assert report["persona_review"] == "PENDING"
    assert report["full_milestone_verdict"] == "NOT_EVALUATED"
    assert not report["runtime_http_gui_delivery_tested"]
    assert all(report["checks"].values())
    assert len(list((tmp_path / "evidence").glob("delivery-*.json"))) == 20


def test_smoke_never_passes_and_duplicate_reply_fails(tmp_path, monkeypatch):
    assert _run(tmp_path / "smoke", monkeypatch, rounds=1)["status"] == "INCONCLUSIVE"
    duplicate = _run(tmp_path / "repeat", monkeypatch, repeat=True)
    assert duplicate["status"] == "FAIL"
    assert duplicate["attempts"][-1]["phase"] == "prepare"
    assert duplicate["attempts"][-1]["status"] == "FAIL"


def test_receipts_redact_keys_and_refuse_overlapping_directories(tmp_path):
    path = tmp_path / "report.json"
    live_chat.EvidenceWriter(path, ['key-"\\secret']).write({"error": 'contains key-"\\secret'})
    assert json.loads(path.read_text())["error"] == "contains <redacted>"
    assert live_chat.safe_endpoint("https://user:pass@example.org/v1?key=secret#token") == "https://example.org/v1"
    with pytest.raises(ValueError, match="separate"):
        asyncio.run(live_chat.run_probe(
            state_dir=tmp_path, out_dir=tmp_path / "out", config_path=None,
            manifest_path=live_chat.Path("presets/runtime/basic-runtime-v1.json"), rounds=20,
        ))


def test_existing_attempt_is_not_overwritten(tmp_path):
    with pytest.raises(ValueError, match="fresh"):
        asyncio.run(live_chat.run_probe(
            state_dir=tmp_path, out_dir=tmp_path.with_name(tmp_path.name + "-reports"), config_path=None,
            manifest_path=live_chat.Path("presets/runtime/basic-runtime-v1.json"), rounds=20,
        ))
