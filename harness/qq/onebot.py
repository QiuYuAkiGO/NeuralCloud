"""Authenticated OneBot 11 forward WebSocket, with one reader and echoed calls."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit
from uuid import uuid4

from websockets.asyncio.client import connect


class OneBotError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


ALLOWED_ACTIONS = frozenset({"get_login_info", "get_status", "get_group_list", "get_group_info",
    "get_group_member_info", "get_group_msg_history", "get_image", "get_msg", "send_group_msg",
    "get_friend_list", "get_friend_msg_history", "send_private_msg"})


def validate_endpoint(url):
    parts = urlsplit(url)
    if (parts.scheme not in {"ws", "wss"} or parts.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parts.username or parts.password or parts.query or parts.fragment):
        raise ValueError("onebot_requires_authenticated_loopback_endpoint")
    return url


class OneBotClient:
    def __init__(self, url, token, *, on_event):
        self.url = validate_endpoint(url)
        if not isinstance(token, str) or len(token) < 32 or any(x.isspace() for x in token):
            raise ValueError("onebot_token_invalid")
        self._token, self.on_event = token, on_event
        self.socket = None
        self._reader = None
        self._dispatcher = None
        self._events = asyncio.Queue(maxsize=128)
        self._pending = {}
        self.closed = asyncio.Event()
        self.last_error_type = None

    async def open(self):
        self.closed.clear()
        self.last_error_type = None
        self._events = asyncio.Queue(maxsize=128)
        self.socket = await connect(self.url, additional_headers={"Authorization": "Bearer " + self._token},
            proxy=None, max_size=24 * 1024 * 1024, max_queue=32, compression=None,
            open_timeout=8, close_timeout=2, ping_interval=15, ping_timeout=15)
        self._reader = asyncio.create_task(self._read())
        self._dispatcher = asyncio.create_task(self._dispatch())

    async def _dispatch(self):
        try:
            while not self.closed.is_set():
                payload = await self._events.get()
                try:
                    await self.on_event(payload)
                finally:
                    self._events.task_done()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.last_error_type = "onebot_event_callback_failed"
            self.closed.set()
            if self.socket is not None:
                await self.socket.close()

    async def _read(self):
        try:
            async for raw in self.socket:
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise OneBotError("onebot_invalid_envelope")
                echo = payload.get("echo")
                if isinstance(echo, str) and echo in self._pending:
                    future = self._pending[echo]
                    if not future.done():
                        future.set_result(payload)
                elif "post_type" in payload:
                    # Event processing can itself need a metadata API. Keep
                    # reading its echo instead of awaiting the callback here.
                    try:
                        self._events.put_nowait(payload)
                    except asyncio.QueueFull:
                        raise OneBotError("onebot_event_queue_overflow") from None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Message bodies, URL signatures, tokens and remote errors stay private.
            self.last_error_type = (exc.code if isinstance(exc, OneBotError) else
                                    "onebot_invalid_envelope" if isinstance(exc, (ValueError, TypeError))
                                    else "onebot_disconnected")
        finally:
            self.closed.set()
            if self.last_error_type is None:
                self.last_error_type = "onebot_disconnected"
            for future in tuple(self._pending.values()):
                if not future.done():
                    future.set_exception(OneBotError("onebot_disconnected"))
            if self.socket is not None:
                await self.socket.close()

    async def call(self, action, params=None, timeout=10):
        if action not in ALLOWED_ACTIONS:
            raise OneBotError("onebot_action_not_allowed")
        if self.socket is None or self.closed.is_set():
            raise OneBotError("onebot_disconnected")
        if not 0 < timeout <= 60:
            raise OneBotError("onebot_deadline_invalid")
        echo = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        try:
            async with asyncio.timeout(timeout):
                await self.socket.send(json.dumps({"action": action, "params": params or {}, "echo": echo},
                                                 ensure_ascii=False))
                result = await future
            if result.get("status") != "ok" or type(result.get("retcode")) is not int or result["retcode"] != 0:
                raise OneBotError("onebot_action_failed")
            data = result.get("data")
            if not isinstance(data, (dict, list)):
                raise OneBotError("onebot_invalid_result")
            return data
        except TimeoutError:
            raise OneBotError("onebot_request_timeout") from None
        except OneBotError:
            raise
        except Exception:
            raise OneBotError("onebot_disconnected") from None
        finally:
            self._pending.pop(echo, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def close(self):
        if self.socket is not None:
            await self.socket.close()
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            await asyncio.gather(self._dispatcher, return_exceptions=True)
        self.closed.set()
