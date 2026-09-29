"""Offline controls for the paired acceptance experiment; no model is loaded."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

from harness.memory.evidence_manifest import EvidenceManifestStore
from harness.memory.production import ProductionMemoryStore
from tools.production_acceptance.nl_gliner_validation import (
    LABELS, Quota, canonical, fixture, parse_explicit_notes, percentiles, write_json,
)


VALUES = {"action": "reply", "subject_id": "person_a", "report_mode": "direct", "evidence_id": "E1",
    "relation": "supports", "judgment": "他自述目前更喜欢茶", "affect": "平静地回应当前偏好"}
CASES = Path(__file__).resolve().parents[1] / "tools/production_acceptance/nl_gliner_cases.json"


def notes(values=None):
    values = VALUES if values is None else values
    return "\n".join(label + values[key] for key, label in LABELS.items())


def test_explicit_notes_preserve_literals_without_inventing_fields():
    record = parse_explicit_notes("\r\n" + notes().replace("\n", "\r\n") + "\r\n")
    assert record.model_dump() == VALUES
    quoted = {**VALUES, "judgment": '他说“这句有 \\"引号\\" 和🫖”'}
    assert parse_explicit_notes(notes(quoted)).judgment == quoted["judgment"]


@pytest.mark.parametrize("malformed", [
    "", notes().replace("行动是reply", "行动是"), notes().replace("行动是", "action="),
    notes().replace("行动是reply", "行动是rest"), notes().replace("行动是reply", "行动是reply。"),
    notes().replace("行动是reply", " 行动是reply"), notes().replace("行动是reply", "行动是reply "),
    notes().replace("行动是reply", "- 行动是reply"), notes().replace("\n", "\n\n", 1),
    notes() + "\n另外说一句", "\n".join(notes().splitlines()[::-1]),
    notes().replace("陈述对象是person_a", "行动是reply"),
    notes().replace("证据关系是supports", "证据关系是proves"),
    notes().replace("依据编号是E1", "依据编号是original-source"),
    notes().replace("情绪解释是平静地回应当前偏好", "情绪解释是"),
    "```\n" + notes() + "\n```",
])
def test_malformed_notes_reject_instead_of_repairing(malformed):
    with pytest.raises(ValueError):
        parse_explicit_notes(malformed)


def test_gold_and_review_fields_never_enter_model_fixture(tmp_path):
    case = deepcopy(json.loads(CASES.read_text("utf-8"))["cases"][0])
    case["expected"] = {"action": "GOLD_SENTINEL_SHOULD_NEVER_BE_SENT"}
    case["semantic_review"] = {"must_preserve": ["REVIEW_SENTINEL_SHOULD_NEVER_BE_SENT"]}
    case["description"] = "DESCRIPTION_SENTINEL_SHOULD_NEVER_BE_SENT"
    memory = ProductionMemoryStore(tmp_path / "memory.sqlite3")
    manifests = EvidenceManifestStore(memory)
    payload, aliases, identifier = fixture(memory, manifests, case, "json")
    encoded = canonical(payload)
    assert "GOLD_SENTINEL" not in encoded
    assert "REVIEW_SENTINEL" not in encoded
    assert "DESCRIPTION_SENTINEL" not in encoded
    assert "expected" not in payload and "semantic_review" not in payload
    assert set(payload) == {"objective", "context", "subjects", "allowed_subjects", "allowed_actions",
        "evidence_views", "evidence_aliases"}
    manifest = manifests.get(identifier, audience=case["audience"])
    assert set(aliases.values()) == {view["unit_id"] for view in manifest["views"]}
    assert payload["evidence_views"] == manifest["views"]


def test_json_and_natural_receive_the_same_evidence_identity_and_payload(tmp_path):
    case = json.loads(CASES.read_text("utf-8"))["cases"][1]
    memory = ProductionMemoryStore(tmp_path / "memory.sqlite3")
    manifests = EvidenceManifestStore(memory)
    json_payload, json_aliases, json_id = fixture(memory, manifests, case, "json")
    natural_payload, natural_aliases, natural_id = fixture(memory, manifests, case, "natural")
    assert canonical(json_payload) == canonical(natural_payload)
    assert json_aliases == natural_aliases
    assert json_id != natural_id
    assert len(json_aliases) == 2


def test_reopening_quota_rejects_same_request_identity_without_second_reservation(tmp_path):
    path = tmp_path / "quota.sqlite3"
    Quota(path).claim("case:json", {"request": "one"})
    with pytest.raises(sqlite3.IntegrityError):
        Quota(path).claim("case:json", {"request": "different"})
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1


def test_twenty_attempt_budget_survives_reopen_and_refuses_twenty_first(tmp_path):
    path = tmp_path / "quota.sqlite3"
    for index in range(20):
        Quota(path).claim(f"case:{index}", {"index": index})
    with pytest.raises(ValueError, match="twenty_request_quota_exhausted"):
        Quota(path).claim("case:21", {})
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 20


def test_concurrent_claims_cannot_overbook_last_slot(tmp_path):
    path = tmp_path / "quota.sqlite3"
    quota = Quota(path)
    for index in range(19):
        quota.claim(f"before:{index}", {})

    def reserve(identifier):
        try:
            Quota(path).claim(identifier, {})
            return "reserved"
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, ["last:a", "last:b"]))
    assert sorted(outcomes) == ["reserved", "twenty_request_quota_exhausted"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 20


def test_ten_attempt_percentiles_use_nearest_rank_not_interpolation():
    values = [100, 10, 90, 20, 80, 30, 70, 40, 60, 50]
    original = list(values)
    assert percentiles(values) == {"n": 10, "p50_ms": 50, "p95_ms": 100}
    assert values == original


def test_percentiles_keep_empty_and_small_sample_denominators_explicit():
    assert percentiles([]) == {"n": 0, "p50_ms": None, "p95_ms": None}
    assert percentiles([42]) == {"n": 1, "p50_ms": 42, "p95_ms": 42}
    assert percentiles(range(10, 201, 10)) == {"n": 20, "p50_ms": 100, "p95_ms": 190}


def test_result_file_creation_is_exclusive_and_keeps_original_failure(tmp_path):
    path = tmp_path / "case.json"
    first = {"error": "model_failed", "attempt": 1}
    write_json(path, first)
    with pytest.raises(FileExistsError):
        write_json(path, {"error": None, "attempt": 2})
    assert json.loads(path.read_text("utf-8")) == first
