"""Focused checks for measurement correctness; no downloaded models required."""
from collections import OrderedDict
import importlib.util
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from clm_benchmark import TransformersEmbedder, percentile, prediction, replace_report, summarize


class MeasurementTests(unittest.TestCase):
    def test_scanner_sharing_violation_retries_only_file_replace(self):
        with patch("clm_benchmark.Path.replace", side_effect=[PermissionError(), PermissionError(), None]) as replace:
            with patch("clm_benchmark.time.sleep"):
                replace_report("report.tmp", "report.json")
        self.assertEqual(replace.call_count, 3)

    def test_failed_requests_are_not_fast_successes(self):
        result = summarize([{"status": 200, "elapsed_ms": 12},
                            {"status": 502, "elapsed_ms": 1},
                            {"status": None, "elapsed_ms": 180000},
                            {"status": 200, "elapsed_ms": 20}])
        self.assertEqual((result["attempts"], result["successes"], result["failures"]), (4, 2, 2))
        self.assertEqual(result["p95_ms"], 20)
        self.assertIsNone(summarize([])["p50_ms"])
        self.assertEqual(percentile(list(range(1, 101)), .95), 95)

    def test_score_label_is_mode_not_rounded_expectation(self):
        answer = {"type": "score", "score": 1.1, "probabilities": {"0": .4, "1": .1, "2": .5}}
        self.assertEqual(prediction(answer), 2)


@unittest.skipUnless(importlib.util.find_spec("torch"), "requires torch; exercised in evaluation environment")
class PoolingTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        import torch

        class Batch(dict):
            def to(self, device):
                return self

        class Tokenizer:
            def __call__(self, texts, **kwargs):
                ids = [[int(t) for t in text.split()] for text in texts]
                if "return_tensors" not in kwargs:
                    return {"input_ids": ids}
                width = max(map(len, ids))
                return Batch(input_ids=torch.tensor([x + [0] * (width - len(x)) for x in ids]),
                             attention_mask=torch.tensor([[1] * len(x) + [0] * (width - len(x)) for x in ids]))

        class Model:
            calls = 0

            def __call__(self, input_ids, attention_mask, use_cache):
                self.calls += 1
                hidden = torch.stack((input_ids.float(), torch.ones_like(input_ids).float()), dim=-1)
                return types.SimpleNamespace(last_hidden_state=hidden)

        self.embedder = TransformersEmbedder.__new__(TransformersEmbedder)
        self.embedder.np, self.embedder.torch, self.embedder.device = np, torch, "cpu"
        self.embedder.tokenizer, self.embedder.model = Tokenizer(), Model()
        self.embedder.lock = threading.RLock()
        self.embedder.cache = OrderedDict()
        self.embedder.cache_limit, self.embedder.batch, self.embedder.max_tokens = 2, 2, 4
        self.embedder.forward_calls = self.embedder.encoded_tokens = 0
        self.embedder.fail = False
        self.fake_module = types.ModuleType("clm.embedder")
        self.fake_module.EmbedderError = RuntimeError

    def test_last_nonpad_pooling_cache_and_eviction(self):
        import numpy as np
        with patch.dict(sys.modules, {"clm.embedder": self.fake_module}):
            vectors, spent = self.embedder.embed(["2 3", "5", "2 3"])
            np.testing.assert_allclose(vectors, [[3 / np.sqrt(10), 1 / np.sqrt(10)],
                                                  [5 / np.sqrt(26), 1 / np.sqrt(26)],
                                                  [3 / np.sqrt(10), 1 / np.sqrt(10)]], rtol=1e-6)
            self.assertEqual(spent, 3)
            self.assertEqual(self.embedder.embed(["2 3"])[1], 0)
            self.assertEqual(self.embedder.model.calls, 1)
            self.embedder.embed(["7"])
            self.assertNotIn("5", self.embedder.cache)

    def test_overlength_rejected_before_any_forward(self):
        with patch.dict(sys.modules, {"clm.embedder": self.fake_module}):
            with self.assertRaisesRegex(ValueError, "no truncation"):
                self.embedder.embed(["1", "1 2 3 4 5"])
            self.assertEqual(self.embedder.model.calls, 0)
            self.assertFalse(self.embedder.cache)


if __name__ == "__main__":
    unittest.main()
