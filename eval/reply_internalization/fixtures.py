"""Adapters only for isolated evaluation. Tool evidence never pretends to be a real paid call."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import sqlite3

from common import digest, save


class Fixtures:
    def __init__(self, decision, settings, job, scenario, preset, arm, config):
        from harness.qq import vision_tool
        from harness.core.inference import TokenUsage
        from harness.qq.memory import QQVisualMemoryStore
        self.decision, self.scenario, self.preset = decision, scenario, preset
        self.group = "private:29999:20003" if scenario.get("private") else "10001"
        self.self_id, self.member = "29999", "20003"
        self.state, self.start = job / "simulation.sqlite3", datetime.now(timezone.utc)
        with sqlite3.connect(self.state) as db:
            db.execute("CREATE TABLE expression_evaluation_marker (simulation_only INTEGER NOT NULL)")
            db.execute("INSERT INTO expression_evaluation_marker VALUES (1)")
        self.store = QQVisualMemoryStore(self.state)
        self.ref = "qq-protocol://sha256/" + digest({"simulation": scenario["id"]})
        self.image, self.requests = None, []
        self.aliases = [decision.QQVerifiedSelfAlias(group_id=self.group, self_member_id=self.self_id, alias="Misty",
            evidence_refs=[self.ref], verification="qq_protocol_verified")]
        cache, configuration = job / "fixture-images", job / "fixture-vision.json"
        cache.mkdir()
        save(configuration, {"enabled": True, "api_key": "unused-frozen-fixture", "image_cache_dir": str(cache)})
        settings.qq_vision_tool_config = configuration
        def invoke(tool, request, *, deadline, trace, request_validator=None, **kwargs):
            tool.check_attention()
            if request_validator:
                request_validator()
            if not set(request.image_ids) <= {i.image_id for i in tool.images}:
                raise ValueError("fixture_selection_invalid")
            self.requests.append({"question": request.question, "image_ids": request.image_ids, "kind": "frozen_fixture"})
            trace.add_counter("fixture_vision_calls")
            if self.image is None or self.image.get("error"):
                return {"tool": "inspect_qq_images", "status": "unavailable", "images": [], "untrusted": True,
                    "error": (self.image or {}).get("error", "fixture_missing")}, TokenUsage()
            selected = [i for i in tool.images if i.image_id in request.image_ids]
            result = {"tool": "inspect_qq_images", "status": "ok", "untrusted": True,
                "images": [{"image_id": i.image_id, **self.image} for i in selected],
                "evidence": [i.public() for i in selected]}
            vision_tool.VisionAnswer.model_validate({"images": result["images"]})
            return result, TokenUsage()
        vision_tool.QQVisionTool.invoke = invoke
        if arm == "prompt_only":
            # Same candidate source, old schema/dataflow, only shared wording changes.
            original = decision._gate_rules
            decision._gate_rules = lambda *a, **kw: original(*a, **kw) + config["expression_rules"]
            from harness.validator import validator
            validator.SEMANTIC_RULES += config["expression_rules"]
        for i, text in enumerate(scenario.get("history", []), 1):
            self.observation(text, i, historical=True)
        history = scenario.get("delivered_history", [])
        for offset in range(0, len(history), 2):
            row = self.observation(history[offset], 20 + offset, historical=True)
            reply = history[offset+1]
            identifier = digest({"seed": scenario["id"], "offset": offset})
            seed = decision.QQReplyDecision(decision_id="qq-decision:" + identifier[:32], input_hash=identifier,
                created_at=self.start-timedelta(seconds=180-offset), action="speak", reason="隔离历史夹具",
                reply=reply, reply_segments=[reply], decision_source="model", outcome="ready", group_id=self.group,
                self_member_id=self.self_id, trigger_member_id=self.member, participants=[self.member],
                source_record_ids=[row.record_id], new_observation_record_ids=[row.record_id], evidence_refs=[self.ref],
                delivery_refs=[], model="fixture-not-generation", persona_version=preset.persona.version, preset_id=preset.preset_id)
            with closing(decision._connect(self.state)) as db, db:
                db.execute("INSERT INTO qq_reply_decisions VALUES(?,?,?,?,?)", (seed.decision_id, seed.input_hash,
                    self.group, "completed", seed.model_dump_json()))
            self.delivered(seed, offset+1, [row], self.start-timedelta(seconds=170-offset))

    def observation(self, text, serial, *, historical=False, image=None):
        from harness.qq.memory import QQVerifiedProfileInput, QQVisualObservationInput, protocol_message_evidence_ref
        author = text.get("speaker", self.member) if isinstance(text, dict) else self.member
        text = text["text"] if isinstance(text, dict) else text
        stamp = self.start+timedelta(seconds=serial-3600) if historical else datetime.now(timezone.utc)
        evidence = "qq-protocol://sha256/" + digest({"text": text, "serial": serial})
        ref = protocol_message_evidence_ref(self.group, author, str(10000+serial))
        image_ref = "qq-direct-image:sha256:" + digest(image) if image is not None else None
        profile = self.store.record_protocol_profile(QQVerifiedProfileInput(group_id=self.group, member_id=author,
            display_name="隔离测试参与者", display_name_source="qq_metadata", verification="qq_protocol_verified",
            capture_id=f"fixture:profile:{serial}", observed_at=stamp, group_evidence=[evidence], member_evidence=[evidence])).profile
        return self.store.record_protocol_observation(QQVisualObservationInput(group_id=self.group, member_id=author,
            profile_record_id=profile.record_id, capture_id=f"fixture:event:{serial}", capture_item_id=f"fixture:item:{serial}",
            observed_at=stamp, text=text, evidence=[evidence, ref, *([image_ref] if image_ref else [])],
            author_attribution="qq_protocol_verified", attribution_evidence=[evidence, ref],
            extraction_method="isolated_simulation_only", message_id=str(10000+serial), message_id_source="qq_metadata",
            image_analysis=([{"image_index": 1, "status": "unknown", "evidence_ref": image_ref,
                             "uncertainties": ["等待模型选择识图"]}] if image_ref else []))).observation

    def guidance(self, index):
        from harness.emotion.models import EmotionGuidance
        from harness.qq.conversation import conversation_audience
        if not self.scenario.get("guidance"):
            return None
        return EmotionGuidance(text=self.scenario["guidance"], context_id="fixture:guidance",
            scope=conversation_audience(self.group), revision=index, status="model")

    def delivered(self, result, index, rows, timestamp=None):
        from harness.qq.memory import protocol_message_evidence_ref
        refs, ids = [], []
        stamp = timestamp or datetime.now(timezone.utc)
        for segment, text in enumerate(self.decision.effective_reply_segments(result)):
            identifier = str(90000+index*10+segment)
            receipt = self.decision.record_qq_protocol_segment_delivery(memory_db=self.state,
                group_id=self.group, self_member_id=self.self_id, decision_id=result.decision_id,
                segment_index=segment, segment_count=len(result.reply_segments), delivered_text=text,
                evidence_refs=[self.ref, protocol_message_evidence_ref(self.group, self.self_id, identifier)],
                source_record_ids=[o.record_id for o in rows], protocol_message_id=identifier, delivered_at=stamp)
            refs.extend(receipt.evidence_refs)
            ids.append(identifier)
        if ids:
            self.decision.record_qq_protocol_delivery(memory_db=self.state, group_id=self.group, self_member_id=self.self_id,
                decision_id=result.decision_id, delivered_text=result.reply, evidence_refs=list(set(refs)),
                protocol_message_ids=ids, source_record_ids=[o.record_id for o in rows], delivered_at=stamp)
