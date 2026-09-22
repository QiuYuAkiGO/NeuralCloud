"""SSH transport behind negotiated, provider-neutral server capabilities."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import json
import time
from typing import Any, Protocol

import asyncssh
import shlex

from harness.embodiment.capabilities import (
    SERVER_EXECUTE, SERVER_OBSERVE, CapabilityDescriptor, CapabilityStatus, EmbodimentState,
    EmbodimentStatus, HealthStatus, ProviderCapabilities, ProviderHealth,
)
from harness.embodiment.manager import EmbodimentManager
from harness.embodiment.server_contracts import (
    MAX_OUTPUT_BYTES, ServerCommandArguments, ServerCommandResult, ServerError,
)
from harness.research.manifest import EmbodimentCondition


SERVER_CAPABILITIES = (SERVER_OBSERVE, SERVER_EXECUTE)
COOLDOWN_SECONDS = 30


class ServerTransport(Protocol):
    """Injected SSH exec surface so Runtime tests can avoid a live daemon."""

    async def execute(self, command: str, *, timeout: float) -> tuple[int | None, Any, Any]: ...

    async def close(self) -> None: ...


def _normalize_fingerprint(value: str) -> str:
    text = value.strip()
    if text.lower().startswith("sha256:"):
        return "SHA256:" + text.split(":", 1)[1]
    return text


class _FingerprintClient(asyncssh.SSHClient):
    def __init__(self, fingerprint: str | None):
        self._fingerprint = _normalize_fingerprint(fingerprint) if fingerprint else None

    def validate_host_public_key(self, host, addr, port, key):
        if self._fingerprint is None:
            return False
        return _normalize_fingerprint(key.get_fingerprint("sha256")) == self._fingerprint


_HOST_KEY_ALGS = {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-dss"}


def parse_host_key(value: str) -> tuple[str, Any]:
    if not isinstance(value, str) or not value.strip() or len(value) > 8192:
        raise ServerError("server_host_key_invalid")
    text = value.strip()
    if text.startswith("SHA256:") and len(text) >= 20:
        return "fingerprint", text
    try:
        parts = text.split()
        if parts and parts[0] in _HOST_KEY_ALGS:
            return "key", asyncssh.import_public_key(text)
        if len(parts) >= 2:
            return "key", asyncssh.import_public_key(" ".join(parts[1:]))
    except (ValueError, asyncssh.KeyImportError, UnicodeError):
        raise ServerError("server_host_key_invalid") from None
    raise ServerError("server_host_key_invalid")


class SSHServerProvider:
    def __init__(self, condition: EmbodimentCondition, *, host: str, port: int, username: str,
                 host_key: str, private_key: str, body_instance_id: str,
                 connect_timeout_seconds: float = 10, default_timeout_seconds: float = 60,
                 transport: ServerTransport | None = None):
        if not isinstance(host, str) or not host.strip() or len(host) > 253 or any(ord(c) <= 32 for c in host):
            raise ServerError("server_host_invalid")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ServerError("server_port_invalid")
        if not isinstance(username, str) or not username.strip() or len(username) > 128 or any(c.isspace() for c in username):
            raise ServerError("server_username_invalid")
        if not isinstance(body_instance_id, str) or not body_instance_id.strip() or len(body_instance_id) > 256:
            raise ServerError("server_body_identity_invalid")
        if isinstance(connect_timeout_seconds, bool) or not isinstance(connect_timeout_seconds, (int, float)) or not 0 < connect_timeout_seconds <= 60:
            raise ServerError("server_timeout_invalid")
        if isinstance(default_timeout_seconds, bool) or not isinstance(default_timeout_seconds, (int, float)) or not 0 < default_timeout_seconds <= 300:
            raise ServerError("server_timeout_invalid")
        try:
            self._private_key = asyncssh.import_private_key(private_key)
        except (ValueError, asyncssh.KeyImportError, UnicodeError, TypeError):
            raise ServerError("server_private_key_invalid") from None
        kind, pinned = parse_host_key(host_key)
        if kind == "fingerprint":
            pinned = _normalize_fingerprint(pinned)
        self.host = host.strip()
        self.port = int(port)
        self.username = username.strip()
        self.body_instance_id = body_instance_id
        self.connect_timeout_seconds = float(connect_timeout_seconds)
        self.default_timeout_seconds = float(default_timeout_seconds)
        self._host_key_kind = kind
        self._host_public_key = pinned if kind == "key" else None
        self._fingerprint = pinned if kind == "fingerprint" else None
        self._private_key_text = private_key
        self.manager = EmbodimentManager(condition.model_copy(deep=True), self)
        self._condition_hash = self._configuration_hash()
        self._activated = False
        self._closed = False
        self._auth_failed = False
        self._activation_lock = asyncio.Lock()
        self._call_lock = asyncio.Lock()
        self._conn: asyncssh.SSHClientConnection | None = None
        self._transport = transport
        self._states = dict.fromkeys(SERVER_CAPABILITIES, HealthStatus.DEGRADED)
        self._reasons = dict.fromkeys(SERVER_CAPABILITIES, "server_ssh_unverified")
        self._cooldowns: dict[str, float] = {}

    def _configuration_hash(self):
        value = {"condition": self.manager.condition.model_dump(mode="json"), "host": self.host, "port": self.port,
                 "username": self.username, "body_instance_id": self.body_instance_id,
                 "connect_timeout_seconds": self.connect_timeout_seconds,
                 "default_timeout_seconds": self.default_timeout_seconds}
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def _known_hosts(self, host, addr, port):
        if self._host_public_key is not None:
            return ([self._host_public_key], [], [])
        return ([], [], [])

    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id="ssh-server", provider_kind="remote_linux_ssh",
            provider_revision="server-v1:" + self._condition_hash, body_instance_id=self.body_instance_id,
            observed_at=datetime.now(UTC), capabilities=[
                CapabilityDescriptor(capability=capability, status=CapabilityStatus.AVAILABLE,
                    implementation_version="1", limits={"max_command_bytes": 2000, "max_output_bytes": MAX_OUTPUT_BYTES,
                        "timeout_seconds": self.default_timeout_seconds, "automatic_retries": False},
                    detail="Attributed remote command output, not verified system state.")
                for capability in SERVER_CAPABILITIES],
        )

    async def health(self) -> ProviderHealth:
        now = time.monotonic()
        for capability, until in list(self._cooldowns.items()):
            if now >= until:
                self._cooldowns.pop(capability, None)
                self._states[capability] = HealthStatus.DEGRADED
                self._reasons[capability] = "server_ssh_unverified"
        states = dict(self._states)
        if self._auth_failed or self._closed:
            states = dict.fromkeys(SERVER_CAPABILITIES, HealthStatus.UNAVAILABLE)
        values = set(states.values())
        overall = (HealthStatus.UNAVAILABLE if values == {HealthStatus.UNAVAILABLE} else
                   HealthStatus.HEALTHY if values == {HealthStatus.HEALTHY} else HealthStatus.DEGRADED)
        return ProviderHealth(provider_id="ssh-server", body_instance_id=self.body_instance_id, status=overall,
            capability_health=states, observed_at=datetime.now(UTC), detail={"reason_codes": dict(self._reasons)})

    async def status(self) -> EmbodimentStatus:
        async with self._activation_lock:
            if self._configuration_hash() != self._condition_hash:
                self.manager.set_terminal("server_provider_activation_changed")
            if not self._activated:
                await self.manager.activate()
                self._activated = True
            else:
                await self.manager.refresh()
            return self.manager.status()

    async def _guard(self, capability: str):
        status = await self.status()
        if (status.state in {EmbodimentState.DISABLED, EmbodimentState.TERMINAL, EmbodimentState.UNAVAILABLE}
                or capability not in status.effective_capabilities):
            raise ServerError("server_capability_unavailable")

    def _failure(self, capability: str, code: str, *, auth: bool = False):
        if auth:
            self._auth_failed = True
            self._states = dict.fromkeys(SERVER_CAPABILITIES, HealthStatus.UNAVAILABLE)
            self._reasons = dict.fromkeys(SERVER_CAPABILITIES, code)
        else:
            self._states[capability] = HealthStatus.UNAVAILABLE
            self._reasons[capability] = code
            self._cooldowns[capability] = time.monotonic() + COOLDOWN_SECONDS

    def _success(self, capability: str):
        self._states[capability] = HealthStatus.HEALTHY
        self._reasons[capability] = "server_ssh_available"
        self._cooldowns.pop(capability, None)

    async def _connection(self) -> asyncssh.SSHClientConnection:
        if self._conn is not None and not self._conn.is_closed():
            return self._conn
        fingerprint = self._fingerprint

        def client_factory():
            return _FingerprintClient(fingerprint)

        try:
            self._conn = await asyncssh.connect(
                self.host, port=self.port, username=self.username,
                client_keys=[self._private_key], known_hosts=self._known_hosts,
                client_factory=client_factory if fingerprint else None,
                connect_timeout=self.connect_timeout_seconds, login_timeout=self.connect_timeout_seconds,
                keepalive_interval=30, keepalive_count_max=3,
            )
            return self._conn
        except asyncssh.HostKeyNotVerifiable as exc:
            self._failure(SERVER_EXECUTE, "server_host_key_unverified", auth=True)
            raise ServerError("server_host_key_unverified") from exc
        except (asyncssh.PermissionDenied, asyncssh.KeyImportError) as exc:
            self._failure(SERVER_EXECUTE, "server_authentication_failed", auth=True)
            raise ServerError("server_authentication_failed") from exc
        except (TimeoutError, asyncssh.TimeoutError, asyncssh.ConnectionLost, OSError, asyncssh.Error) as exc:
            self._failure(SERVER_EXECUTE, "server_network_error")
            raise ServerError("server_network_error") from exc

    def _redact(self, text: str) -> str:
        secret = self._private_key_text.strip()
        if secret:
            text = text.replace(secret, "[redacted]")
        return text.replace("-----BEGIN OPENSSH PRIVATE KEY-----", "[redacted]")

    def _decode(self, data: bytes | str | None) -> tuple[str, bool]:
        if data is None:
            return "", False
        if isinstance(data, str):
            raw = data.encode("utf-8", "replace")
        else:
            raw = bytes(data)
        truncated = len(raw) > MAX_OUTPUT_BYTES
        text = raw[:MAX_OUTPUT_BYTES].decode("utf-8", "replace")
        return self._redact(text), truncated

    async def run(self, request: ServerCommandArguments, *, deadline: datetime | None = None,
                  capability: str = SERVER_EXECUTE) -> ServerCommandResult:
        try:
            request = ServerCommandArguments.model_validate(request)
        except (TypeError, ValueError):
            raise ServerError("server_command_arguments_invalid") from None
        await self._guard(capability)
        timeout = float(request.timeout_seconds or self.default_timeout_seconds)
        if deadline is not None:
            timeout = min(timeout, max(0.1, (deadline - datetime.now(UTC)).total_seconds()))
        remote = request.command
        if request.cwd:
            remote = f"cd {shlex.quote(request.cwd)} && {request.command}"
        started = time.monotonic()
        async with self._call_lock:
            try:
                if self._transport is not None:
                    exit_status, raw_stdout, raw_stderr = await asyncio.wait_for(
                        self._transport.execute(remote, timeout=timeout), timeout=timeout)
                else:
                    conn = await self._connection()
                    result = await conn.run(
                        remote, check=False, timeout=timeout, request_pty=False,
                        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
                    exit_status, raw_stdout, raw_stderr = result.exit_status, result.stdout, result.stderr
            except (asyncssh.TimeoutError, TimeoutError, asyncio.TimeoutError) as exc:
                self._failure(capability, "server_command_timeout")
                raise ServerError("server_command_timeout") from exc
            except ServerError:
                raise
            except (asyncssh.PermissionDenied, asyncssh.KeyImportError) as exc:
                self._failure(capability, "server_authentication_failed", auth=True)
                raise ServerError("server_authentication_failed") from exc
            except asyncssh.HostKeyNotVerifiable as exc:
                self._failure(capability, "server_host_key_unverified", auth=True)
                raise ServerError("server_host_key_unverified") from exc
            except (asyncssh.ConnectionLost, OSError, asyncssh.Error) as exc:
                self._conn = None
                self._failure(capability, "server_network_error")
                raise ServerError("server_network_error") from exc
            stdout, stdout_cut = self._decode(raw_stdout)
            stderr, stderr_cut = self._decode(raw_stderr)
            self._success(capability)
            return ServerCommandResult(
                status="completed", exit_code=int(exit_status if exit_status is not None else -1),
                stdout=stdout, stderr=stderr, truncated=stdout_cut or stderr_cut,
                duration_ms=round((time.monotonic() - started) * 1000, 3), risk_class="routine",
            )

    async def close(self) -> None:
        self._closed = True
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.close()
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()
            await conn.wait_closed()
