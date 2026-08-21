from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from harness.core.exceptions import PersonaLockedError
from harness.core.models import PersonaCore, PersonaIdentity
from tests.factories import vaelith

TZ = ZoneInfo("Asia/Shanghai")


def test_persona_bootstrap_and_lock(service) -> None:
    persona = service.get_persona()
    assert persona is not None
    assert persona.identity.locked is True
    assert persona.identity.name == "Vaelith"
    with pytest.raises(PersonaLockedError):
        service.update_persona(persona)
    with pytest.raises(PersonaLockedError):
        service.delete_persona(persona.persona_id)
    with pytest.raises(PersonaLockedError):
        service.stores.persona.update(persona)
    assert service.stores.audit.unauthorized_persona_attempts() >= 2
    # still the original row
    assert service.get_persona().identity.core_traits[0] == "傲慢"


def test_cannot_overwrite_existing_persona(service) -> None:
    clone = PersonaCore(
        persona_id="persona_v3",
        version="v-hack",
        identity=PersonaIdentity(name="Assistant", core_traits=["温柔体贴"], locked=False),
        created_at=datetime(2026, 8, 21, tzinfo=TZ),
    )
    with pytest.raises(PersonaLockedError):
        service.stores.persona.insert(clone)
    assert service.get_persona().identity.name == "Vaelith"


def test_vaelith_fixture_locked() -> None:
    assert vaelith().identity.locked is True
