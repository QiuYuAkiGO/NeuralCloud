"""Loopback-only semantic client; no network model fallback."""

import json
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class SemanticClient:
    def __init__(self, url, database_id=None, token=None):
        parts = urlsplit(url)
        if parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.username or parts.password:
            raise ValueError("LOOPBACK_SEMANTIC_REQUIRED")
        self.url = url.rstrip("/")
        self.database_id = database_id
        if not token or len(token) < 32:
            raise ValueError("SEMANTIC_CREDENTIAL_REQUIRED")
        self.token = token

    def search(self, query, limit=50, timeout=1.2):
        request = Request(
            self.url + "/search",
            data=json.dumps({"query": query, "limit": limit}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token},
        )
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(128 * 1024 + 1)
            if len(raw) > 128 * 1024:
                raise ValueError("SEMANTIC_RESPONSE_BOUNDS")
            result = json.loads(raw)
        if result.get("status") != "READY":
            raise ValueError("SEMANTIC_NOT_READY")
        if self.database_id and result.get("database_id") != self.database_id:
            raise ValueError("SEMANTIC_DATABASE_MISMATCH")
        return result["items"]
