"""Opt-in phone-only idle cognition, independent of global channel autonomy."""
import asyncio
import json
import logging
from pathlib import Path
import threading
import time
from uuid import uuid4

from harness.agentic.context import persona_system
from harness.agentic.mobile import AgentMobile, CONFIG
from harness.agentic.runner import AgentRunner

STATUS = Path('/var/lib/neuralcloud/mobile-autonomy.status.json')
HISTORY = Path('/var/lib/neuralcloud/mobile-autonomy.runs.jsonl')
IDLE = '''这是手机自主空闲决策机会，不是新收到的用户消息，也没有指定你必须执行的任务。
你可以根据自己的意愿和已有上下文选择是否使用专用手机，例如看看 QQ 有没有值得回应的新联系、
浏览感兴趣的公开信息，或什么也不做。不要为了验收而伪造兴趣或发消息。
若选择处理手机 QQ 中的请求，只有 2285027817 有权提出操作请求；在已授权会话中直接处理，无需打开资料卡核对身份。
若主动联系，也只联系 2285027817，避免打扰和重复发送。手机没有新消息时不得假装有人发言。
finish_turn 是你的本轮内部记录，不会作为用户消息发送；要真实发送必须使用手机界面的发送操作。
'''


class MobileAutonomy:
    def __init__(self, service):
        self.service=service
        self.task=None
        self.status={'state':'starting','next_at':0}

    def write(self, **changes):
        self.status.update(changes)
        temp=STATUS.with_suffix('.tmp')
        temp.write_text(json.dumps(self.status,ensure_ascii=False))
        temp.replace(STATUS)

    async def start(self):
        self.task=asyncio.create_task(self.run(),name='misty-mobile-idle')

    async def close(self):
        if self.task:
            self.task.cancel()
            try:await self.task
            except asyncio.CancelledError:pass
            self.task=None

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.getLogger(__name__).exception('Phone idle turn deferred')
                self.write(state='deferred',error=type(exc).__name__,error_detail=str(exc)[:300])
            await asyncio.sleep(10)

    async def tick(self):
        service=self.service
        if not service.enabled or service.host.adapter._maintenance_paused:
            self.write(state='paused');return
        try:config=json.loads(CONFIG.read_text())
        except (OSError,ValueError):return
        if not config.get('enabled') or not config.get('autonomous_enabled'):
            self.write(state='disabled');return
        if time.time()<self.status['next_at'] or service.priority.foreground:return
        runtime=await asyncio.to_thread(service.runtime_state_provider)
        if not runtime.get('running') or runtime.get('rest',{}).get('resting'):
            self.write(state='runtime_paused');return
        async with service.priority.background_gate:
            if service.priority.foreground:return
            self.write(state='running',started_at=time.time(),next_at=time.time()+max(0, float(config.get('idle_interval_seconds', 300))),error=None)
            operation=asyncio.create_task(self.decide(runtime))
            with service.priority.lock:
                service.priority.task,service.priority.loop=operation,asyncio.get_running_loop()
                if service.priority.foreground:operation.cancel()
            try:
                result=await operation
                self.write(state='idle',last_result=result.finish.text,last_calls=result.calls,completed_at=time.time())
                with HISTORY.open('a') as f:f.write(json.dumps(self.status,ensure_ascii=False)+'\n')
            except asyncio.CancelledError:
                self.write(state='preempted')
                if asyncio.current_task().cancelling():raise
            finally:
                with service.priority.lock:
                    if service.priority.task is operation:
                        service.priority.task=service.priority.loop=None

    async def decide(self, frozen):
        service=self.service
        stopped=threading.Event()
        futures=set();lock=threading.Lock();loop=asyncio.get_running_loop()
        def guard():
            if stopped.is_set() or service.priority.foreground or service.host.adapter._maintenance_paused:
                raise ValueError('mobile_idle_preempted')
            config=json.loads(CONFIG.read_text())
            current=service.runtime_state_provider()
            if (not config.get('enabled') or not config.get('autonomous_enabled') or not current.get('running')
                    or current.get('runtime_instance_id')!=frozen.get('runtime_instance_id')
                    or current.get('manifest_revision')!=frozen.get('manifest_revision')
                    or current.get('rest',{}).get('resting')):
                raise ValueError('mobile_idle_fence_changed')
        async def complete(messages, kwargs):
            async with service.background_scope('mobile-idle:' + uuid4().hex):
                return await service.llm.acomplete_agent_turn(messages, **kwargs)
        class Bridge:
            def complete_agent_turn(self, messages, **kwargs):
                guard()
                with lock:
                    if stopped.is_set():raise ValueError('mobile_idle_preempted')
                    future=asyncio.run_coroutine_threadsafe(complete(messages,kwargs),loop)
                    futures.add(future)
                try:return future.result()
                finally:
                    with lock:futures.discard(future)
        from harness.agentic.mobile_contracts import MobileObserve
        probe=AgentMobile.for_audience('local:main','host-route-phone',validator=guard,autonomous=True)
        screen=await asyncio.to_thread(probe.invoke,'mobile_observe',MobileObserve(),deadline=time.monotonic()+30)
        group = screen.get('group_id') == '1044946257'
        audience = 'qq:mobile:group:1044946257' if group else 'local:main'
        prompt = ('这是群聊 1044946257 的自主互动机会，没有伪造的新消息。观察群内真实消息，按群聊规则决定是否回应。' if group else IDLE)
        conversation=service.conversation(audience,'autonomous:mobile:1044946257' if group else 'autonomous:mobile')
        mobile=AgentMobile.for_audience(audience,conversation,validator=guard,autonomous=True)
        if mobile is None:raise ValueError('mobile_idle_disabled')
        self.write(conversation=conversation,phone_scope=mobile.conversation)
        runner=AgentRunner(store=service.store,context=service.context,llm=Bridge(),
                           persona_provider=service.persona_provider,priority=service.priority)
        worker=asyncio.create_task(asyncio.to_thread(runner.run,conversation,
            system=persona_system(service.persona_provider()),current={'role':'user','content':prompt},source_id='',
            timeout=120,foreground=False,mobile=mobile))
        try:
            async with asyncio.timeout(125):return await asyncio.shield(worker)
        finally:
            with lock:
                stopped.set()
                for future in tuple(futures):future.cancel()
            if not worker.done():
                try:await asyncio.shield(worker)
                except (Exception,asyncio.CancelledError):pass
