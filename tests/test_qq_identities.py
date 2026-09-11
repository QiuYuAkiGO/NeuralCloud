from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from harness.qq.identities import IdentityScope, SessionIdentityCache, VerifiedIdentity, VisualHint


NOW = datetime(2026, 9, 11, 1, tzinfo=timezone.utc)


def scope():
    return IdentityScope("fictional-login-1", "fictional-body-1", 2, "0x100", "10001", "20001")


def hint(name="虚构成员", digest="a"):
    return VisualHint(name, digest * 64)


def identity(member="30001", capture="fictional-card-1"):
    return VerifiedIdentity(member, "qq-profile:fictional-1", capture, NOW, "虚构资料昵称",
                            ["test-evidence://card"], ["test-evidence://chat"],
                            ["test-evidence://card", "test-evidence://chat"])


def test_first_bind_and_hit_preserve_old_card_time_and_all_evidence():
    cache, s, h, person = SessionIdentityCache(), scope(), hint(), identity()
    assert cache.resolve(s, h, [h]) is None
    assert cache.last_reason == "not_found"
    assert cache.record(s, h, person, [h])
    assert cache.resolve(s, h, [h, h]) is person  # Multiple messages by the same visual author are allowed.
    assert cache.last_reason == "cache_hit"
    later = replace(person, observed_at=NOW + timedelta(hours=1), capture_id="fictional-card-later")
    assert cache.record(s, h, later, [h])
    assert cache.resolve(s, h, [h]) is person
    assert cache.resolve(s, h, [h]).observed_at == NOW
    assert person.evidence_refs == ("test-evidence://card", "test-evidence://chat")


@pytest.mark.parametrize("field,value", [
    ("login_epoch", "fictional-login-2"), ("body_instance_id", "fictional-body-2"),
    ("environment_revision", 3), ("window_id", "0x101"), ("group_id", "10002"), ("self_member_id", "20002"),
])
def test_every_scope_component_is_required_for_reuse(field, value):
    cache, s, h = SessionIdentityCache(), scope(), hint()
    assert cache.record(s, h, identity(), [h])
    assert cache.resolve(replace(s, **{field: value}), h, [h]) is None


def test_visible_same_name_different_avatars_block_lookup_and_record():
    cache, s, first, second = SessionIdentityCache(), scope(), hint(), hint(digest="b")
    assert cache.record(s, first, identity(), [first])
    assert cache.resolve(s, first, [first, second]) is None
    assert cache.last_reason == "visible_name_collision"
    assert not cache.record(s, second, identity("30002"), [first, second])
    assert len(cache) == 1
    assert cache.resolve(s, first, [first]) is not None


def test_exact_hint_two_verified_qq_accounts_permanently_conflicts_until_invalidate():
    cache, s, h = SessionIdentityCache(), scope(), hint()
    assert cache.record(s, h, identity(), [h])
    assert not cache.record(s, h, identity("30002"), [h])
    assert cache.last_reason == "permanent_identity_conflict"
    assert cache.resolve(s, h, [h]) is None
    assert not cache.record(s, h, identity(), [h])
    cache.invalidate()
    assert len(cache) == 0
    assert cache.record(s, h, identity("30002"), [h])


def test_verified_conflict_is_retained_even_during_visible_name_collision():
    cache, s, h, other = SessionIdentityCache(), scope(), hint(), hint(digest="b")
    cache.record(s, h, identity(), [h])
    assert not cache.record(s, h, identity("30002"), [h, other])
    assert cache.resolve(s, h, [h]) is None
    assert cache.last_reason == "permanent_identity_conflict"


@pytest.mark.parametrize("changed", [hint("虚构改名"), hint(digest="b"), hint(" 虚构成员"), hint("虚构成员🙂")])
def test_name_and_avatar_are_exact_and_changes_do_not_merge(changed):
    cache, s, h = SessionIdentityCache(), scope(), hint()
    cache.record(s, h, identity(), [h])
    assert cache.resolve(s, changed, [changed]) is None


def test_absent_hint_does_not_create_or_resolve_binding():
    cache, s, h = SessionIdentityCache(), scope(), hint()
    assert not cache.record(s, h, identity(), [])
    assert cache.last_reason == "hint_not_visible"
    cache.record(s, h, identity(), [h])
    assert cache.resolve(s, h, []) is None


def test_capacity_keeps_conflict_tombstone_and_does_not_evict_existing_people():
    cache, s, first, second, third = SessionIdentityCache(max_entries=2), scope(), hint(), hint("乙"), hint("丙")
    cache.record(s, first, identity(), [first])
    cache.record(s, first, identity("30002"), [first])
    cache.record(s, second, identity("30003"), [second])
    assert not cache.record(s, third, identity("30004"), [third])
    assert cache.last_reason == "capacity_exhausted"
    assert len(cache) == 2
    assert cache.resolve(s, first, [first]) is None
    assert cache.resolve(s, second, [second]).member_id == "30003"
    assert not cache.record(s, first, identity(), [first])


def test_new_cache_has_no_bindings_and_instances_cannot_share():
    first, second, s, h = SessionIdentityCache(), SessionIdentityCache(), scope(), hint()
    first.record(s, h, identity(), [h])
    assert second.resolve(s, h, [h]) is None
    first.invalidate()
    assert first.resolve(s, h, [h]) is None


def test_inputs_and_returned_bindings_are_deeply_immutable():
    source = ["test-evidence://card"]
    person = replace(identity(), profile_evidence_refs=source)
    source.clear()
    assert person.profile_evidence_refs == ("test-evidence://card",)
    for model, field, value in [(person, "member_id", "30002"), (scope(), "group_id", "10002"),
                                (hint(), "display_name", "other")]:
        with pytest.raises(FrozenInstanceError):
            setattr(model, field, value)


@pytest.mark.parametrize("field,value", [
    ("login_epoch", ""), ("body_instance_id", "a" * 257), ("window_id", "a\n"),
    ("environment_revision", True), ("environment_revision", -1), ("group_id", "01001"),
    ("self_member_id", 20001), ("group_id", "1" * 21),
])
def test_bad_scope_rejected(field, value):
    with pytest.raises(ValueError):
        replace(scope(), **{field: value})


@pytest.mark.parametrize("field,value", [
    ("display_name", "  "), ("display_name", "x" * 257), ("display_name", "a\x00b"),
    ("avatar_sha256", "a" * 63), ("avatar_sha256", "A" * 64), ("avatar_sha256", None),
])
def test_bad_visual_hint_rejected(field, value):
    with pytest.raises(ValueError):
        replace(hint(), **{field: value})


@pytest.mark.parametrize("field,value", [
    ("member_id", "unknown"), ("profile_record_id", ""), ("capture_id", "\t"),
    ("observed_at", NOW.replace(tzinfo=None)), ("observed_at", "2026-09-11"),
    ("profile_evidence_refs", []), ("identity_evidence_refs", "not-a-list"),
    ("evidence_refs", ["x"] * 17), ("profile_evidence_refs", ["a" * 2049]),
    ("identity_evidence_refs", [None]), ("evidence_refs", [" "]),
    ("evidence_refs", ["test-evidence://card"]), ("identity_evidence_refs", ["bad\nreference"]),
])
def test_bad_or_incomplete_verified_evidence_rejected(field, value):
    with pytest.raises(ValueError):
        replace(identity(), **{field: value})


@pytest.mark.parametrize("limit", [0, 257, True, 1.0, "256"])
def test_bad_capacity_rejected(limit):
    with pytest.raises(ValueError):
        SessionIdentityCache(max_entries=limit)


@pytest.mark.parametrize("visible", [None, "name", [None], [hint()] * 257])
def test_bad_visible_list_rejected_without_mutating_cache(visible):
    cache, s, h = SessionIdentityCache(), scope(), hint()
    with pytest.raises(ValueError):
        cache.record(s, h, identity(), visible)
    assert len(cache) == 0
    with pytest.raises(ValueError):
        cache.resolve(s, h, visible)


def test_identity_and_scope_objects_required_not_model_dicts():
    cache, s, h = SessionIdentityCache(), scope(), hint()
    with pytest.raises(ValueError):
        cache.resolve(s.__dict__, h, [h])
    with pytest.raises(ValueError):
        cache.record(s, h, identity().__dict__, [h])
    assert len(cache) == 0
