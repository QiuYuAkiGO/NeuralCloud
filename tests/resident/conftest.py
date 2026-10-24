import pytest
from neuralcloud.storage import MemoryStore, initialize


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "memory.sqlite3"
    initialize(path, "misty-test")
    instance = MemoryStore(path, "misty-test")
    yield instance
    instance.close()
