import sqlite3

import pytest

from harness.memory.production import ProductionMemoryStore
from harness.memory.retention import retain_explicit_request
from tests.test_production_memory import experience


@pytest.mark.parametrize("text,pinned", [
    ("请记住，我以后喜欢喝茶。", True), ("小雾，帮我记下这个承诺。", True),
    ("Please remember that I prefer tea.", True), ("不要记住这个。", False),
    ("记住了吗？", False), ("他让我记住这句话", False),
])
def test_explicit_request_pins_original_before_model_and_retraction_wins(tmp_path, text, pinned):
    memory = ProductionMemoryStore(tmp_path / "memory.sqlite3")
    memory.ingest([experience(text=text)])
    assert retain_explicit_request(memory, "e1") == pinned
    assert retain_explicit_request(memory, "e1") == pinned
    with sqlite3.connect(memory.path) as db:
        assert db.execute("SELECT pinned FROM pm_records WHERE id='e1'").fetchone()[0] == int(pinned)
    memory.retract_source("e1", event_id="withdraw", reason="withdrawn")
    assert not retain_explicit_request(memory, "e1")
    assert not memory.recall(audience="qq:group:100").get("entries")
