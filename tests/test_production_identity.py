import pytest

from harness.memory.identity import IdentityStore
from harness.memory.production import ProductionMemoryStore
from tests.test_production_memory import experience, NOW


def test_identity_binding_is_explicit_reversible_and_cannot_widen_permissions(tmp_path):
    memory = ProductionMemoryStore(tmp_path / "memory.sqlite3")
    memory.ingest([experience("public"), experience("private", audience="qq:private:999:200", text="secret"),
                   experience("confirmation", author="local:main", audience="local:main", text="I explicitly bind these accounts")])
    identities = IdentityStore(memory)
    assert identities.aliases(("other:person:200",)) == ("other:person:200",)
    with pytest.raises(ValueError, match="owner_authorization"):
        identities.bind("other:person:200", "qq:member:200", event_id="bad", evidence_ids=["confirmation"], authorized_by="model")
    assert identities.bind("other:person:200", "qq:member:200", event_id="bind", evidence_ids=["confirmation"], authorized_by="local:main")
    assert not identities.bind("other:person:200", "qq:member:200", event_id="bind", evidence_ids=["confirmation"], authorized_by="local:main")
    assert set(identities.aliases(("other:person:200",))) == {"qq:member:200", "other:person:200"}
    public = memory.recall(audience="qq:group:100", subject_ids=("other:person:200",), now=NOW)
    assert {row["record_id"] for row in public["entries"]} == {"public"}
    assert "secret" not in str(public)
    assert IdentityStore(ProductionMemoryStore(memory.path)).aliases(("other:person:200",)) == identities.aliases(("other:person:200",))
    identities.bind("other:person:200", "qq:member:200", event_id="unbind", evidence_ids=["confirmation"], authorized_by="local:main", active=False)
    assert identities.aliases(("other:person:200",)) == ("other:person:200",)


def test_withdrawing_binding_evidence_immediately_removes_alias(tmp_path):
    memory = ProductionMemoryStore(tmp_path / "memory.sqlite3")
    memory.ingest([experience("confirmation", author="local:main", audience="local:main")])
    identities = IdentityStore(memory)
    identities.bind("other:person:200", "qq:member:200", event_id="bind", evidence_ids=["confirmation"], authorized_by="local:main")
    memory.retract_source("confirmation", event_id="withdraw", reason="wrong binding")
    assert identities.aliases(("other:person:200",)) == ("other:person:200",)
