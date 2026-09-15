"""Retry policy for side-effect-free QQ model requests, never GUI delivery."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math

import httpx


def transient_retry_delay(error: Exception) -> float | None:
    """Allow one short retry; long server backoffs remain explicit failures."""
    if isinstance(error, httpx.HTTPStatusError):
        if error.response.status_code not in {408, 429, 500, 502, 503, 504}:
            return None
        header = error.response.headers.get("Retry-After")
        if header:
            try:
                delay = float(header)
            except ValueError:
                try:
                    moment = parsedate_to_datetime(header)
                    delay = (moment - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    delay = 0.25
            if not math.isfinite(delay) or delay > 2:
                return None
            return max(0.25, delay)
        return 0.25
    if isinstance(error, (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError)):
        return 0.25
    return None
