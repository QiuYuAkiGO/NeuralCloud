"""Bounded user-requested desktop tasks; journal every step before its effect.

This bridge owns task continuity, not GUI execution. Each action still passes
through Runtime's ordinary permissions, independent verify and commit path.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from harness.contracts.decisions import GoalProposal
from harness.runtime.models import ActionExecutionState, RuntimeEvent, RuntimeEventPriority, RuntimeEventType, utc_now

if TYPE_CHECKING:
    from harness.runtime.supervisor import RuntimeSupervisor


class _RestoredRoots:
    def __init__(self, index, boundary):
        self.index, self.boundary = index, boundary
        self.interrupted = set()

    def __contains__(self, key):
        sequence = self.index.get(key)
        return key in self.interrupted or sequence is not None and sequence <= self.boundary


class BasicTerminalRuntime:
    def __init__(self, supervisor: RuntimeSupervisor) -> None:
        self.runtime = supervisor
        self.config = supervisor.manifest.basic_terminal
        self.tasks: dict[str, dict[str, Any]] = {}
        self.contexts: dict[str, dict[str, Any]] = {}
        self.restored_roots: set[str] = set()
    def recover(self) -> None:
        # The shared projection is initialized under the Runtime instance lock.
        store = self.runtime.recovery
        self.restored_roots = _RestoredRoots(store.mapping("terminal_tasks"), store.projector.cursor)
        for seq in list(store.mapping("terminal_active").values()):
            task = store.read(seq).data["task"]
            self.tasks[task["task_id"]] = task

        # Called only after Runtime has acquired the exclusive instance lock.
        # Reconcile submitted actions normally, but never retype a command on boot.
        for task in list(self.tasks.values()):
            self.restored_roots.interrupted.add(task["task_id"])
            self._save({**task, "status": "interrupted", "reason": "runtime restarted"})

    def stop_reason(self, event: RuntimeEvent, *, require_active: bool = False) -> str | None:
        from datetime import datetime

        task_id = str(event.payload.get("terminal_task_id") or event.event_id)
        item = self.contexts.get(event.event_id)
        task = self.tasks.get(task_id) or (item["task"] if item else None)
        if task_id in self.restored_roots:
            return "终端任务因 Runtime 重启而停止；请核对已有输出后重新下达任务。"
        if require_active and task_id not in self.tasks:
            return "终端任务已被中断，已取消尚未执行的操作。"
        if task is None:
            return None
        if task["root_event"]["receive_seq"] < self.runtime._last_user_receive_seq:
            return "收到新的用户消息，已停止此前终端任务。"
        if utc_now() >= datetime.fromisoformat(task["deadline"]):
            return "终端任务已超过时限，已停止后续操作。"
        return None

    def _save(self, task: dict[str, Any]) -> None:
        self.runtime._append_journal("basic_terminal_task", data={"task": task})
        if task["status"] == "working":
            self.tasks[task["task_id"]] = task
        else:
            self.tasks.pop(task["task_id"], None)

    def interrupt_for_user(self, event: RuntimeEvent) -> None:
        for task in list(self.tasks.values()):
            if task["root_event"]["event_id"] != event.event_id:
                self._save({**task, "status": "interrupted", "reason": "new user message"})

    async def expire_pending_approvals(self) -> None:
        """Close overdue unstarted actions on the existing Runtime heartbeat."""
        from datetime import datetime

        # Approval holds the same lock through reservation. Never cancel a GUI
        # effect that started while this heartbeat was waiting for the lock.
        async with self.runtime._approval_lock:
            now = utc_now()
            for action_id, context in list(self.runtime._action_context.items()):
                decision = context["prepared"].decision
                if not isinstance(decision, GoalProposal) or not decision.policy_id.startswith("basic_terminal_"):
                    continue
                action = self.runtime.action_coordinator.get(action_id)
                if action is None or not action.required_approvals or action.state not in {
                    ActionExecutionState.AWAITING_APPROVAL, ActionExecutionState.READY,
                }:
                    continue
                event = context["event"]
                task = self.tasks.get(str(event.payload.get("terminal_task_id") or event.event_id))
                deadline = action.deadline
                if task is not None:
                    deadline = min(deadline, datetime.fromisoformat(task["deadline"]))
                if now >= deadline:
                    # The ordinary outcome event commits the cancellation and
                    # continues to a failure reply; no external effect is run.
                    await self.runtime.reject_action(
                        action_id, reason="终端任务未完成：等待批准已超过时限，未执行待批准的操作。",
                    )

    async def context(self, event: RuntimeEvent) -> dict[str, Any] | None:
        from harness.cognitive.basic_terminal import is_terminal_action_request, normalize_terminal_observation

        if event.event_type == RuntimeEventType.USER_MESSAGE:
            if not is_terminal_action_request(str(event.payload.get("text") or "")):
                return None
            task = {
                "task_id": event.event_id,
                "root_event": event.model_dump(mode="json"),
                "history": [],
                "deadline": (utc_now() + timedelta(seconds=self.config.task_timeout_seconds)).isoformat(),
                "status": "working",
            }
        elif event.event_type == RuntimeEventType.TERMINAL_CONTINUATION:
            task = self.tasks.get(str(event.payload.get("terminal_task_id") or ""))
            if task is None or event.payload.get("step") != len(task["history"]):
                return None
        else:
            return None
        root = RuntimeEvent.model_validate(task["root_event"])
        context: dict[str, Any] = {
            "enabled": True,
            "terminal_app_id": self.config.terminal_app_id,
            "permitted_intents": list(self.config.permitted_intents),
            "provider_intents": [],
            "task_id": task["task_id"],
            "step": len(task["history"]),
            "max_steps": self.config.max_steps,
            "history": task["history"],
            "root_event_id": root.event_id,
            "root_source_time": root.source_time.isoformat(),
            "text": str(root.payload.get("text") or ""),
            "session_id": root.payload.get("session_id"),
            "channel": root.payload.get("channel") or "chat",
            "observation": None,
        }
        from datetime import datetime

        if task["task_id"] in self.restored_roots:
            context["error"] = "终端任务因 Runtime 重启而停止；请核对已有输出后重新下达任务。"
        elif utc_now() >= datetime.fromisoformat(task["deadline"]):
            context["error"] = "终端任务未完成：已超过时限，已停止后续操作。"
        elif self.runtime._perception_guard is None:
            context["error"] = "Linux 桌面观察后端不可用，未执行终端命令。"
        elif not self.runtime.embodiment.supports("action.execute.v1"):
            context["error"] = "Linux 桌面动作能力不可用，未执行终端命令。"
        else:
            try:
                supported = getattr(self.runtime.embodiment.provider, "supported_intents", None)
                context["provider_intents"] = sorted(supported()) if callable(supported) else []
                snapshot = await self.runtime._perception_guard.backend.observe()
                age = (utc_now() - snapshot.captured_at).total_seconds()
                if age < -self.runtime.manifest.embodiment.future_clock_tolerance_seconds:
                    raise ValueError("desktop observation is in the future")
                if age > self.runtime.manifest.embodiment.max_state_age_seconds:
                    raise ValueError("desktop observation is stale")
                context["observation"] = normalize_terminal_observation(
                    snapshot, terminal_app_id=self.config.terminal_app_id,
                )
            except Exception as exc:
                context["error"] = f"无法取得可验证的 Linux 桌面观察：{type(exc).__name__}"
        self.contexts[event.event_id] = {"task": task, "context": context}
        return context

    def bind(self, event: RuntimeEvent, prepared) -> None:
        item = self.contexts.get(event.event_id)
        if item is not None and isinstance(prepared.decision, GoalProposal):
            self._save({**item["task"], "pending_prepare_id": prepared.prepare_id})

    def finish_decision(self, event: RuntimeEvent, prepared, detail: dict[str, Any]) -> None:
        item = self.contexts.pop(event.event_id, None)
        if item is None:
            return
        if not isinstance(prepared.decision, GoalProposal) or detail.get("effective_decision_kind") in {"defer", "no_action"}:
            task = item["task"]
            if task["task_id"] in self.tasks:
                self._save({**task, "status": "finished", "decision_kind": prepared.decision.kind})

    async def action_completed(self, event: RuntimeEvent, action, evidence) -> None:
        task_id = str(event.payload.get("terminal_task_id") or event.event_id)
        task = self.tasks.get(task_id)
        if task is None:
            return
        # A recovered outcome may be committed more than once, but it may only
        # advance the task once. Unknown external effects never reach this hook.
        if any(item.get("action_id") == action.action_id for item in task["history"]):
            return
        spec = action.embodied_action
        history = [*task["history"], {
            "action_id": action.action_id,
            "intent_id": spec.intent.intent_id if spec else "",
            "arguments": spec.intent.arguments if spec else {},
            "state": action.state.value.lower(),
            "evidence_refs": list(evidence.evidence_refs),
        }]
        task = {**task, "history": history, "pending_prepare_id": None}
        self._save(task)
        root = RuntimeEvent.model_validate(task["root_event"])
        digest = hashlib.sha256(f"{task_id}:{len(history)}".encode()).hexdigest()[:32]
        await self.runtime.submit_event(
            event_type=RuntimeEventType.TERMINAL_CONTINUATION,
            priority=RuntimeEventPriority.IMPORTANT_ENVIRONMENT,
            source="basic_terminal",
            event_id=f"terminal_{digest}",
            correlation_id=root.correlation_id,
            idempotency_key=f"terminal:{task_id}:{len(history)}",
            payload={
                "terminal_task_id": task_id,
                "step": len(history),
                "text": root.payload.get("text") or "",
                "session_id": root.payload.get("session_id"),
                "channel": root.payload.get("channel") or "chat",
            },
        )
