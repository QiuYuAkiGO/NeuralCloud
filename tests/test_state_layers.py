from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from harness.core.models import PsychState, WorldState
from harness.memory.perception import ChatPerceptionBackend

TZ = ZoneInfo("Asia/Shanghai")


def test_psych_short_ttl_expires_on_read(service) -> None:
    now = datetime(2026, 8, 21, 10, 0, tzinfo=TZ)
    service.upsert_psych(
        PsychState(
            character_id="character",
            long={"sensitivity_to_betrayal": 0.85},
            mid={"current_focus": "project_alpha", "energy_level": 0.6},
            short={"mood": "slightly_irritated", "arousal": 0.4, "ttl_seconds": 7200},
            updated_at=now,
        ),
        now=now,
    )
    live = service.get_state(now=now + timedelta(hours=1))
    assert live.psych is not None
    assert live.psych.short["mood"] == "slightly_irritated"
    assert live.short_psych_expired is False

    expired = service.get_state(now=now + timedelta(hours=3))
    assert expired.short_psych_expired is True
    assert expired.psych is not None
    assert expired.psych.short.get("expired") is True
    assert "mood" not in expired.psych.short
    assert expired.psych.long["sensitivity_to_betrayal"] == 0.85


def test_relationship_delta(service) -> None:
    now = datetime(2026, 8, 20, 22, 10, tzinfo=TZ)
    rel = service.apply_relationship_delta(
        "user",
        closeness=0.18,
        trust=0.24,
        attachment=0.05,
        recent_tension=0.31,
        last_major_event="evt_03812",
        history_note="共同经历过重要事件",
        now=now,
    )
    assert rel.closeness == pytest.approx(0.68)
    assert rel.trust == pytest.approx(0.74)
    assert rel.last_major_event == "evt_03812"
    again = service.apply_relationship_delta("user", recent_tension=-0.1, now=now)
    assert again.recent_tension == pytest.approx(0.21)


def test_world_chat_perception(service) -> None:
    now = datetime(2026, 8, 21, 13, 54, tzinfo=TZ)
    world = service.update_world(now=now)
    assert world.channel == "chat"
    assert world.user_presence == "active"
    snap = service.get_state(now=now)
    assert snap.world is not None
    assert snap.world.time == now

    custom = WorldState(
        time=now,
        active_application="Firefox",
        active_window_title="本地 Wiki - 角色设定",
        open_windows=["Files", "Terminal"],
        user_presence="active",
        user_activity="reading",
        channel="chat",
    )
    service.update_world(custom)
    assert service.get_state(now=now).world.active_application == "Firefox"


def test_perception_protocol_stub() -> None:
    backend = ChatPerceptionBackend(user_presence="away")
    observed = backend.observe(now=datetime(2026, 8, 21, tzinfo=TZ))
    assert observed.user_presence == "away"
    assert observed.channel == "chat"
