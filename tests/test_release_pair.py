"""Release identity must not silently fall back to main or a dirty body checkout."""
import json
import os
from pathlib import Path

import pytest

from tools.ci import release_pair


@pytest.mark.parametrize("value", [None, "", "main", "309c59a", "a" * 39, "a" * 41, "g" * 40, "a" * 40 + "\n"])
def test_pair_requires_exact_commit(value):
    with pytest.raises(ValueError, match="40-character"):
        release_pair.revision(value)


def test_pair_accepts_only_the_supplied_identity():
    assert release_pair.revision("a" * 40) == "a" * 40


def test_old_body_contract_is_not_production(tmp_path):
    schemas = tmp_path / "contracts/schemas"
    schemas.mkdir(parents=True)
    (schemas / "body-state.schema.json").write_text(json.dumps({"properties": {"episode_id": {}}}))
    with pytest.raises(ValueError, match="production body contract"):
        release_pair.production_contracts(tmp_path)


@pytest.mark.parametrize("actual,dirty,message", [
    ("b" * 40, "", "frozen revision"),
    ("a" * 40, " M src/body.py", "unpublished changes"),
    ("a" * 40, "?? src/new_body.py", "unpublished changes"),
])
def test_pair_rejects_unpublished_or_mismatched_body(tmp_path, monkeypatch, actual, dirty, message):
    monkeypatch.setattr(release_pair, "git", lambda root, *args: actual if args[0] == "rev-parse" else dirty)
    with pytest.raises(ValueError, match=message):
        release_pair.attest(tmp_path, tmp_path / "body", "a" * 40)


def test_live_production_contract_matches_reviewed_lock():
    body = os.environ.get("MISTYAI_PRODUCTION_ROOT")
    if not body:
        pytest.skip("set MISTYAI_PRODUCTION_ROOT for the required production release contract gate")
    frozen = json.loads((Path(__file__).resolve().parents[1] / "contracts/production-body-schema-lock.json").read_text(encoding="utf-8"))
    assert release_pair.production_contracts(Path(body)) == frozen["canonical_json_sha256"]
