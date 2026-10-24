"""Cancellable SSE dispatch for synchronous native QQ tool/vision workers."""
import asyncio
from contextvars import ContextVar
import threading

dispatch = ContextVar('native_background_dispatch', default=None)


async def run(service, operation, *, timeout=60):
    async with asyncio.timeout(timeout):
        return await _run(service, operation)


async def _run(service, operation):
    priority = service.priority
    async with priority.background_gate:
        loop, stopped, guard = asyncio.get_running_loop(), threading.Event(), threading.Lock()
        futures, http_tasks = set(), set()

        async def request(client, messages, kwargs):
            task = asyncio.current_task()
            http_tasks.add(task)
            try:
                kwargs.pop('reasoning_effort', None)
                return await client.acomplete_agent_turn(messages, **kwargs)
            finally:
                http_tasks.discard(task)

        def bridge(client, messages, **kwargs):
            with guard:
                if stopped.is_set():
                    raise ValueError('native_preempted')
                future = asyncio.run_coroutine_threadsafe(request(client, messages, kwargs), loop)
                futures.add(future)
            try:
                return future.result()
            finally:
                with guard:
                    futures.discard(future)

        async def work():
            token = dispatch.set(bridge)
            try:
                return await operation()
            finally:
                dispatch.reset(token)

        task = asyncio.create_task(work())
        with priority.lock:
            priority.task, priority.loop = task, loop
            if priority.foreground:
                task.cancel()
        try:
            return await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            raise ValueError('native_preempted') from None
        finally:
            with guard:
                stopped.set()
                for future in tuple(futures):
                    future.cancel()
            if http_tasks:
                await asyncio.gather(*tuple(http_tasks), return_exceptions=True)
            with priority.lock:
                if priority.task is task:
                    priority.task = priority.loop = None
