"""Forward the auxiliary Ollama endpoint over the existing pinned SSH host.

Requires Paramiko. Use a terminal password prompt, an existing password file,
or an existing SSH key/agent. The listener is always loopback only. Password
contents are never accepted in command arguments or written to disk.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
from pathlib import Path
import select
import socketserver

import paramiko


class PinnedHostKey(paramiko.MissingHostKeyPolicy):
    def __init__(self, fingerprint: str):
        self.fingerprint = fingerprint.removeprefix("SHA256:").rstrip("=")

    def missing_host_key(self, client, hostname, key):
        actual = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
        if actual != self.fingerprint:
            raise paramiko.SSHException("SSH host fingerprint mismatch")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="frp-fan.com")
    parser.add_argument("--ssh-port", type=int, default=54849)
    parser.add_argument("--username", default="qiuyu")
    parser.add_argument("--fingerprint", default="SHA256:GWuxOIlCKRP3SI7sjyyFld8tL45JqdhCtrb5VyfH7mw")
    parser.add_argument("--local-port", type=int, default=11435)
    parser.add_argument("--remote-port", type=int, default=11435)
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--password-file", type=Path, help="Read an existing UTF-8 password file")
    auth.add_argument("--key-file", type=Path, help="Use an existing SSH private key")
    auth.add_argument("--agent", action="store_true", help="Authenticate using the existing SSH agent")
    args = parser.parse_args()
    password = None
    if args.password_file:
        password = args.password_file.read_text(encoding="utf-8-sig").rstrip("\r\n")
        if not password:
            raise ValueError("SSH password file is empty")
    elif not args.key_file and not args.agent:
        password = getpass.getpass("SSH password: ")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(PinnedHostKey(args.fingerprint))
    try:
        client.connect(args.host, port=args.ssh_port, username=args.username,
                       password=password, look_for_keys=False, allow_agent=args.agent,
                       key_filename=str(args.key_file) if args.key_file else None,
                       timeout=15, auth_timeout=15, banner_timeout=15)
        password = None
        transport = client.get_transport()
        if transport is None:
            raise RuntimeError("SSH transport unavailable")
        transport.set_keepalive(30)

        class ForwardHandler(socketserver.BaseRequestHandler):
            def handle(self):
                channel = None
                try:
                    channel = transport.open_channel(
                        "direct-tcpip", ("127.0.0.1", args.remote_port),
                        self.request.getpeername(), timeout=10)
                    if channel is None:
                        return
                    while transport.is_active():
                        ready, _, _ = select.select([self.request, channel], [], [], 30)
                        if self.request in ready:
                            data = self.request.recv(65536)
                            if not data:
                                break
                            channel.sendall(data)
                        if channel in ready:
                            data = channel.recv(65536)
                            if not data:
                                break
                            self.request.sendall(data)
                except (OSError, EOFError, paramiko.SSHException):
                    pass
                finally:
                    if channel is not None:
                        channel.close()
                    self.request.close()

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        with Server(("127.0.0.1", args.local_port), ForwardHandler) as server:
            server.timeout = 1
            print(f"FORWARD_READY http://127.0.0.1:{args.local_port}/v1", flush=True)
            while transport.is_active():
                server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        password = None
        client.close()


if __name__ == "__main__":
    main()
