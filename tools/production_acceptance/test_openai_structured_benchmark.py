"""Ensure refusal/truncation cannot masquerade as usable structured responses."""
import json
import unittest

from openai_structured_benchmark import access_blocked, decode_final, payload_for
from deepseek_structured_benchmark import make_payload, summary


class OpenAIMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.request = {"state": "frozen", "questions": {"decision": {"type": "choice", "criteria": {"a": "A", "b": "B"}}}}

    def test_provider_changes_preserve_task_and_schema(self):
        original, schema = make_payload(self.request, "deepseek-flash", "none", "json_schema")
        for effort in ["none", "low"]:
            body, actual = payload_for(self.request, "test", effort)
            self.assertEqual(body["input"], original["input"])
            self.assertEqual(body["instructions"], original["instructions"])
            self.assertEqual(actual, schema)
            self.assertTrue(body["text"]["format"]["strict"])
            self.assertFalse(body["store"])
            self.assertEqual("temperature" in body, effort == "none")

    def test_truncated_json_and_refusal_are_failures(self):
        _, schema = payload_for(self.request, "test", "none")
        for status, parts in [
            ("incomplete", [{"type": "output_text", "text": '{"decision":"a"}'}]),
            ("completed", [{"type": "refusal", "refusal": "declined"}]),
            ("completed", [{"type": "output_text", "text": '{"decision":"unknown"}'}]),
        ]:
            row = decode_final({"status": status, "output": [{"type": "message", "content": parts}]}, schema)
            row["elapsed_ms"] = 1
            self.assertEqual(summary([row])["usable"], 0)

    def test_complete_json_is_usable_without_reasoning_storage(self):
        _, schema = payload_for(self.request, "test", "none")
        row = decode_final({"status": "completed", "output": [
            {"type": "reasoning", "summary": [{"text": "hidden"}]},
            {"type": "message", "content": [{"type": "output_text", "text": '{"decision":"a"}'}]}]}, schema)
        self.assertTrue(row["completed"] and row["schema_valid"])
        self.assertNotIn("hidden", json.dumps(row))

    def test_http_200_terminal_access_error_stops_run(self):
        _, schema = payload_for(self.request, "test", "none")
        for code in ["rate_limit_exceeded", "credit_balance_exhausted", "insufficient_quota"]:
            row = decode_final({"status": "failed", "error": {"code": code}}, schema)
            row["status"] = 200
            self.assertTrue(access_blocked(row))
            self.assertFalse(row["completed"])
        self.assertFalse(access_blocked({"status": 200, "provider_error": None}))


if __name__ == "__main__":
    unittest.main()
