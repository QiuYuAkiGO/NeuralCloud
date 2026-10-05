"""Offline structural evidence for the memory API cache research.

Run from the repository root with its Python environment. Uses synthetic test
fixtures and temporary databases only; does not call a model or read API keys.
Character counts are NOT provider token counts or observed cache hit rates.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from harness.memory.qq_assimilation import QQAssimilationQueue, QQMemoryProposer, _SYSTEM, _json
from harness.memory.shared import SharedMemoryStore
from harness.memory.revision.config import DailyRevisionConfig
from harness.memory.revision.provider import DailyRevisionProvider, _encoded
from harness.memory.revision.models import strict_json_schema
from harness.memory.revision.queue import DailyRevisionQueue, digest
from tests.test_qq_assimilation import make_job, old_impression, proposal
from tests.test_daily_revision_provider import source


def common(a, b):
    return next((index for index, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))


def ordered(value, keys):
    # Preserve top-level order without changing canonicalization within values.
    assert set(keys) == set(value)
    return "{" + ",".join(_json(key) + ":" + _json(value[key]) for key in keys) + "}"


def split_schema(payload):
    schema = deepcopy(payload["schema"])
    definitions = schema["$defs"]
    episode = definitions["QQEpisodeProposal"]["properties"]
    impression = definitions["QQImpressionProposal"]["properties"]
    source_ids = episode["source_ids"]["items"].pop("enum")
    assert impression["source_ids"]["items"].pop("enum") == source_ids
    bindings = {"source_ids": source_ids, "subject_refs": impression["subject_refs"]["items"].pop("enum"),
                "impression_kinds": impression["kind"].pop("enum")}
    restored = deepcopy(schema)
    for name in ("QQEpisodeProposal", "QQImpressionProposal"):
        restored["$defs"][name]["properties"]["source_ids"]["items"]["enum"] = bindings["source_ids"]
    restored["$defs"]["QQImpressionProposal"]["properties"]["subject_refs"]["items"]["enum"] = bindings["subject_refs"]
    restored["$defs"]["QQImpressionProposal"]["properties"]["kind"]["enum"] = bindings["impression_kinds"]
    assert restored == payload["schema"]
    return {**payload, "schema": schema, "schema_bindings": bindings}


class CaptureQQ(QQMemoryProposer):
    def __init__(self, job, *, repair=False):
        super().__init__(None)
        self.job = job
        self.repair = repair
        self.payloads = []

    def _generate(self, payload):
        self.payloads.append(deepcopy(payload))
        if self.repair and len(self.payloads) == 1:
            return {"episodes": [], "impressions": []}
        return proposal(self.job).model_dump(mode="json")


class CaptureDaily(DailyRevisionProvider):
    def __init__(self, config, queue):
        super().__init__(config, queue, client=object())
        self.calls = []

    def _call(self, run_id, stage, model, system, value, deadline, *, validate=None):
        self.calls.append({"stage": stage, "system": system, "user": _encoded(value),
                           "schema": strict_json_schema(model)})
        if stage.startswith("clean:"):
            result = model.model_validate({"cleaned": [{"source_ids": [row["source_id"]],
                "disposition": "keep", "summary": row["text"], "evidence_kind": "speaker_claim", "reason": ""}
                for row in value["sources"]]})
        elif stage == "synthesize":
            result = model.model_validate({})
        else:
            result = model.model_validate({"approved": True, "reasons": [],
                "reviewed_source_ids": [row["source_id"] for row in value["sources"]]})
        if validate:
            validate(result)
        return result


def run():
    with TemporaryDirectory(prefix="nc-memory-cache-probe-") as folder:
        root = Path(folder)
        qq = []
        persona = {"identity": {"name": "Synthetic Persona", "traits": ["careful", "concise"]}, "version": "fixture-v1"}
        for index in (1, 2):
            store = SharedMemoryStore(root / f"qq-{index}.sqlite3")
            queue = QQAssimilationQueue(store.path, owner_id="misty", preset_id="active", self_id="999",
                allowed_group_ids=("100", "101"))
            job = make_job((store, queue), suffix=f"sample-{index}", text=f"合成资料 {index}", message_id=str(index))
            capture = CaptureQQ(job, repair=index == 1)
            capture.propose(job, old_impressions=[old_impression()], persona=persona)
            qq.append(capture)
        first, second = (item.payloads[0] for item in qq)
        keys = ["persona", "conversation_kind", "conversation_id", "group_id", "self_id", "schema",
                "old_impressions", "frozen_sources"]
        baseline = [_json(first), _json(second)]
        candidate = [ordered(first, keys), ordered(second, keys)]
        assert all(json.loads(a) == json.loads(b) for a, b in zip(baseline, candidate))
        split = [split_schema(value) for value in (first, second)]
        split_keys = ["schema", "persona", "conversation_kind", "conversation_id", "group_id", "self_id",
                      "old_impressions", "schema_bindings", "frozen_sources"]
        split_encoded = [ordered(value, split_keys) for value in split]
        repaired = _json(qq[0].payloads[1])
        extra = {key: value for key, value in qq[0].payloads[1].items() if key not in first}
        appended_messages = [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": baseline[0]},
                             {"role": "user", "content": _json(extra)}]
        assert appended_messages[1]["content"] == baseline[0]

        queue = DailyRevisionQueue(root / "daily.sqlite3")
        records = [source("sample-one"), source("sample-two", "用户说喜欢绘画")]
        job = queue.enqueue(owner_id="misty", preset_id="active", day="2026-09-19", partition="local",
            sources=records, basis={})
        config = DailyRevisionConfig(chunk_input_tokens=512)
        daily = CaptureDaily(config, queue)
        daily.propose(job["run_id"], records, {"persona": persona, "existing": {}, "active_revision_ids": []})
        calls = daily.calls
        one = calls[0]
        payload = {"model": config.model, "messages": [{"role": "system", "content": one["system"]},
            {"role": "user", "content": one["user"]}], "max_tokens": config.max_completion_tokens,
            "reasoning_effort": config.reasoning_effort,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "daily_revision_CleanedBatch", "strict": True, "schema": one["schema"]}}}
        def cache_key(conf):
            from harness.memory.revision.provider import PROMPT_VERSION
            return digest({"version": PROMPT_VERSION,
                "config": conf.model_dump(mode="json", exclude={"api_key_file", "queue_path"}),
                "endpoint": conf.base_url, "payload": payload})
        queue.reserve(job["run_id"], "synthetic-request", "2026-09-20", 200, None)
        queue.account("synthetic-request", {"prompt_tokens": 100, "completion_tokens": 10,
            "total_tokens": 110, "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20})
        with closing(sqlite3.connect(queue.path)) as db:
            retained_usage = json.loads(db.execute(
                "SELECT usage_json FROM daily_revision_usage WHERE request_hash=?", ("synthetic-request",)).fetchone()[0])
        report = {
            "schema_version": "memory-cache-research-v1", "date": "2026-09-20",
            "data": "synthetic fixtures only", "live_model_calls": 0,
            "metric_warning": "Character-level structural comparisons only; not provider tokens or actual hit rates.",
            "qq": {
                "current_top_level_key_order": list(json.loads(baseline[0])),
                "candidate_top_level_key_order": keys,
                "system_characters": len(_SYSTEM),
                "baseline_user_characters": len(baseline[0]),
                "current_two_job_user_common_prefix_characters": common(*baseline),
                "candidate_two_job_user_common_prefix_characters": common(*candidate),
                "candidate_preserves_all_values": True,
                "schema_split_user_common_prefix_characters": common(*split_encoded),
                "schema_split_user_characters": len(split_encoded[0]),
                "schema_split_static_schema_characters": len(_json(split[0]["schema"])),
                "schema_split_static_schema_equal": split[0]["schema"] == split[1]["schema"],
                "schema_split_reconstructs_original_schema": True,
                "current_repair_user_common_prefix_characters": common(baseline[0], repaired),
                "append_repair_preserves_original_messages": True,
                "dynamic_schema_identical_across_jobs": first["schema"] == second["schema"],
            },
            "daily": {
                "stage_shapes": [{"stage": c["stage"], "system_characters": len(c["system"]),
                    "user_characters": len(c["user"]), "user_key_order": list(json.loads(c["user"])),
                    "schema_characters": len(_encoded(c["schema"]))} for c in calls],
                "clean_synthesize_system_common_prefix_characters": common(calls[0]["system"], calls[2]["system"]),
                "schedule_only_change_invalidates_current_stage_cache": cache_key(config) != cache_key(
                    config.model_copy(update={"schedule_time": "04:00"})),
                "usage_retained_after_input_with_80_hit_20_miss": retained_usage,
                "provider_cache_fields_retained": "prompt_cache_hit_tokens" in retained_usage,
            },
        }
        return report


if __name__ == "__main__":
    result = run()
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if len(sys.argv) == 2:
        Path(sys.argv[1]).write_text(rendered, encoding="utf-8")
    print(rendered)
