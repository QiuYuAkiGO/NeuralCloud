from __future__ import annotations

import base64
import io
import json
from types import SimpleNamespace

from PIL import Image
import pytest

from harness.qq.grounding import GroundingError, inspect_qq_frame


def reading():
    return {"scene": "group_chat", "blocked": False,
            "group_title": {"display_name": "合成群(2)", "rect": {"x": 20, "y": 80, "width": 140, "height": 20}},
            "input_box": None, "send_button": None, "visible_draft": "",
            "messages": [], "opened_profile": None, "uncertainties": []}


def image():
    target = io.BytesIO()
    Image.new("RGB", (1280, 720), "white").save(target, format="PNG")
    return target.getvalue()


def test_actual_image_block_and_external_frame_binding():
    calls = []

    class LLM:
        def complete_chat_result(self, messages, **kwargs):
            calls.append(messages)
            return SimpleNamespace(text=json.dumps(reading()))

    png = image()
    result = inspect_qq_frame(LLM(), png, frame_id="synthetic-frame", evidence_ref="synthetic-ref")
    assert result.frame_id == "synthetic-frame" and result.evidence_ref == "synthetic-ref"
    uri = calls[0][1]["content"][1]["image_url"]["url"]
    assert base64.b64decode(uri.partition(",")[2]) == png
    assert "synthetic-frame" not in str(calls) and "synthetic-ref" not in str(calls)
    assert "角色、等级小标签不是昵称" in calls[0][0]["content"]


@pytest.mark.parametrize("fault", ["out_of_frame", "zero_size", "negative", "string_coordinate", "unknown_field", "fake_message_id", "wrong_profile_scene"])
def test_invalid_coordinates_or_model_authority_fail_closed_legacy_mode(legacy_json_mode, fault):
    data = reading()
    if fault == "out_of_frame":
        data["group_title"]["rect"]["x"] = 1279
    elif fault == "zero_size":
        data["group_title"]["rect"]["width"] = 0
    elif fault == "negative":
        data["group_title"]["rect"]["y"] = -1
    elif fault == "string_coordinate":
        data["group_title"]["rect"]["x"] = "20"
    elif fault == "unknown_field":
        data["action"] = "send"
    elif fault == "wrong_profile_scene":
        data["scene"] = "member_profile"
    else:
        data["messages"] = [{"direction": "incoming", "kind": "text", "display_name": "合成成员", "text": "合成文本",
                             "avatar": None, "bubble": None, "message_id": "invented", "uncertainties": []}]

    class LLM:
        def complete_chat_result(self, *args, **kwargs):
            return SimpleNamespace(text=json.dumps(data))

    with pytest.raises(GroundingError, match="qq_geometry_schema_"):
        inspect_qq_frame(LLM(), image(), frame_id="synthetic-frame", evidence_ref="synthetic-ref")


def test_profile_prompt_omits_background_and_leaves_room_for_model_reasoning():
    data = reading()
    data.update(scene="group_profile", group_title=None, visible_draft=None,
                opened_profile={"kind": "group", "display_name": "合成群", "group_id": "10001", "member_id": None,
                                "card": {"x": 300, "y": 140, "width": 500, "height": 360},
                                "close": None, "uncertainties": []})

    class LLM:
        def complete_chat_result(self, messages, **kwargs):
            prompt = messages[0]["content"]
            assert "资料卡场景必须messages=[]、input_box=null、send_button=null、visible_draft=null" in prompt
            assert "资料卡遮住背景聊天是正常情况" in prompt
            assert "10001" not in prompt
            assert kwargs["max_tokens"] == 16384
            return SimpleNamespace(text=json.dumps(data))

    result = inspect_qq_frame(LLM(), image(), frame_id="card", evidence_ref="synthetic-ref")
    assert result.scene == "group_profile" and result.opened_profile.group_id == "10001"
    assert result.messages == [] and result.visible_draft is None


def test_truncated_profile_response_is_never_repaired_or_used_legacy_mode(legacy_json_mode, ):
    class LLM:
        def complete_chat_result(self, *args, **kwargs):
            return SimpleNamespace(text='{"scene":"group_profile","blocked":false,"opened_profile":{"group_id":"10001')

    with pytest.raises(GroundingError, match="qq_geometry_schema_json_invalid"):
        inspect_qq_frame(LLM(), image(), frame_id="card", evidence_ref="synthetic-ref")


@pytest.mark.parametrize("fault,kind,field", [
    ("bounds", "qq_rects_in_frame", "group_title.rect"),
    ("scene", "qq_coherent_scene", "opened_profile.kind"),
    ("private_extra_key", "extra_forbidden", "item"),
])
def test_schema_diagnostics_contain_only_types_and_fixed_field_names_legacy_mode(legacy_json_mode, fault, kind, field):
    data = reading()
    if fault == "bounds":
        data["group_title"]["rect"]["x"] = 1279
    elif fault == "scene":
        data["scene"] = "member_profile"
    else:
        data["synthetic-private-raw-field"] = "synthetic-private-raw-value"

    class LLM:
        def complete_chat_result(self, *args, **kwargs):
            return SimpleNamespace(text=json.dumps(data))

    with pytest.raises(GroundingError) as caught:
        inspect_qq_frame(LLM(), image(), frame_id="card", evidence_ref="synthetic-ref")
    assert caught.value.diagnostics == ({"field": field, "type": kind},)
    assert "synthetic-private" not in str(caught.value) + repr(caught.value.diagnostics)
    assert kind.removeprefix("qq_") in str(caught.value)


def test_roster_schema_defaults_and_self_indicator_prompt_are_explicit():
    from harness.qq.grounding import PROMPT, ScreenReading

    assert ScreenReading.model_validate(reading()).visible_members == []
    assert "查看我的QQ空间" in PROMPT and "查看他的QQ空间" in PROMPT
    data = reading()
    data["visible_members"] = [{"display_name": "Synthetic(备注)", "row": {"x": 1100, "y": 500, "width": 170, "height": 30},
                                "avatar": None, "uncertainties": []}]
    result = ScreenReading.model_validate(data)
    assert result.visible_members[0].display_name == "Synthetic(备注)"


@pytest.mark.parametrize("text,category,verified", [
    ("查看我的QQ空间", "none", "own_space_link"),
    ("查看我的QQ空间", "own_space_link", "own_space_link"),
    ("查看他的QQ空间", "own_space_link", "none"),
    ("查看她的QQ空间", "own_space_link", "none"),
    ("查看TA的QQ空间", "own_space_link", "none"),
    ("QQ空间", "own_space_link", "none"),
    ("编辑备注", "edit_own_profile", "none"),
    ("修改群昵称", "edit_own_profile", "none"),
    ("昵称：查看我的QQ空间", "own_space_link", "none"),
    (None, "own_space_link", "none"),
])
def test_self_marker_depends_on_exact_control_text_not_model_category(text, category, verified):
    from harness.qq.grounding import OpenedProfile

    profile = OpenedProfile(kind="member", display_name="合成名称", member_id="20001",
                            self_indicator=category, self_indicator_text=text,
                            card={"x": 760, "y": 360, "width": 320, "height": 350}, close=None, uncertainties=[])
    assert profile.verified_self_indicator == verified
