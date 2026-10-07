"""Background readiness probe and circuit state; never wait on the reply path."""
from __future__ import annotations

import threading
import time

import httpx


class ProviderHealth:
    def __init__(self, settings, *, interval=5.0, timeout=1.0, recovery_successes=2,
                 transport=None, clock=time.monotonic, start=True):
        self.interval = interval
        self.timeout = timeout
        self.recovery_successes = recovery_successes
        self._clock = clock
        self._url = settings.llm_base_url.rstrip("/") + "/models"
        self._model = settings.llm_model
        self._client = httpx.Client(timeout=timeout, transport=transport,
            headers={"Authorization": f"Bearer {settings.llm_api_key}"})
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._epoch = 0
        self._successes = 0
        self._ready = False
        self._checked = None
        self._reason = "initializing"
        self._thread = None
        if start:
            self._thread = threading.Thread(target=self._run, name="llm-readiness", daemon=True)
            self._thread.start()

    def snapshot(self):
        with self._lock:
            age = None if self._checked is None else max(0, self._clock() - self._checked)
            fresh = age is not None and age <= self.interval * 2 + self.timeout
            return {"ready": self._ready and fresh, "reason": self._reason if fresh else "unknown_or_stale",
                    "checked_age_seconds": age, "interval_seconds": self.interval,
                    "timeout_seconds": self.timeout, "recovery_successes": self.recovery_successes}

    def failed(self, reason="generation_unavailable"):
        with self._lock:
            self._epoch += 1
            self._ready = False
            self._successes = 0
            self._checked = self._clock()
            self._reason = reason

    def probe(self):
        with self._lock:
            epoch = self._epoch
        try:
            response = self._client.get(self._url)
            response.raise_for_status()
            body = response.json()
            ready = any(row.get("id") == self._model for row in body.get("data", [])
                        if isinstance(row, dict))
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            ready = False
        with self._lock:
            # A probe begun before a failed generation cannot close its circuit.
            if self._epoch != epoch:
                return
            self._checked = self._clock()
            self._successes = self._successes + 1 if ready else 0
            self._ready = self._successes >= self.recovery_successes
            self._reason = "ready" if self._ready else ("recovering" if ready else "probe_failed")

    def _run(self):
        while not self._stop.is_set():
            started = self._clock()
            self.probe()
            self._stop.wait(max(.05, self.interval - (self._clock() - started)))
        self._client.close()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.timeout + .1)
        else:
            self._client.close()
