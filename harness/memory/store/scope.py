"""Hard memory boundaries shared by repositories and the writer."""

from harness.core.models import CandidateMemory, MemoryScope


def visible_to(scope: MemoryScope | None, audience: str) -> bool:
    # Old presets have no scope. Their records remain private to the owner.
    if scope is None:
        return audience == "local:main"
    if scope.channel == "qq_private" and audience != "local:main":
        # A mutable/corrupt visibility list must not grant private experiences
        # to a group, another peer, or the same peer on another QQ account.
        if audience != scope.qq_private_audience:
            return False
    return scope.owner_id == "misty" and audience in scope.visibility


def candidate_matches(
    row: CandidateMemory, *, subject: str | None, predicate: str | None,
    value: str | None, claim: str, scope: MemoryScope | None,
) -> bool:
    # Equal wording does not establish that two speakers or predicates match.
    if row.scope != scope or row.subject != subject or row.predicate != predicate:
        return False
    if value and row.value:
        return row.value == value
    return bool((value and (row.value == value or value in row.claim))
                or (claim and claim == row.claim))
