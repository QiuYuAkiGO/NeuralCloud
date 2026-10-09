"""Bind host-supplied web observations to the same body segment the model sees."""
import hashlib
import json

from harness.core.inference import MemoryEvidenceEntry


def web_entries(context, *, audience):
    if context is None:
        return []
    from harness.cognitive.web import normalize_web_context
    context = normalize_web_context(context)
    entries = []
    for source in context["sources"]:
        for field, channel in (("snippet", "web_search_snippet"), ("content", "web_read_content")):
            if source.get(field):
                entries.append(MemoryEvidenceEntry(source_id=source["source_ref"]+":"+field, text=source[field],
                    audience=audience, source_channel=channel, source_conversation_id=audience,
                    source_refs=[source["source_ref"], source["url"], *context["evidence_refs"]],
                    observed_at=source["retrieved_at"], status="provisional"))
    for failure in context["failures"]:
        code = failure.get("code") if isinstance(failure, dict) else None
        if isinstance(code, str) and code:
            # This establishes an execution result, not truth of missing page content.
            identifier = hashlib.sha256(json.dumps(failure, sort_keys=True).encode()).hexdigest()
            entries.append(MemoryEvidenceEntry(source_id="web-failure:"+identifier, text=code,
                audience=audience, source_channel="web_execution_failure", source_conversation_id=audience,
                source_refs=context["evidence_refs"], status="retained_fact"))
    return entries
