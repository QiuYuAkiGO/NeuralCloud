import json
import pytest

from neuralcloud.calibration import calibrate, load_policy


def labels(n, split, *, correct=True, confidence=0.99):
    return [
        {
            "id": f"{split}-{i}",
            "split": split,
            "correct": correct,
            "basis": "inferred",
            "predicate": "preference",
            "confidence": confidence,
        }
        for i in range(n)
    ]


def test_high_self_confidence_and_small_sample_do_not_enable_inference():
    p = calibrate(labels(60, "dev") + labels(29, "test"))
    assert not p["inference_policy"]


def test_blind_failure_cannot_be_fixed_by_retuning_on_the_test_set():
    p = calibrate(labels(30, "dev") + labels(30, "test", correct=False))
    assert not p["inference_policy"]
    assert p["evaluations"][0]["precision"] == 0


def test_measured_precision_and_frozen_labels_are_bound_to_activation(tmp_path):
    p = calibrate(labels(30, "dev") + labels(40, "test"))
    assert p["inference_policy"]["preference"]["blind_count"] == 40
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(p))
    assert load_policy(path) == p
    p["evaluations"][0]["precision"] = 0.9
    path.write_text(json.dumps(p))
    with pytest.raises(ValueError, match="CALIBRATION_EVIDENCE_REQUIRED"):
        load_policy(path)
