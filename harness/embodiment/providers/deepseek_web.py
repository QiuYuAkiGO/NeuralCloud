"""DeepSeek Search / Fetch behind the existing attributed web contracts."""
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from harness.embodiment.capabilities import WEB_READ, WEB_SEARCH
from harness.embodiment.providers.web import WebProviderTransport, WebPageError
from harness.embodiment.web_contracts import WebError, WebResult, WebSearchArguments, WebSource, validate_public_url


class DeepSeekWebProvider(WebProviderTransport):
    provider_id = "deepseek-web"

    def __init__(self, condition, base_url, *args, **kwargs):
        # Never send the configured credential to a user-controlled origin.
        try:
            url = urlsplit(base_url)
            valid = (url.scheme == "https" and url.hostname == "api.deepseek.com"
                     and url.port in {None, 443} and url.path in {"", "/"}
                     and url.username is None and url.password is None and not url.query and not url.fragment)
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise WebError("web_api_endpoint_invalid")
        super().__init__(condition, base_url, *args, **kwargs)

    def _check_status(self, capability, status):
        if capability == WEB_READ and status in {403, 404}:
            raise WebPageError("web_fetch_blocked" if status == 403 else "web_fetch_unavailable")
        if status == 402:
            raise WebError("web_balance_insufficient")
        super()._check_status(capability, status)

    async def search(self, request, *, before_dispatch=None):
        try:
            request = WebSearchArguments.model_validate(request)
            query = request.query
            if request.time_range:
                days = {"day": 1, "week": 7, "month": 30, "year": 365}[request.time_range]
                query += " after:" + (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
            if len(query) > 128:
                raise ValueError()
        except (TypeError, ValueError):
            raise WebError("web_search_arguments_invalid") from None
        async with self._call_lock:
            value = await self._post(WEB_SEARCH, "/v1/tools/search",
                {"query": query, "count": request.max_results, "need_content": False}, before_dispatch=before_dispatch)
            try:
                rows = value.get("results")
                if not isinstance(rows, list) or len(rows) > 50:
                    raise WebError("web_response_invalid")
                sources, failures, seen = [], [], set()
                for index, row in enumerate(rows[:request.max_results]):
                    try:
                        if not isinstance(row, dict):
                            raise WebError("web_response_invalid")
                        url = validate_public_url(row.get("url"))
                        if url in seen:
                            continue
                        title, cut_title = self._text(row.get("title", ""), 1000)
                        # Both fields are provider summaries, never read evidence.
                        snippet, cut_snippet = self._text(row.get("summary") or row.get("snippet", ""), 2000)
                        date = row.get("date")
                        date = self._text(date, 128)[0] if isinstance(date, str) else None
                        sources.append(WebSource(title=title, url=url, snippet=snippet, published_at=date,
                            retrieved_at=datetime.now(UTC), truncated=cut_title or cut_snippet))
                        seen.add(url)
                    except (WebError, ValueError, TypeError):
                        failures.append({"index": index, "code": "web_search_result_invalid"})
                result = WebResult(status="partial" if failures else "completed", sources=sources, failures=failures)
            except WebError as exc:
                self._failure(WEB_SEARCH, exc.code)
                raise
            self._success(WEB_SEARCH)
            return result

    async def read(self, urls, *, before_dispatch=None):
        if not isinstance(urls, list) or not 1 <= len(urls) <= 3:
            raise WebError("web_read_arguments_invalid")
        urls = [validate_public_url(url) for url in urls]
        if len(set(urls)) != len(urls):
            raise WebError("web_read_arguments_invalid")
        sources, failures = [], []
        async with self._call_lock:
            for url in urls:
                try:
                    value = await self._post(WEB_READ, "/v1/tools/fetch", {"url": url}, before_dispatch=before_dispatch)
                    title, title_cut = self._text(value.get("title", ""), 1000)
                    content, content_cut = self._text(value.get("content"), 12000)
                    if not content.strip():
                        raise WebPageError("web_fetch_empty")
                    sources.append(WebSource(origin="read", title=title, url=url, content=content,
                        retrieved_at=datetime.now(UTC), truncated=title_cut or content_cut))
                except WebPageError as exc:
                    failures.append({"url": url, "code": exc.code})
            self._success(WEB_READ)
        return WebResult(status="partial" if failures else "completed", sources=sources, failures=failures)


def web_provider_class(settings):
    if settings.provider == "deepseek":
        return DeepSeekWebProvider
    from harness.embodiment.providers.web import TavilyWebProvider
    return TavilyWebProvider
