"""A three-bubble answer is one sample only after all delivery evidence agrees."""
from copy import deepcopy
from datetime import timedelta
import hashlib

import pytest

from harness.qq import acceptance as module
from tests.test_qq_acceptance import setup as setup, add_turn, start, complete_samples


def segmented_sources(config):
    trace = add_turn(config, 0, latency=27)
    sources = module._sources(config)
    event = next(iter(sources["events"].values()))
    decision = sources["decisions"][trace["decision_id"]]
    send = next(iter(sources["sends"].values()))
    receipt = next(iter(sources["receipts"].values()))
    texts = ["嗯，记得呀。", "我们在试自动收发。", "还会一起看耗时。"]
    body = "\n\n".join(texts)
    digest = hashlib.sha256(body.encode()).hexdigest()
    decision.update(reply=body, reply_segments=texts)
    first = module._date(trace["first_seen_at"])
    send.update(segment_count=3, reply_sha256=digest, receipt_id=receipt["receipt_id"],
                run_id=event["run_id"], deadline_at=(first + timedelta(seconds=60)).isoformat())
    receipt.update(delivered_text=body, delivered_segments=texts,
                   segment_receipt_ids=[f"segment-receipt:{i}" for i in range(3)],
                   evidence_refs=[f"segment-evidence:{i}" for i in range(3)])
    send["evidence_refs"] = receipt["evidence_refs"]
    sources["segments"], sources["segment_receipts"] = {}, {}
    for index, text in enumerate(texts):
        proof = deepcopy(send["preclick_proof"])
        started = first + timedelta(seconds=3 + index * 6)
        child_id, receipt_id = f"child:{index}", f"segment-receipt:{index}"
        proof.update(reservation_id=child_id, reply_sha256=hashlib.sha256(text.encode()).hexdigest(),
                     verified_at=started.isoformat(), source_record_ids=[event["source_record_id"]])
        child = dict(child_id=child_id, parent_id=send["reservation_id"], group_id=event["group_id"],
                     run_id=event["run_id"], event_id=event["event_id"], decision_id=decision["decision_id"],
                     segment_index=index, segment_count=3, text=text, reply_sha256=proof["reply_sha256"],
                     plan_sha256=digest, status="sent", receipt_id=receipt_id, proof=proof,
                     click_started_at=started.isoformat(), click_acknowledged_at=(started + timedelta(seconds=.2)).isoformat(),
                     attempt_count=1, deadline_at=send["deadline_at"], evidence_refs=[f"segment-evidence:{index}"])
        item = dict(receipt_id=receipt_id, decision_id=decision["decision_id"], segment_index=index, segment_count=3,
                    group_id=event["group_id"], self_member_id=send["self_member_id"], delivered_text=text,
                    evidence_refs=child["evidence_refs"], source_record_ids=[event["source_record_id"]],
                    verification="qq_ui_verified", delivered_at=(started + timedelta(seconds=2)).isoformat())
        sources["segments"][child_id] = child
        sources["segment_receipts"][receipt_id] = item
        sources["attempts"][child_id] = [dict(reservation_id=child_id, phase="sent", proof=proof)]
    return trace, event, sources


def test_three_bubbles_count_once_at_final_durable_receipt(setup, monkeypatch):
    start(setup)
    trace, event, sources = segmented_sources(setup)
    assert module._link_delivery(trace, event, sources)[2] == 27
    monkeypatch.setattr(module, "_sources", lambda config: sources)
    complete_samples(setup)
    result = module.report_run(setup.run_dir)
    assert result["counts"]["warm_speak"] == 1
    assert result["latency_seconds"]["warm_speak"]["max"] == 27
    assert result["turns"][0]["segment_count"] == 3


@pytest.mark.parametrize("fault", ["missing_receipt", "wrong_body", "wrong_identity", "missing_ack", "extra_click", "new_deadline", "late_click", "out_of_order", "early_aggregate"])
def test_incomplete_or_inconsistent_segment_chain_never_counts_success(setup, fault):
    trace, event, sources = segmented_sources(setup)
    child = sources["segments"]["child:1"]
    item = sources["segment_receipts"]["segment-receipt:1"]
    if fault == "missing_receipt":
        del sources["segment_receipts"]["segment-receipt:2"]
    elif fault == "wrong_body":
        item["delivered_text"] = "别的正文"
    elif fault == "wrong_identity":
        item["self_member_id"] = "33333"
    elif fault == "missing_ack":
        child["click_acknowledged_at"] = None
    elif fault == "extra_click":
        child["attempt_count"] += 1
        sources["attempts"][child["child_id"]] *= 2
    elif fault == "new_deadline":
        child["deadline_at"] = (module._date(child["deadline_at"]) + timedelta(seconds=60)).isoformat()
    elif fault == "late_click":
        child["click_started_at"] = child["deadline_at"]
    elif fault == "out_of_order":
        child["proof"]["verified_at"] = trace["first_seen_at"]
    else:
        next(iter(sources["receipts"].values()))["delivered_at"] = item["delivered_at"]
    with pytest.raises(module.AcceptanceError):
        module._link_delivery(trace, event, sources)
