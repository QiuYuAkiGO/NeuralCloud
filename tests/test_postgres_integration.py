from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from harness.memory.store.postgres import make_engine
from harness.memory.store.schema import Base

pytestmark = pytest.mark.integration


def _database_url() -> str | None:
    return os.environ.get("NC_DATABASE_URL") or os.environ.get("DATABASE_URL")


@pytest.fixture
def pg_engine():
    url = _database_url()
    if not url:
        url = "postgresql+psycopg2://neuralcloud:neuralcloud@localhost:5432/neuralcloud"
    try:
        engine = make_engine(url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            conn.commit()
    except Exception as exc:  # pragma: no cover - skip when docker/driver is down
        pytest.skip(f"postgres unavailable: {exc}")
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    from harness.memory.store.postgres import create_all as pg_create_all

    Base.metadata.drop_all(engine)
    pg_create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_postgres_persona_trigger_and_roundtrip(pg_engine) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.orm import Session

    from harness.core.exceptions import EvidenceRequiredError, PersonaLockedError
    from harness.core.models import EpisodicEvent, SemanticFact
    from harness.llm.embeddings import MockEmbedding
    from harness.memory.service import MemoryService
    from harness.memory.store.postgres import PostgresStores
    from tests.factories import vaelith

    TZ = ZoneInfo("Asia/Shanghai")
    with Session(pg_engine) as session:
        stores = PostgresStores.from_session(session)
        service = MemoryService(stores, embeddings=MockEmbedding())
        persona = service.bootstrap_persona(vaelith())
        session.commit()
        with pytest.raises(PersonaLockedError):
            service.update_persona(persona)
        # DB trigger is a second line of defense
        with pytest.raises(DBAPIError):
            session.execute(
                text("UPDATE persona_core SET version = 'hacked' WHERE persona_id = :pid"),
                {"pid": persona.persona_id},
            )
        session.rollback()

        event = service.record_event(
            EpisodicEvent(
                event_id="evt_pg_1",
                time=datetime(2026, 8, 21, tzinfo=TZ),
                participants=["user", "character"],
                summary="用户提到猫",
                importance=0.5,
                emotional_valence=0.0,
                evidence=["traj_pg"],
                objective_fact="用户提到猫",
                uniqueness=0.82,
                tier="mid",
                expire_at=datetime(2026, 8, 28, tzinfo=TZ),
                hit_count=4,
                last_hit_at=datetime(2026, 8, 22, tzinfo=TZ),
                tier_updated_at=datetime(2026, 8, 22, tzinfo=TZ),
                tier_reasons=["test:roundtrip"],
            )
        )
        session.commit()
        restored = stores.episodic.get(event.event_id)
        assert restored is not None
        assert restored.tier == "mid"
        assert restored.uniqueness == pytest.approx(0.82)
        assert restored.expire_at == datetime(2026, 8, 28, tzinfo=TZ)
        assert restored.hit_count == 4
        assert restored.last_hit_at == datetime(2026, 8, 22, tzinfo=TZ)
        assert restored.tier_reasons == ["test:roundtrip"]
        with pytest.raises(EvidenceRequiredError):
            stores.semantic.insert(
                SemanticFact.model_construct(
                    fact_id="sem_bad",
                    subject="user",
                    predicate="x",
                    value="y",
                    confidence=0.9,
                    supporting_events=[],
                    formed_at=datetime(2026, 8, 21, tzinfo=TZ),
                )
            )
        session.rollback()
        assert stores.persona.get().identity.name == "Vaelith"
        assert stores.audit.unauthorized_persona_attempts() >= 1
