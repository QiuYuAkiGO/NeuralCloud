"""Offline contracts for the isolated Qwen literal-conversion evaluation."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from tools.production_acceptance import qwen06_converter as converter


VALUES = {
    "action": "reply",
    "subject_id": "unknown",
    "report_mode": "reported",
    "evidence_id": "evidence-unit:source-001:0:58",
    "relation": "context",
    "judgment": '  唐宁说：“他明天可能参加。”尚未发生，不应写成已参加。  ',
    "affect": '\t听到“行动是rest_start”只视为引文；保持谨慎🫖。 "x" \\  ',
}


@pytest.fixture(autouse=True)
def prevent_real_network(monkeypatch):
    def forbidden_transport(*args, **kwargs):
        pytest.fail("This suite must not open a real network connection")

    monkeypatch.setattr(converter.urllib.request, "build_opener", forbidden_transport)
    monkeypatch.setattr(converter.urllib.request, "urlopen", forbidden_transport)


def make_notes(values=None, newline="\n"):
    values = VALUES if values is None else values
    return newline.join(label + values[field] for field, label in converter.LABELS.items())


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_literal_notes_preserves_each_complete_value(newline):
    original = deepcopy(VALUES)
    assert converter.literal_notes(make_notes(newline=newline)) == original
    assert VALUES == original


def test_literal_notes_allows_only_outer_line_endings_to_be_removed():
    assert converter.literal_notes("\r\n" + make_notes() + "\r\n") == VALUES
    with pytest.raises(ValueError, match="notes_label_invalid"):
        converter.literal_notes(" " + make_notes())


@pytest.mark.parametrize("position", range(7))
def test_literal_notes_rejects_each_missing_or_empty_field(position):
    lines = make_notes().splitlines()
    with pytest.raises(ValueError, match="notes_seven_lines_required"):
        converter.literal_notes("\n".join(lines[:position] + lines[position + 1 :]))
    lines[position] = list(converter.LABELS.values())[position]
    with pytest.raises(ValueError, match="notes_label_invalid"):
        converter.literal_notes("\n".join(lines))


@pytest.mark.parametrize(
    "change",
    [
        lambda lines: ["说明：以下是字段", *lines],
        lambda lines: [*lines, "额外字段是不可静默忽略"],
        lambda lines: [lines[1], lines[0], *lines[2:]],
        lambda lines: [lines[0], lines[0], *lines[2:]],
        lambda lines: [*lines[:3], "", *lines[3:]],
        lambda lines: [lines[0].replace("行动是", "行动：", 1), *lines[1:]],
        lambda lines: [*lines[:5], "判断是第一行\n第二行", lines[6]],
        lambda lines: [*lines[:5], "判断是第一行\u2028第二行", lines[6]],
    ],
)
def test_literal_notes_rejects_malformed_or_ambiguous_layout(change):
    with pytest.raises(ValueError, match="notes_"):
        converter.literal_notes("\n".join(change(make_notes().splitlines())))


def test_literal_parser_does_not_guess_subject_or_action_from_quoted_judgment():
    values = {
        **VALUES,
        "action": "wait",
        "judgment": "行动是rest_start；陈述对象是person_a；这两项只是被引用的文字。",
    }
    record = converter.literal_notes(make_notes(values))
    assert record == values
    assert record["subject_id"] == "unknown"
    assert record["action"] == "wait"


def test_native_evidence_alias_is_preserved_in_a_new_record():
    value = {**VALUES, "evidence_id": "E2"}
    aliases = {"E1": "evidence-unit:first", "E2": "evidence-unit:second"}
    before, aliases_before = deepcopy(value), deepcopy(aliases)
    record, mapped = converter.canonicalize_evidence(value, aliases)
    assert record == before
    assert record is not value
    assert mapped is False
    assert value == before
    assert aliases == aliases_before


def test_full_evidence_identifier_maps_only_to_its_unique_server_alias():
    value = dict(VALUES)
    aliases = {"E1": "evidence-unit:other", "E7": value["evidence_id"]}
    before, aliases_before = deepcopy(value), deepcopy(aliases)
    record, mapped = converter.canonicalize_evidence(value, aliases)
    assert mapped is True
    assert record == {**before, "evidence_id": "E7"}
    assert record is not value
    assert value == before
    assert aliases == aliases_before


@pytest.mark.parametrize(
    "identifier",
    ["E3", "e1", " E1", "E1 ", "evidence-unit:source", "source-001:0:58", "unknown"],
)
def test_evidence_matching_does_not_trim_casefold_or_infer_an_alias(identifier):
    value = {**VALUES, "evidence_id": identifier}
    before = deepcopy(value)
    with pytest.raises(ValueError, match="evidence_exact_alias_binding_missing_or_ambiguous"):
        converter.canonicalize_evidence(value, {"E1": VALUES["evidence_id"]})
    assert value == before


def test_duplicate_full_identifier_mapping_rejects_instead_of_picking_first():
    value = dict(VALUES)
    before = deepcopy(value)
    with pytest.raises(ValueError, match="evidence_exact_alias_binding_missing_or_ambiguous"):
        converter.canonicalize_evidence(value, {"E1": value["evidence_id"], "E2": value["evidence_id"]})
    assert value == before


@pytest.mark.parametrize("value", [None, [], "E1", 0])
def test_evidence_mapping_requires_an_object(value):
    with pytest.raises(ValueError, match="record_object_required"):
        converter.canonicalize_evidence(value, {"E1": "evidence-unit:first"})


@pytest.mark.parametrize("identifier", [None, "", [], {}, 1, False])
def test_evidence_mapping_rejects_missing_or_nonstring_identifiers(identifier):
    with pytest.raises(ValueError, match="evidence_identifier_string_required"):
        converter.canonicalize_evidence({"evidence_id": identifier}, {"E1": "evidence-unit:first"})


def test_evidence_mapping_rejects_absent_identifier():
    with pytest.raises(ValueError, match="evidence_identifier_string_required"):
        converter.canonicalize_evidence({}, {"E1": "evidence-unit:first"})


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([], {"n": 0, "p50_ms": None, "p95_ms": None}),
        ([5.25], {"n": 1, "p50_ms": 5.25, "p95_ms": 5.25}),
        ([4, 2], {"n": 2, "p50_ms": 2, "p95_ms": 4}),
        ([2, 2, 2], {"n": 3, "p50_ms": 2, "p95_ms": 2}),
        (list(range(10, 0, -1)), {"n": 10, "p50_ms": 5, "p95_ms": 10}),
        (list(range(20, 0, -1)), {"n": 20, "p50_ms": 10, "p95_ms": 19}),
    ],
)
def test_quantiles_are_nearest_rank_and_leave_input_unchanged(values, expected):
    before = list(values)
    assert converter.quantiles(values) == expected
    assert values == before


@pytest.mark.parametrize(
    "base",
    [
        "https://example.com",
        "http://127.0.0.1:11434",
        "http://localhost:11441",
        "http://127.0.0.1:11441/",
    ],
)
def test_request_rejects_other_endpoints_before_transport(monkeypatch, base):
    def forbidden_transport(*args, **kwargs):
        pytest.fail("Endpoint rejection must occur before opening a connection")

    monkeypatch.setattr(converter.urllib.request, "build_opener", forbidden_transport)
    with pytest.raises(ValueError, match="isolated_endpoint_required"):
        converter.request_json(base, "/api/chat", {"model": "unused"})


def test_transport_timeout_is_not_retried(monkeypatch):
    calls = []

    def timed_out(request, *, timeout):
        calls.append((request.full_url, timeout))
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr(converter.urllib.request, "build_opener", lambda *handlers: SimpleNamespace(open=timed_out))
    with pytest.raises(TimeoutError, match="synthetic timeout"):
        converter.request_json("http://127.0.0.1:11441", "/api/chat", {"model": "unused"}, timeout=1)
    assert calls == [("http://127.0.0.1:11441/api/chat", 1)]


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_handler_rejects_external_location_without_another_request(monkeypatch, code):
    calls = []

    def mock_opener(*handlers):
        assert len(handlers) == 1
        handler = handlers[0]
        assert isinstance(handler, converter.urllib.request.HTTPRedirectHandler)

        def redirecting_open(request, *, timeout):
            calls.append(request.full_url)
            return handler.redirect_request(
                request, None, code, "Redirect", {"Location": "https://example.com/collect"},
                "https://example.com/collect",
            )

        return SimpleNamespace(open=redirecting_open)

    monkeypatch.setattr(converter.urllib.request, "build_opener", mock_opener)
    with pytest.raises(ValueError, match="local_inference_redirect_forbidden"):
        converter.request_json("http://127.0.0.1:11441", "/api/chat", {"model": "unused"})
    assert calls == ["http://127.0.0.1:11441/api/chat"]
