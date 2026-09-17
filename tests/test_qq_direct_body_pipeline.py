"""QQ keeps compilation and validation while avoiding redundant decisions."""
import pytest

from harness.config import Settings
from harness.core.inference import ContextSegment, MemoryEvidenceContext, ValidationResult
from harness.pipeline.pipeline import InferencePipeline
from harness.memory.service import build_inmemory_service


@pytest.fixture
def service():
    memory = build_inmemory_service(settings=Settings(call_log_enabled=False, record_interaction=False))
    try:
        yield memory
    finally:
        memory.close()


@pytest.mark.parametrize("validator_enabled", [False, True])
def test_qq_direct_body_validates_once_without_repair_or_retrieval(service, monkeypatch, validator_enabled):
    class RejectingValidator:
        calls = 0

        def should_run(self, path):
            return True

        def check(self, candidate, **kwargs):
            self.calls += 1
            return ValidationResult(verdict="revise", violations=["invented_memory"], severity=1)

    validator = RejectingValidator()
    settings = Settings(validator_enabled=validator_enabled, max_revisions=3,
                        continuation_enabled=True, max_reply_segments=3, record_interaction=True)
    pipeline = InferencePipeline(service, settings=settings, validator=validator)
    evidence = MemoryEvidenceContext(audience="qq:group:10001")
    before = pipeline.session_store.snapshot()

    def forbidden(*args, **kwargs):
        pytest.fail("QQ direct entry parsed, retrieved, interpreted, or observed emotion")

    for obj, method in ((pipeline.parser, "parse"), (pipeline.gate, "evaluate"),
                        (pipeline.interpreter, "interpret"), (pipeline.state_builder, "build"),
                        (pipeline, "_observe_emotion"), (service, "query")):
        monkeypatch.setattr(obj, method, forbidden)
    final = pipeline.prepare_qq_response("固定正文指令", session_id="qq-group-draft:10001",
        context_segments=[ContextSegment(kind="user", text="本轮问题")], memory_evidence=evidence,
        prefix_messages=[{"role": "system", "content": "人设约束"}, {"role": "user", "content": "可信范围的数据"},
                         {"role": "assistant", "content": '{"action":"speak"}'}])
    assert final.usage.calls == 1 and final.revisions == 0 and final.stopped_by == "disabled"
    assert validator.calls == int(validator_enabled)
    assert final.validation.verdict == ("revise" if validator_enabled else "pass")
    assert final.validation.skipped is (not validator_enabled)
    assert pipeline.session_store.snapshot() == before
    assert settings.max_revisions == 3 and settings.continuation_enabled


def test_qq_direct_body_rejects_unscoped_evidence_before_model(service):
    pipeline = InferencePipeline(service)
    with pytest.raises(ValueError, match="scoped evidence"):
        pipeline.prepare_qq_response("固定指令", session_id="qq-group-draft:10001", context_segments=[],
            memory_evidence=MemoryEvidenceContext(audience="local:private"), prefix_messages=[])
