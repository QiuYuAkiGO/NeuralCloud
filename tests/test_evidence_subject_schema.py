"""The schema sent to models must reject the same missing/ambiguous subjects."""
from copy import deepcopy

from jsonschema import Draft202012Validator
import pytest
from pydantic import ValidationError

from harness.cognitive.production import ProductionAppraisal, ProductionChoice
from harness.memory.evidence_models import ClaimProposal


def _claim(**subject):
    # Synthetic original and IDs: no production conversation is needed to
    # reproduce the failed proposal shape.
    return {
        **subject,
        "topic": "drink",
        "statement": "The person reports preferring tea.",
        "kind": "self_report",
        "report_mode": "direct",
        "facet": "preference",
        "anchors": [{
            "unit_id": "synthetic:utterance",
            "start": 0,
            "end": 13,
            "quote": "I prefer tea.",
            "role": "supports",
            "rationale": "Explicit present preference in the original utterance.",
        }],
    }


def _payload(model, claim):
    if model is ClaimProposal:
        return claim
    value = {"memory_updates": [{"group_id": "synthetic:group", "claims": [claim]}]}
    if model is ProductionChoice:
        value.update(kind="reply", reason="Respond to the incoming message.", text="Understood.")
    return value


MODELS = (ClaimProposal, ProductionAppraisal, ProductionChoice)
VALID_SUBJECTS = (
    {"subject_id": "qq:user:synthetic"},
    {"subject_id": "qq:user:synthetic", "subject_mention": None},
    {"subject_mention": "the visitor"},
    {"subject_id": None, "subject_mention": "the visitor"},
)
INVALID_SUBJECTS = (
    {},
    {"subject_id": None},
    {"subject_mention": None},
    {"subject_id": None, "subject_mention": None},
    {"subject_id": "qq:user:synthetic", "subject_mention": "the visitor"},
    {"subject_id": ""},
    {"subject_id": "s" * 161},
    {"subject_id": 123},
    {"subject_id": False},
    {"subject_mention": ""},
    {"subject_mention": "s" * 2001},
    {"subject_mention": 123},
    {"subject_mention": False},
)


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("mode", ("validation", "serialization"))
@pytest.mark.parametrize("subject", VALID_SUBJECTS)
def test_subject_choice_accepts_known_ids_and_unresolved_mentions(model, mode, subject):
    schema = model.model_json_schema(mode=mode)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    payload = _payload(model, _claim(**subject))
    validator.validate(payload)
    parsed = model.model_validate(payload)
    # Serialized defaults include the unused subject as null; they must also
    # satisfy the same schema and preserve the subject without guessing.
    dumped = parsed.model_dump(mode="json")
    validator.validate(dumped)
    parsed_claim = parsed if model is ClaimProposal else parsed.memory_updates[0].claims[0]
    assert parsed_claim.subject_id == subject.get("subject_id")
    assert parsed_claim.subject_mention == subject.get("subject_mention")


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("mode", ("validation", "serialization"))
@pytest.mark.parametrize("subject", INVALID_SUBJECTS)
def test_subject_choice_rejects_missing_ambiguous_or_invalid_subjects(model, mode, subject):
    payload = _payload(model, _claim(**subject))
    validator = Draft202012Validator(model.model_json_schema(mode=mode))
    assert not validator.is_valid(payload)
    with pytest.raises(ValidationError):
        model.model_validate(payload)


@pytest.mark.parametrize("model", MODELS)
def test_generated_schema_preserves_subject_length_limits(model):
    # Exercise the upper bounds as well as their rejection above, ensuring the
    # added alternatives do not accidentally narrow Identifier or Text.
    validator = Draft202012Validator(model.model_json_schema())
    for subject in ({"subject_id": "s" * 160}, {"subject_mention": "s" * 2000}):
        payload = _payload(model, _claim(**subject))
        validator.validate(payload)
        model.model_validate(payload)


@pytest.mark.parametrize("model", MODELS)
def test_subject_schema_keeps_frozen_strict_model_configuration(model):
    payload = _payload(model, _claim(subject_id="qq:user:synthetic"))
    parsed = model.model_validate(payload)
    parsed_claim = parsed if model is ClaimProposal else parsed.memory_updates[0].claims[0]
    with pytest.raises(ValidationError, match="frozen_instance"):
        parsed_claim.subject_id = "qq:user:other"
    claim = _claim(subject_id="qq:user:synthetic")
    claim["invented_subject_authority"] = "trusted"
    payload = _payload(model, claim)
    assert not Draft202012Validator(model.model_json_schema()).is_valid(payload)
    with pytest.raises(ValidationError, match="extra_forbidden"):
        model.model_validate(payload)


@pytest.mark.parametrize("model", (ProductionAppraisal, ProductionChoice))
@pytest.mark.parametrize("proposal_kind", ("initial_self_report", "preference_correction"))
def test_reported_speaker_and_supersession_do_not_supply_missing_subject(model, proposal_kind):
    # Synthetic variations on a missing subject, not captured model responses.
    # Neither a reported speaker nor a superseded record may silently supply
    # the subject of a self-report or preference correction.
    claim = _claim()
    claim["reported_speaker_id"] = "qq:user:synthetic"
    if proposal_kind == "preference_correction":
        claim["supersedes"] = [{"record_id": "synthetic:old-preference", "expected_revision": 1}]
    payload = _payload(model, claim)
    unchanged = deepcopy(payload)
    assert not Draft202012Validator(model.model_json_schema()).is_valid(payload)
    with pytest.raises(ValidationError, match="claim_requires_subject_or_unbound_mention"):
        model.model_validate(payload)
    assert payload == unchanged
