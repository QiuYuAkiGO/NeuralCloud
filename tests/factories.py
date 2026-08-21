from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from harness.core.models import (
    InteractionEvent,
    PersonaCore,
    PersonaIdentity,
    PsychState,
    WorldState,
)
from harness.memory.service import MemoryService

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 21, 13, 54, tzinfo=TZ)


def vaelith(*, created_at: datetime | None = None) -> PersonaCore:
    return PersonaCore(
        persona_id="persona_v3",
        version="v3",
        identity=PersonaIdentity(
            name="Vaelith",
            core_traits=["傲慢", "善良", "高自尊", "不直接表达关心"],
            values=["承诺必须兑现", "不轻易求助"],
            speech_style=["短句", "偶尔毒舌", "生气时反而正式"],
            knowledge_boundary=["不知道现代社会常识之外的事"],
            locked=True,
        ),
        created_at=created_at or datetime(2026, 1, 1, tzinfo=TZ),
    )


def seed_running_example(service: MemoryService, *, now: datetime | None = None) -> None:
    moment = now or NOW
    service.apply_relationship_delta(
        "user",
        closeness=0.18,
        trust=0.24,
        recent_tension=0.31,
        last_major_event="evt_04010",
        now=moment,
    )
    service.upsert_psych(
        PsychState(
            character_id="character",
            short={"mood": "guarded", "label": "slightly_defensive", "ttl_seconds": 7200},
            updated_at=moment,
        ),
        now=moment,
    )
    service.update_world(
        WorldState(
            time=moment,
            channel="chat",
            user_presence="active",
            user_activity="reading",
            active_application="Firefox",
        )
    )
    service.record_event(
        InteractionEvent(
            event_id="evt_04010",
            timestamp=datetime(2026, 8, 20, 23, 40, tzinfo=TZ),
            speaker="user",
            raw_text="你以后不用管我了",
            session_id="sess_0881",
            evidence=["chat:sess_0881:evt_04010"],
        ),
        importance=0.86,
        emotional_valence=-0.55,
        event_type="commitment_or_statement",
        curate=False,
        now=moment,
    )
