"""Run the native main-model loop as preemptible background cognition."""
from __future__ import annotations

import asyncio
import threading

from harness.agentic.actions import GoalActions
from harness.agentic.context import persona_system
from harness.agentic.internal_state import TurnState
from harness.agentic.runner import AgentRunner
from harness.agentic.store import canonical


async def decide(service, request, ready, payload, *, timeout):
    # Queue behind a current memory/diary task using the shared FIFO lock.
    # Waiting consumes the decision deadline, but never a model attempt.
    async with asyncio.timeout(timeout):
        return await _decide(service, request, ready, payload, timeout=timeout)


async def _decide(service, request, ready, payload, *, timeout):
    priority = service.priority
    async with priority.background_gate:
        async def operation():
            runtime = await asyncio.to_thread(service.runtime_state_provider)
            frozen = request.goal_context.get('native_state') or {}
            if (runtime.get('mode') != 'on' or not runtime.get('running') or
                    not runtime.get('autonomous_enabled') or any(runtime.get(k) != frozen.get(k)
                    for k in ('runtime_instance_id', 'manifest_revision')) or
                    runtime.get('rest', {}).get('revision') != frozen.get('rest_revision')):
                raise ValueError('native_autonomy_fence_changed')
            conversation = service.conversation('local:main', 'autonomous:goals')
            state = TurnState(service, conversation, runtime)
            loop = asyncio.get_running_loop()
            stopped, futures, guard = threading.Event(), set(), threading.Lock()

            class Bridge:
                def complete_agent_turn(self, messages, **kwargs):
                    kwargs.pop('reasoning_effort', None)
                    with guard:
                        if stopped.is_set():
                            raise ValueError('native_preempted')
                        future = asyncio.run_coroutine_threadsafe(service.llm.acomplete_agent_turn(messages, **kwargs), loop)
                        futures.add(future)
                    try:
                        return future.result()
                    finally:
                        with guard:
                            futures.discard(future)

            runner = AgentRunner(store=service.store, context=service.context, llm=Bridge(),
                persona_provider=service.persona_provider, priority=priority)
            thread = asyncio.create_task(asyncio.to_thread(runner.run, conversation,
                system=persona_system(service.persona_provider()),
                current={'role':'user', 'content':canonical(payload)}, source_id='', timeout=timeout,
                internal_state=state, actions=GoalActions(state, request, ready), foreground=False))
            try:
                return await asyncio.shield(thread)
            finally:
                # Do not leave a synchronous worker or SSE generation behind
                # after foreground arrival, timeout, or service shutdown.
                with guard:
                    stopped.set()
                    for future in tuple(futures):
                        future.cancel()
                if not thread.done():
                    try:
                        await asyncio.shield(thread)
                    except (Exception, asyncio.CancelledError):
                        pass

        task = asyncio.create_task(operation())
        with priority.lock:
            priority.task, priority.loop = task, asyncio.get_running_loop()
            if priority.foreground:
                task.cancel()
        try:
            async with asyncio.timeout(timeout):
                return await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            raise ValueError('native_preempted') from None
        finally:
            with priority.lock:
                if priority.task is task:
                    priority.task = priority.loop = None
