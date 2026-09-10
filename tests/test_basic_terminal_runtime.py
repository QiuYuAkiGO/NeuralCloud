"""Production Runtime/Cognitive GUI loop with a deterministic desktop, not Linux acceptance."""
from __future__ import annotations

import asyncio
import json
import re
import threading

import pytest

from harness.cognitive.basic_terminal import DEFAULT_TERMINAL_APP_ID as APP, GUI_TERMINAL_INTENTS
from harness.embodiment.manager import EmbodimentManager
from harness.embodiment.providers.simulator import DeterministicEmbodimentSimulator, SimulatorConfig
from harness.research.manifest import ExperimentManifest
from harness.runtime import ActionCoordinator, ActionExecutionState, PassiveHomeostasisAdapter, RuntimeSupervisor
from harness.runtime.perception import PerceptionGuard
from tests.test_runtime_supervisor import _manifest, _pipeline_adapter


REQUEST = "请打开 Linux 终端执行 pwd，并核验实际输出。"


class Desktop(DeterministicEmbodimentSimulator):
    def __init__(self, *, fail=False):
        super().__init__(SimulatorConfig(scripted_outcomes={"ui.key_chord.v1": "failure"} if fail else {}))
        self.opened = False
        self.typed = ""
        self.output = ""
        self.executed = []
        self.verified = []

    def supported_intents(self):
        return GUI_TERMINAL_INTENTS

    async def observe(self, *, now=None):
        snapshot = await super().observe(now=now)
        window = {"app_id": APP, "window_id": "window-1", "focused": True}
        node = {"app_id": APP, "role": "terminal", "name": "Terminal", "text": self.output}
        return snapshot.model_copy(update={"extensions": {"mistyai.desktop.v1": {
            "observation_id": f"obs-{snapshot.revision}",
            "active_window": window if self.opened else None,
            "windows": [window] if self.opened else [],
            "a11y_nodes": [node] if self.opened else [],
            "a11y_ref": snapshot.evidence_refs[0].ref,
        }}})

    async def execute(self, execution):
        intent = execution.embodied_action.intent
        self.executed.append(intent.intent_id)
        result = await super().execute(execution)
        if result.success:
            if intent.intent_id == "app.open.v1":
                self.opened = True
            elif intent.intent_id == "ui.type_text.v1":
                self.typed = intent.arguments["text"]
            elif intent.intent_id == "ui.key_chord.v1":
                marker = re.search(r"NC_TERMINAL_EXIT_[0-9a-f]+", self.typed).group()
                begin = re.search(r"NC_TERMINAL_BEGIN_[0-9a-f]+", self.typed).group()
                self.output = begin + "\n/home/misty-body\n" + marker + ":0\n"
        return result

    async def verify(self, execution):
        self.verified.append(execution.embodied_action.intent.intent_id)
        return await super().verify(execution)


class PlannerModel:
    def __init__(self, *, block=False):
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        if not block:
            self.release.set()

    def complete_chat(self, messages, **kwargs):
        self.calls.append(messages)
        self.entered.set()
        assert self.release.wait(5)
        context = json.loads(messages[-1]["content"])
        observed, history = context["observation"], context["history"]
        intents = [item["intent_id"] for item in history]
        criteria = [{"kind": "window", "app_id": APP, "condition": "present_and_focused"}]
        if "ui.key_chord.v1" in intents:
            return json.dumps({"kind": "complete", "observed_text": "/home/misty-body", "evidence_ref": observed["terminal_text_evidence_ref"]})
        if "ui.type_text.v1" in intents:
            intent_id, arguments = "ui.key_chord.v1", {"keys": ["enter"]}
        elif observed["windows"]:
            intent_id = "ui.type_text.v1"
            arguments = {"selector": {"observation_id": observed["observation_id"], "app_id": APP, "role": "terminal", "name": "Terminal"}, "text": "pwd"}
            criteria = [{"kind": "a11y_node", "app_id": APP, "role": "terminal", "name": "Terminal", "condition": "present"}]
        else:
            intent_id, arguments = "app.open.v1", {"app_id": APP}
        return json.dumps({"kind": "act", "intent": {"intent_id": intent_id, "arguments": arguments}, "success_criteria": criteria})


def runtime(tmp_path, *, fail=False, block=False, enabled=True):
    _, pipeline, adapter = _pipeline_adapter(tmp_path)
    model = PlannerModel(block=block)
    pipeline.llm = model
    payload = _manifest().model_dump(mode="json")
    payload["basic_terminal"] = {"enabled": enabled}
    payload["embodiment"] = {
        "mode": "observe", "body_provider": "mistyai", "body_instance_id": "sim-body-01",
        "capability_requirements": [{"capability": name, "required": True} for name in (
            "action.execute.v1", "action.verify.v1", "observation.snapshot.v1", "evidence.reference.v1",
        )],
    }
    manifest = ExperimentManifest.model_validate(payload)
    desktop = Desktop(fail=fail)
    manager = EmbodimentManager(manifest.embodiment, desktop)
    supervisor = RuntimeSupervisor(
        manifest=manifest, cognitive=adapter, homeostasis=PassiveHomeostasisAdapter(),
        state_dir=tmp_path / "runtime", embodiment_manager=manager,
        action_coordinator=ActionCoordinator(desktop, capability_check=manager.supports),
    )
    supervisor.attach_perception_guard(PerceptionGuard(desktop, supervisor.submit_event, interval_seconds=60))
    return supervisor, desktop, model, pipeline


async def boot(supervisor):
    await supervisor.boot_and_recover()
    await supervisor._perception_guard.stop()
    supervisor.scheduler.suspend()
    while (event := await supervisor.queue.get(timeout=0)) is not None:
        await supervisor.dispatch(event)


async def drain(supervisor, *, approvals=True):
    results = []
    for _ in range(35):
        if supervisor._action_tasks:
            await asyncio.gather(*list(supervisor._action_tasks.values()))
        event = await supervisor.queue.get(timeout=0)
        if event is None:
            break
        result = await supervisor.dispatch(event)
        results.append(result)
        raw = result.detail.get("action")
        if raw and raw["state"] == "AWAITING_APPROVAL" and approvals:
            await supervisor.approve_action(raw["action_id"], approval="researcher")
    return results


@pytest.mark.parametrize("fail", [False, True])
def test_chat_to_verified_gui_steps_to_nonempty_delivered_reply(tmp_path, fail):
    async def scenario():
        supervisor, desktop, model, pipeline = runtime(tmp_path, fail=fail)
        try:
            await boot(supervisor)
            await supervisor.submit_user_message(REQUEST, session_id="sess_terminal")
            results = await drain(supervisor)
            assert desktop.executed == ["app.open.v1", "ui.type_text.v1", "ui.key_chord.v1"]
            assert desktop.verified[:2] == desktop.executed[:2]
            replies = [r for r in results if r.decision_kind == "reply"]
            assert len(replies) == 1 and replies[0].detail["delivered"]
            text = "".join(replies[0].detail["delivery"]["delivered_segments"])
            assert ("未完成" in text) if fail else ("/home/misty-body" in text and "退出码 0" in text)
            assert not supervisor._basic_terminal.tasks
            executed = list(desktop.executed)
            for result in results:
                if "action" in result.detail:
                    await supervisor.approve_action(result.detail["action"]["action_id"], approval="researcher")
            assert desktop.executed == executed
            turns = pipeline.session_store.recent("sess_terminal")
            assert sum(turn.text == REQUEST for turn in turns) == 1
            assert model.calls
        finally:
            await supervisor.stop(reason="test finished")
    asyncio.run(scenario())


def test_pending_gui_input_requires_existing_approval_and_cannot_run_after_new_user(tmp_path):
    async def scenario():
        supervisor, desktop, _, _ = runtime(tmp_path)
        try:
            await boot(supervisor)
            await supervisor.submit_user_message(REQUEST)
            results = await drain(supervisor, approvals=False)
            pending = next(r.detail["action"] for r in results if r.detail.get("action", {}).get("state") == "AWAITING_APPROVAL")
            assert desktop.executed == ["app.open.v1"]
            await supervisor.submit_user_message("停止刚才的操作。")
            outcome = await supervisor.approve_action(pending["action_id"], approval="researcher")
            assert outcome.state == ActionExecutionState.CANCELLED
            assert desktop.executed == ["app.open.v1"]
        finally:
            await supervisor.stop(reason="test finished")
    asyncio.run(scenario())


def test_late_first_plan_cannot_revive_after_new_user(tmp_path):
    async def scenario():
        supervisor, desktop, model, _ = runtime(tmp_path, block=True)
        try:
            await boot(supervisor)
            await supervisor.submit_user_message(REQUEST)
            first = await supervisor.queue.get(timeout=0)
            processing = asyncio.create_task(supervisor.dispatch(first))
            assert await asyncio.to_thread(model.entered.wait, 5)
            await supervisor.submit_user_message("先不要执行，陪我聊聊。")
            model.release.set()
            result = await processing
            assert result.decision_kind == "no_action"
            assert not desktop.executed and not supervisor._basic_terminal.tasks
        finally:
            model.release.set()
            await supervisor.stop(reason="test finished")
    asyncio.run(scenario())


@pytest.mark.parametrize("enabled", [False, True])
def test_ordinary_chat_and_client_metadata_do_not_authorize_terminal(tmp_path, enabled):
    async def scenario():
        supervisor, desktop, model, _ = runtime(tmp_path, enabled=enabled)
        try:
            await boot(supervisor)
            from harness.runtime import RuntimeEventPriority, RuntimeEventType
            await supervisor.submit_event(
                event_type=RuntimeEventType.USER_MESSAGE, priority=RuntimeEventPriority.USER,
                source="gui", payload={"text": "你好。", "mode": "basic_terminal_task", "basic_terminal": {"enabled": True}},
            )
            results = await drain(supervisor)
            assert any(r.decision_kind == "reply" and r.detail["delivered"] for r in results)
            assert not model.calls and not desktop.executed
        finally:
            await supervisor.stop(reason="test finished")
    asyncio.run(scenario())


def test_restart_stops_pending_terminal_task_without_retyping(tmp_path):
    async def scenario():
        supervisor, desktop, _, _ = runtime(tmp_path)
        await boot(supervisor)
        await supervisor.submit_user_message(REQUEST)
        results = await drain(supervisor, approvals=False)
        pending = next(r.detail["action"] for r in results if r.detail.get("action", {}).get("state") == "AWAITING_APPROVAL")
        assert desktop.executed == ["app.open.v1"]
        await supervisor.stop(reason="restart test")

        restored, new_desktop, new_model, _ = runtime(tmp_path)
        try:
            await boot(restored)
            assert not restored._basic_terminal.tasks
            assert not new_model.calls and not new_desktop.executed
            outcome = await restored.approve_action(pending["action_id"], approval="researcher")
            assert outcome.state == ActionExecutionState.CANCELLED
            assert not new_desktop.executed
            await drain(restored)
            await restored.submit_user_message(REQUEST)
            await drain(restored)
            assert new_desktop.executed == ["app.open.v1", "ui.type_text.v1", "ui.key_chord.v1"]
        finally:
            await restored.stop(reason="test finished")
    asyncio.run(scenario())
