"""Runtime events must not cause operator reads without a browser request."""
import asyncio
import httpx
from gui.runtime_server import RuntimeGateway


def test_runtime_events_only_invalidate_even_with_no_viewers(monkeypatch):
    async def run():
        gateway = RuntimeGateway("http://runtime.test", "fixture")
        original = httpx.AsyncClient
        completed = asyncio.Event()
        reads = []

        async def no_read(*args, **kwargs):
            reads.append(True)
            raise AssertionError("unsolicited snapshot")

        async def handle(request):
            return httpx.Response(200, text='event: event.completed\ndata: {}\n\n')

        monkeypatch.setattr(gateway, "status", no_read)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(
            **kw, transport=httpx.MockTransport(handle)))
        for viewed in (False, True):
            queue = gateway.subscribe() if viewed else None
            completed.clear()
            broadcast = gateway.broadcast

            async def observe(event):
                await broadcast(event)
                completed.set()
                await asyncio.Event().wait()

            with monkeypatch.context() as patch:
                patch.setattr(gateway, "broadcast", observe)
                task = asyncio.create_task(gateway._stream())
                await asyncio.wait_for(completed.wait(), 2)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if queue is not None:
                assert queue.get_nowait() == {"type": "state_dirty"}
                gateway.unsubscribe(queue)
        assert not reads
        await gateway.close()
    asyncio.run(run())
