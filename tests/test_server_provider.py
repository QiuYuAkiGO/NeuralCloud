"""In-process SSH transport: host pinning, truncation, timeout and redaction."""

from __future__ import annotations

import asyncio

import asyncssh
import pytest

from harness.embodiment.capabilities import EmbodimentState, SERVER_EXECUTE, SERVER_OBSERVE
from harness.embodiment.providers.server import SSHServerProvider, parse_host_key
from harness.embodiment.server_contracts import ServerCommandArguments, ServerError
from harness.research.manifest import EmbodimentCondition, CapabilityRequirement


def _condition():
    return EmbodimentCondition(mode="observe", body_instance_id="server-01",
        capability_requirements=[CapabilityRequirement(capability=name, required=False)
                                 for name in (SERVER_OBSERVE, SERVER_EXECUTE)])


async def _listener(handler, *, client_public, host_key):
    return await asyncssh.listen(
        "127.0.0.1", 0, server_host_keys=[host_key],
        authorized_client_keys=asyncssh.import_authorized_keys(client_public),
        process_factory=handler,
    )


def _export(key, kind):
    value = key.export_private_key("openssh") if kind == "private" else key.export_public_key("openssh")
    return value.decode() if isinstance(value, bytes) else value


@pytest.fixture
def keys():
    host = asyncssh.generate_private_key("ssh-ed25519")
    client = asyncssh.generate_private_key("ssh-ed25519")
    return host, client


def test_parse_host_key_accepts_fingerprint_and_openssh_line(keys):
    host, _ = keys
    public = _export(host, "public")
    kind, value = parse_host_key(public)
    assert kind == "key"
    fingerprint = host.convert_to_public().get_fingerprint("sha256")
    kind, value = parse_host_key(fingerprint)
    assert kind == "fingerprint" and value == fingerprint


def test_pinned_public_key_executes_and_truncates(keys):
    async def scenario():
        host, client = keys
        async def handle(process):
            command = process.command or ""
            try:
                if "sleep" in command:
                    try:
                        await asyncio.wait_for(process.stdin.read(-1), timeout=2)
                    except Exception:
                        pass
                elif "huge" in command:
                    process.stdout.write("x" * (70 * 1024))
                elif "BEGIN OPENSSH PRIVATE KEY" in command:
                    process.stdout.write(_export(client, "private"))
                else:
                    process.stdout.write(f"ran:{command}\n")
            finally:
                process.exit(0)
                process.close()
                await process.wait_closed()

        listener = await _listener(handle, client_public=_export(client, "public"), host_key=host)
        try:
            provider = SSHServerProvider(
                _condition(), host="127.0.0.1", port=listener.get_port(), username="misty-ops",
                host_key=_export(host, "public"), private_key=_export(client, "private"),
                body_instance_id="server-01", connect_timeout_seconds=5, default_timeout_seconds=5)
            status = await provider.status()
            assert status.state == EmbodimentState.DEGRADED
            result = await provider.run(ServerCommandArguments(command="uname -a"))
            assert result.exit_code == 0 and "ran:uname -a" in result.stdout
            huge = await provider.run(ServerCommandArguments(command="huge"))
            assert huge.truncated and len(huge.stdout.encode()) <= 64 * 1024
            leaked = await provider.run(ServerCommandArguments(command="BEGIN OPENSSH PRIVATE KEY"))
            assert "BEGIN OPENSSH PRIVATE KEY" not in leaked.stdout
            with pytest.raises(ServerError, match="server_command_timeout"):
                await provider.run(ServerCommandArguments(command="sleep", timeout_seconds=1))
            await provider.close()
        finally:
            listener.close()
            try:
                await asyncio.wait_for(listener.wait_closed(), timeout=5)
            except TimeoutError:
                pass
    asyncio.run(scenario())


def test_fingerprint_mismatch_is_rejected(keys):
    async def scenario():
        host, client = keys
        other = asyncssh.generate_private_key("ssh-ed25519")
        async def handle(process):
            process.exit(0)
            process.close()
            await process.wait_closed()
        listener = await _listener(handle, client_public=_export(client, "public"), host_key=host)
        try:
            provider = SSHServerProvider(
                _condition(), host="127.0.0.1", port=listener.get_port(), username="misty-ops",
                host_key=other.convert_to_public().get_fingerprint("sha256"),
                private_key=_export(client, "private"), body_instance_id="server-01")
            with pytest.raises(ServerError, match="server_host_key_unverified"):
                await provider.run(ServerCommandArguments(command="id"))
            await provider.close()
        finally:
            listener.close()
            try:
                await asyncio.wait_for(listener.wait_closed(), timeout=5)
            except TimeoutError:
                pass
    asyncio.run(scenario())
