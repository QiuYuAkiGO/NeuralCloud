"""Checks for API comparison schema, SSE completion, and failure accounting."""
import json
import unittest

from jsonschema import Draft202012Validator, ValidationError

from deepseek_structured_benchmark import make_payload, output_text, parse_sse, summary


class StructuredMeasurementTests(unittest.TestCase):
    def test_choice_schema_rejects_extra_fields_and_unknown_actions(self):
        request = {"state": "a", "questions": {"decision": {"type": "choice", "criteria": {"yes": "Y", "no": "N"}}}}
        body, schema = make_payload(request, "deepseek-flash", "none", "json_schema")
        validator = Draft202012Validator(schema)
        validator.validate({"decision": "yes"})
        for value in [{"decision": "maybe"}, {"decision": "yes", "explanation": "x"}, {}]:
            with self.assertRaises(ValidationError):
                validator.validate(value)
        self.assertEqual(json.loads(body["input"]), {"state": "a", "question": request["questions"]["decision"]})
        self.assertEqual(body["reasoning"], {"effort": "none"})

    def test_sse_keepalives_multiline_and_final_frame(self):
        events = list(parse_sse([": ping", "", "event: response.output_text.delta", 'data: {"type":',
                                'data: "response.output_text.delta", "delta":"中"}', "",
                                'data: {"type":"response.completed", "response":{"status":"completed"}}']))
        self.assertEqual(events[0]["delta"], "中")
        self.assertEqual(events[-1]["response"]["status"], "completed")

    def test_reasoning_is_not_scored_as_output(self):
        response = {"output": [{"type": "reasoning", "content": [{"type": "reasoning_text", "text": "private"}]},
                               {"type": "message", "content": [{"type": "output_text", "text": '{"decision":true}'}]}]}
        self.assertEqual(output_text(response), '{"decision":true}')

    def test_fast_error_and_incomplete_json_do_not_lower_latency(self):
        result = summary([{"schema_valid": False, "completed": False, "elapsed_ms": 1},
                          {"schema_valid": True, "completed": False, "elapsed_ms": 5},
                          {"schema_valid": True, "completed": True, "elapsed_ms": 100, "ttft_ms": 50}])
        self.assertEqual(result["usable"], 1)
        self.assertEqual(result["failures"], 2)
        self.assertEqual(result["p50_ms"], 100)


if __name__ == "__main__":
    unittest.main()
