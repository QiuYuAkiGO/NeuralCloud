"""Private child-process IPC to the pinned Tencent SDK, with no model interface."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from uuid import uuid4

from harness.qq.onebot import OneBotError

INGRESS_KEYS = frozenset({
    "dispatch", "group_at_dispatch", "group_dispatch", "lifecycle_dispatch", "other_dispatch",
    "group_callback", "group_allowed_callback", "other_message_callback", "raw_callback",
    "parse_error", "sdk_error", "ready", "resumed",
})


class OfficialQQClient:
    def __init__(self, credentials_file, *, app_id, send_groups, on_event, on_lifecycle=None, send_users=()):
        self.credentials_file = Path(credentials_file)
        self.app_id = app_id
        self.send_groups = frozenset(send_groups)
        self.send_users = frozenset(send_users)
        self.on_event = on_event
        self.on_lifecycle = on_lifecycle
        self.closed = asyncio.Event()
        self.ready = asyncio.Event()
        self.enabled = asyncio.Event()
        self.connected = False
        self.ready_data = None
        self.last_error_type = None
        self.ingress_counts = {}
        self.process = None
        self._reader = self._dispatcher = None
        self._events = asyncio.Queue(maxsize=128)
        self._pending = {}

    async def open(self):
        self.check_credentials()
        script = Path(__file__).resolve().parents[2] / "tools/qqbot/bridge.mjs"
        env = dict(os.environ, NODE_TLS_REJECT_UNAUTHORIZED="1")
        # Credentials stay in a permission-restricted file; no secret argv/env.
        self.process = await asyncio.create_subprocess_exec(
            "node",
            str(script),
            str(self.credentials_file),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=2 * 1024 * 1024 + 1,
            env=env,
        )
        self._reader = asyncio.create_task(self._read())
        self._dispatcher = asyncio.create_task(self._dispatch())
        try:
            async with asyncio.timeout(40):
                await self.ready.wait()
            if not self.connected:
                raise OneBotError("qqbot_connect_failed")
        except BaseException:
            await self.close()
            raise

    def check_credentials(self):
        """Check private metadata before starting any SDK or authentication."""
        try:
            info = self.credentials_file.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536 or (os.name != "nt" and info.st_mode & 0o077):
                raise ValueError
            value = json.loads(self.credentials_file.read_text(encoding="utf-8"))
            if (
                not isinstance(value, dict)
                or set(value) not in ({"appId", "appSecret", "groups"}, {"appId", "appSecret", "groups", "users"})
                or value["appId"] != self.app_id
                or not isinstance(value["appSecret"], str)
                or not 16 <= len(value["appSecret"]) <= 4096
                or not isinstance(value["groups"], list)
                or len(value["groups"]) != len(self.send_groups)
                or set(value["groups"]) != self.send_groups
                or not isinstance(value.get("users", []), list)
                or len(value.get("users", [])) != len(self.send_users)
                or set(value.get("users", [])) != self.send_users
            ):
                raise ValueError
        except (OSError, ValueError, TypeError):
            raise ValueError("qqbot_private_credentials_or_scope_invalid") from None

    async def _read(self):
        try:
            while raw := await self.process.stdout.readline():
                item = json.loads(raw)
                if not isinstance(item, dict):
                    raise ValueError
                kind = item.get("type")
                if kind in {"ready", "resumed"}:
                    if item.get("app_id") != self.app_id:
                        raise ValueError
                    if kind == "ready":
                        self.ready_data = item.get("data")
                    self.connected = True
                    self.ready.set()
                elif kind == "disconnected":
                    # End this actor epoch immediately; no in-flight response can
                    # become sendable when the SDK later reconnects/resumes.
                    self.last_error_type = "qqbot_gateway_disconnected"
                    break
                elif kind == "telemetry":
                    counts = item.get("counts")
                    if (set(item) != {"type", "app_id", "counts"}
                            or item.get("app_id") != self.app_id or not isinstance(counts, dict)
                            or set(counts) != INGRESS_KEYS
                            or any(type(v) is not int or not 0 <= v <= 2147483647 for v in counts.values())
                            or any(v < self.ingress_counts.get(k, 0) for k, v in counts.items())):
                        raise ValueError
                    self.ingress_counts = dict(counts)
                elif kind in {"event", "lifecycle"}:
                    if item.get("app_id") != self.app_id:
                        raise ValueError
                    self._events.put_nowait(item)
                elif kind == "result":
                    future = self._pending.get(item.get("id"))
                    if future is not None and not future.done():
                        future.set_result(item)
                elif kind == "error":
                    self.last_error_type = "qqbot_gateway_error"
                    break
                else:
                    raise ValueError
        except asyncio.CancelledError:
            raise
        except Exception:
            self.last_error_type = "qqbot_transport_invalid"
        finally:
            self.connected = False
            self.closed.set()
            self.ready.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(OneBotError("qqbot_disconnected"))

    async def _dispatch(self):
        try:
            await self.enabled.wait()
            while not self.closed.is_set():
                item = await self._events.get()
                try:
                    if item["type"] == "event":
                        await self.on_event({k: v for k, v in item.items() if k != "type"})
                    elif self.on_lifecycle is not None:
                        await self.on_lifecycle(item)
                finally:
                    self._events.task_done()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.last_error_type = "qqbot_event_callback_failed"
            self.connected = False
            self.closed.set()

    async def reply(self, *, group_openid=None, user_openid=None, message_id, seq, text, timeout):
        if (group_openid is None) == (user_openid is None):
            raise OneBotError("qqbot_reply_scope_invalid")
        if (user_openid is not None and user_openid not in self.send_users
                or group_openid is not None and group_openid not in self.send_groups):
            raise OneBotError("qqbot_reply_scope_invalid")
        if self.closed.is_set() or not self.connected or not 0 < timeout <= 60:
            raise OneBotError("qqbot_unavailable")
        import time

        identity = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[identity] = future
        command = {
            "id": identity,
            "action": "reply",
            **({"user_openid": user_openid} if user_openid is not None else {"group_openid": group_openid}),
            "msg_id": message_id,
            "msg_seq": seq,
            "text": text,
            "deadline_ms": (time.time() + timeout) * 1000,
        }
        try:
            async with asyncio.timeout(timeout):
                self.process.stdin.write((json.dumps(command, ensure_ascii=False) + "\n").encode())
                await self.process.stdin.drain()
                result = await future
            if result.get("ok") is not True or not isinstance(result.get("result"), dict):
                raise OneBotError("qqbot_send_unconfirmed")
            return result["result"]
        except TimeoutError:
            # The parent outbox records UNKNOWN; never retry this POST.
            raise OneBotError("qqbot_send_unconfirmed") from None
        finally:
            self._pending.pop(identity, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def call(self, action, params=None, timeout=10):
        # The inherited image reader can attempt get_image on a failed URL.
        # Open Platform provides no equivalent; keep explicit unknown imagery.
        raise OneBotError("qqbot_read_api_unsupported")

    async def close(self):
        self.closed.set()
        self.connected = False
        for task in (self._reader, self._dispatcher):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(x for x in (self._reader, self._dispatcher) if x is not None), return_exceptions=True)
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
