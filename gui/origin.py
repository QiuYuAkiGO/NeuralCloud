"""Origin protection for the GUI's authenticated reverse-proxy deployment.

Authentication belongs to the private ingress proxy. This middleware prevents a
browser carrying those credentials from issuing cross-site mutations or opening
a cross-site WebSocket. It never treats forwarded headers as authorization.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.datastructures import Headers, URL
from starlette.responses import JSONResponse


def normalize_origin(value: str) -> str:
    if any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("origin cannot contain whitespace or control characters")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("origin must contain only an HTTP(S) scheme and authority")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    if port is not None and port != {"http": 80, "https": 443}[parsed.scheme]:
        host += f":{port}"
    return f"{parsed.scheme}://{host}"


class GUIOriginMiddleware:
    def __init__(self, app, public_origin: str = ""):
        self.app = app
        self.public_origin = normalize_origin(public_origin) if public_origin else ""
        if self.public_origin and not self.public_origin.startswith("https://"):
            raise ValueError("NC_PUBLIC_ORIGIN requires HTTPS")

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        checked = kind == "websocket" or (kind == "http" and scope["method"] not in {"GET", "HEAD", "OPTIONS"})
        if not checked:
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        supplied = headers.get("origin")
        allowed = headers.get("sec-fetch-site") != "cross-site"
        if not supplied:
            # Existing loopback/non-browser integrations remain compatible.
            # Public deployments require browser proof of the expected origin.
            allowed = allowed and not self.public_origin
        else:
            try:
                url = URL(scope=scope)
                scheme = {"ws": "http", "wss": "https"}.get(url.scheme, url.scheme)
                expected = self.public_origin or normalize_origin(f"{scheme}://{url.netloc}")
                allowed = allowed and normalize_origin(supplied) == expected
            except ValueError:
                allowed = False
        if not allowed:
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse({"detail": "same_origin_required"}, status_code=403)(scope, receive, send)
            return
        return await self.app(scope, receive, send)
