"""Explicit remember requests retain the original attributed utterance."""
from contextlib import closing
import json
import re


_REQUEST = re.compile(
    r"(?:^|[，,。.!！?？\n])\s*(?:(?:请你?|帮我|麻烦你|你要)?(?:永远)?记(?:住|下)(?!了吗|吗|了没)|"
    r"(?:please\s+)?remember\s+(?:that|this|my|our|I\b)|(?:please\s+)?keep\s+this\s+in\s+mind)",
    re.IGNORECASE,
)


def retain_explicit_request(memory, record_id):
    """Run only on an authenticated incoming source, never generated prose.

    This handles unambiguous direct request forms before any optional model
    runs, including limited dialogue while the body is unavailable. Other
    important content remains eligible for a model proposal with its evidence.
    """
    with closing(memory._connect()) as db:
        row = db.execute("SELECT payload,revoked FROM pm_sources WHERE id=?", (record_id,)).fetchone()
    if row is None or row["revoked"]:
        return False
    source = json.loads(row["payload"])
    if source["kind"] != "observation" or source["author_id"].startswith("character:"):
        return False
    if not _REQUEST.search(source["text"]):
        return False
    memory.retain(record_id, event_id="explicit-remember:" + record_id, pinned=True, evidence_ids=[record_id])
    return True
