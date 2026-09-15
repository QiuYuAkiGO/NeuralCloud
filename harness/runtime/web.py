"""Runtime authority, scoped evidence and durable budgets for network reads."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

from harness.contracts.capability_calls import CapabilityOffer
from harness.embodiment.web_contracts import (
    WebError, WebReadArguments, WebResult, WebSearchArguments, WebSource, validate_public_url,
)
from harness.runtime.models import RuntimeLifecycle, utc_now

SEARCH = "web.search.v1"
READ = "web.read.v1"
_URL = re.compile(r'https?://[^\s<>"\[\]{}，。；！？、（）]+', re.I)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class WebCallStore:
    """A request reservation survives failure, cancellation and process restart."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, capability TEXT NOT NULL,
                fingerprint TEXT NOT NULL, day TEXT NOT NULL, autonomous INTEGER NOT NULL,
                started_at TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS sources (
                scope TEXT NOT NULL, ref TEXT NOT NULL, record TEXT NOT NULL,
                PRIMARY KEY(scope, ref)
            );
            CREATE TABLE IF NOT EXISTS rounds (id TEXT PRIMARY KEY, record TEXT NOT NULL);
        """)
        # A started POST is never replayed, including after process restart.
        # Opening another connection must not mutate a live owner's reservation.

    def call(self, identity):
        cursor = self.db.execute("SELECT * FROM calls WHERE id=?", (identity,))
        row = cursor.fetchone()
        return dict(zip([item[0] for item in cursor.description], row)) if row else None

    def reserve(self, identity, scope, capability, fingerprint, day, autonomous, limit, autonomous_limit):
        # SQLite's transaction protects shared quota even across controller instances.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            previous = self.call(identity)
            if previous:
                if (previous["fingerprint"], previous["scope"], previous["capability"]) != (fingerprint, scope, capability):
                    raise WebError("web_call_conflict")
                self.db.rollback()
                return previous
            total, auto = self.usage(day)
            if total >= limit or autonomous and auto >= autonomous_limit:
                raise WebError("web_budget_exhausted")
            self.db.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,'started',NULL,NULL)",
                (identity, scope, capability, fingerprint, day, int(autonomous), utc_now().isoformat()))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return None

    def finish(self, identity, *, result=None, error=None, uncertain=False):
        with self.db:
            self.db.execute("UPDATE calls SET status=?, result=?, error=? WHERE id=?",
                ("completed" if result is not None else "uncertain" if uncertain else "failed",
                 _json(result) if result is not None else None, error, identity))

    def usage(self, day):
        row = self.db.execute("SELECT COUNT(*),COALESCE(SUM(autonomous),0) FROM calls WHERE day=?", (day,)).fetchone()
        return int(row[0]), int(row[1])

    def put_source(self, scope, source):
        with self.db:
            self.db.execute("INSERT INTO sources VALUES (?,?,?) ON CONFLICT(scope,ref) DO UPDATE SET record=excluded.record",
                (scope, source.source_ref, source.model_dump_json()))

    def source(self, scope, ref):
        row = self.db.execute("SELECT record FROM sources WHERE scope=? AND ref=?", (scope, ref)).fetchone()
        return WebSource.model_validate_json(row[0]) if row else None

    def sources(self, scope):
        return [WebSource.model_validate_json(row[0]) for row in self.db.execute(
            "SELECT record FROM sources WHERE scope=? ORDER BY rowid DESC LIMIT 10", (scope,))]

    def round(self, identity):
        row = self.db.execute("SELECT record FROM rounds WHERE id=?", (identity,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_round(self, identity, value):
        with self.db:
            self.db.execute("INSERT INTO rounds VALUES (?,?) ON CONFLICT(id) DO UPDATE SET record=excluded.record",
                            (identity, _json(value)))

    def close(self):
        self.db.close()


class WebController:
    def __init__(self, supervisor, settings, state_dir, *, provider=None, config_store=None, test_provider_factory=None):
        self.supervisor = supervisor
        self.settings = settings
        self.provider = provider
        self.config_store = config_store
        self.test_provider_factory = test_provider_factory
        self.calls = WebCallStore(Path(state_dir) / "web" / "calls.sqlite3")
        self._lock = asyncio.Lock()
        self._provider_identity = id(provider)
        self._revision = self._configuration()
        self._closed = False

    def _configuration(self):
        return _hash({"settings": self.settings.model_dump(mode="json") if self.settings else None,
            "authorization": self.supervisor.manifest.web_access.model_dump(mode="json"),
            "config_revision": self.config_store.snapshot()["active_revision"] if self.config_store else 0})

    @property
    def enabled(self):
        return bool(self.settings and self.provider and self.supervisor.manifest.web_access.enabled)

    @property
    def conversation_enabled(self):
        return self.enabled and self.supervisor.manifest.web_access.conversation_enabled

    def _day(self):
        return utc_now().astimezone(ZoneInfo(self.supervisor.manifest.timezone)).date().isoformat()

    def _permitted(self, capability, autonomous):
        if not self.enabled or self._configuration() != self._revision or id(self.provider) != self._provider_identity:
            return False
        if self.supervisor.lifecycle != RuntimeLifecycle.RUNNING:
            return False
        if autonomous:
            config = self.supervisor.manifest.autonomous_capabilities
            return config.enabled and config.owners.get("web", "off") == "shared" and capability in config.allowed_capabilities
        return self.supervisor.manifest.web_access.conversation_enabled

    def register_urls(self, scope, text):
        for value in _URL.findall(text)[:3]:
            try:
                url = validate_public_url(value.rstrip(".,;:!?，。；！？）)"))
            except (ValueError, WebError):
                continue
            ref = "web-source:" + _hash([scope, url])[:32]
            if self.calls.source(scope, ref) is None:
                self.calls.put_source(scope, WebSource(source_ref=ref, origin="user_link", title="用户提供的链接", url=url,
                    retrieved_at=utc_now()))

    def context(self, scope, *, failures=()):
        sources, remaining = [], 12000
        for source in self.calls.sources(scope):
            value = source.model_dump(mode="json")
            content = value["content"][:min(8000, max(0, remaining - 500))]
            snippet = value["snippet"][:min(1200, max(0, remaining - len(content) - 500))]
            value.update(content=content, snippet=snippet,
                         truncated=value["truncated"] or content != source.content or snippet != source.snippet)
            size = len(_json(value))
            if size > remaining:
                continue
            sources.append(value)
            remaining -= size
        return {"status": "partial" if failures else "completed", "sources": sources,
                "failures": list(failures)[:6],
                "evidence_refs": [s["source_ref"] for s in sources if s["origin"] != "user_link"]}

    async def catalog(self, scope="autonomous", *, autonomous=True, deadline=None):
        if not self.enabled:
            return []
        status = await self.provider.status()
        now = utc_now()
        total, auto = self.calls.usage(self._day())
        within_budget = total < self.settings.daily_requests and (not autonomous or auto < self.settings.autonomous_daily_requests)
        context = self.context(scope)
        offers = []
        for capability, model in ((SEARCH, WebSearchArguments), (READ, WebReadArguments)):
            supported = capability in status.activated_capabilities
            available = (supported and capability in status.effective_capabilities
                         and status.state.value in {"available", "degraded"} and self._permitted(capability, autonomous))
            ready = available and within_budget and (capability == SEARCH or bool(context["sources"]))
            schema = model.model_json_schema()
            if capability == SEARCH:
                schema["properties"]["max_results"].update(maximum=self.settings.max_results,
                                                         default=min(5, self.settings.max_results))
            offers.append(CapabilityOffer(offer_id="web:" + capability, channel="web", capability_id=capability,
                provider_id=status.provider_id or "tavily", body_instance_id=self.settings.body_instance_id,
                scope={"context_id": scope}, activation_revision=self._revision,
                input_schema=schema, observation=context, observation_refs=context["evidence_refs"],
                supported=supported, available=available, ready=ready,
                valid_until=min(deadline, now + timedelta(seconds=30)) if deadline else now + timedelta(seconds=30)))
        return offers

    @staticmethod
    def _binding(offer):
        return (offer.provider_id, offer.body_instance_id, offer.scope, offer.activation_revision, offer.capability_id)

    def _replay_or_raise(self, previous, fingerprint, scope):
        if previous["fingerprint"] != fingerprint or previous["scope"] != scope:
            raise WebError("web_call_conflict")
        if previous["status"] != "completed":
            raise WebError(previous["error"] or "web_result_uncertain")
        result = WebResult.model_validate_json(previous["result"])
        for source in result.sources:
            if self.calls.source(scope, source.source_ref) is None:
                self.calls.put_source(scope, source)
        return result

    async def execute(self, identity, offer, arguments, *, deadline, autonomous=False, guard=None):
        async with self._lock:
            if not self.enabled:
                raise WebError("web_not_enabled")
            if utc_now() >= deadline:
                raise WebError("web_deadline_exceeded")
            scope = offer.scope.get("context_id")
            if not isinstance(scope, str):
                raise WebError("web_scope_invalid")
            capability = offer.capability_id
            if capability == SEARCH:
                request = WebSearchArguments.model_validate({"max_results": min(5, self.settings.max_results), **arguments})
                if request.max_results > self.settings.max_results:
                    raise WebError("web_results_limit_exceeded")
                payload = request.model_dump(mode="json")
            elif capability == READ:
                request = WebReadArguments.model_validate(arguments)
                records = [self.calls.source(scope, ref) for ref in request.source_refs]
                if any(record is None for record in records):
                    raise WebError("web_source_not_in_scope")
                payload = {"urls": [validate_public_url(record.url) for record in records]}
            else:
                raise WebError("web_capability_unknown")
            fingerprint = _hash([payload, self._binding(offer)])
            previous = self.calls.call(identity)
            if previous:
                return self._replay_or_raise(previous, fingerprint, scope)
            fresh = await self.catalog(scope, autonomous=autonomous, deadline=deadline)
            current = next((item for item in fresh if item.capability_id == capability), None)
            if (not current or not current.ready or self._binding(current) != self._binding(offer)
                    or offer.valid_until <= utc_now() or guard and guard()):
                raise WebError("web_call_superseded")
            if utc_now() >= deadline:
                raise WebError("web_deadline_exceeded")
            # No await between the last authority check, reservation and dispatch.
            reserved = self.calls.reserve(identity, scope, capability, fingerprint, self._day(), autonomous,
                                          self.settings.daily_requests, self.settings.autonomous_daily_requests)
            if reserved:
                return self._replay_or_raise(reserved, fingerprint, scope)
            try:
                timeout = min(self.settings.timeout_seconds, (deadline - utc_now()).total_seconds())
                async with asyncio.timeout(timeout):
                    result = await self.provider.search(request) if capability == SEARCH else await self.provider.read(payload["urls"])
                if utc_now() >= deadline or not self._permitted(capability, autonomous) or guard and guard():
                    raise WebError("web_late_result")
                result = self._store_result(identity, scope, result)
            except BaseException as exc:
                code = exc.code if isinstance(exc, WebError) else "web_timeout" if isinstance(exc, TimeoutError) else "web_request_failed"
                self.calls.finish(identity, error=code, uncertain=isinstance(exc, (TimeoutError, asyncio.CancelledError)))
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise WebError(code) from None
            # A journal failure must not erase a durable successful POST result.
            self._journal(identity, capability, result)
            return result

    def _store_result(self, identity, scope, result):
        result = WebResult.model_validate(result.model_dump())
        sources = []
        for item in result.sources:
            url = validate_public_url(item.url)
            ref = "web-source:" + _hash([scope, url])[:32]
            previous = self.calls.source(scope, ref)
            source = item.model_copy(update={"source_ref": ref, "url": url,
                "title": item.title or (previous.title if previous else ""),
                "snippet": item.snippet or (previous.snippet if previous else ""),
                "content": item.content[:8000], "truncated": item.truncated or len(item.content) > 8000})
            sources.append(source)
        normalized = result.model_copy(update={"sources": sources,
            "evidence_refs": ["web-call:" + identity] + [source.source_ref for source in sources]})
        # Publish the result and scoped evidence in one durable transaction.
        with self.calls.db:
            self.calls.db.execute("UPDATE calls SET status='completed', result=?, error=NULL WHERE id=?",
                                 (normalized.model_dump_json(), identity))
            for source in sources:
                previous = self.calls.source(scope, source.source_ref)
                if source.origin == "search" and previous is not None and previous.origin == "read" and previous.content:
                    source = previous
                self.calls.db.execute("INSERT INTO sources VALUES (?,?,?) ON CONFLICT(scope,ref) DO UPDATE SET record=excluded.record",
                                     (scope, source.source_ref, source.model_dump_json()))
        return normalized

    def _journal(self, identity, capability, result):
        append = getattr(self.supervisor, "_append_journal", None)
        if append:
            append("web_request_completed", data={"call_id": identity, "capability": capability,
                "source_count": len(result.sources), "provider_request_id": result.provider_request_id,
                "usage": result.usage, "evidence_refs": result.evidence_refs})

    async def status(self):
        body = await self.provider.status() if self.provider is not None else None
        total, auto = self.calls.usage(self._day())
        auto_config = self.supervisor.manifest.autonomous_capabilities
        auto_authorized = auto_config.owners.get("web", "off") == "shared" and bool(
            {SEARCH, READ} & set(auto_config.allowed_capabilities))
        return {"enabled": self.enabled, "conversation_enabled": self.conversation_enabled,
            "autonomous_enabled": self.enabled and auto_authorized and auto_config.enabled,
            "autonomous_global_enabled": auto_config.enabled,
            "authorization": {**self.supervisor.manifest.web_access.model_dump(mode="json"),
                              "autonomous_enabled": auto_authorized},
            "body": body.model_dump(mode="json") if body else None,
            "usage": {"day": self._day(), "requests": total, "autonomous_requests": auto,
                "daily_limit": self.settings.daily_requests if self.settings else 100,
                "autonomous_daily_limit": self.settings.autonomous_daily_requests if self.settings else 20}}

    async def test(self, settings, api_key):
        if self.test_provider_factory is None:
            raise WebError("web_test_unavailable")
        async with self._lock:
            provider = self.test_provider_factory(settings, api_key)
            scope = "test:" + uuid4().hex
            quota = self.settings or settings
            outcomes = []
            try:
                urls = []
                for capability in (SEARCH, READ):
                    identity = scope + ":" + capability
                    self.calls.reserve(identity, scope, capability, _hash([scope, capability]), self._day(), False,
                                       quota.daily_requests, quota.autonomous_daily_requests)
                    try:
                        async with asyncio.timeout(settings.timeout_seconds):
                            result = await provider.search(WebSearchArguments(query="Tavily documentation", max_results=1)) if capability == SEARCH else await provider.read(urls)
                        result = self._store_result(identity, scope, result)
                        outcomes.append({"capability": capability, "status": result.status, "source_count": len(result.sources)})
                        urls = [source.url for source in result.sources[:1]]
                        if not urls:
                            break
                    except BaseException as exc:
                        code = exc.code if isinstance(exc, WebError) else "web_test_failed"
                        self.calls.finish(identity, error=code, uncertain=isinstance(exc, (TimeoutError, asyncio.CancelledError)))
                        if isinstance(exc, asyncio.CancelledError):
                            raise
                        raise WebError(code) from None
                complete = len(outcomes) == 2 and all(item["status"] == "completed" and item["source_count"] for item in outcomes)
                return {"status": "completed" if complete else "partial", "checks": outcomes,
                        "activated": False}
            finally:
                await provider.close()

    async def close(self):
        if not self._closed:
            self._closed = True
            if self.provider is not None:
                await self.provider.close()
            self.calls.close()
