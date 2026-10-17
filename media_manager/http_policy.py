"""Bounded retries without replaying ambiguous quota-consuming POST requests."""
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from .utils import MediaManagerError

_cooldowns = {}


class ProviderDeferred(MediaManagerError):
    def __init__(self, retry_at, reason="Provider temporarily unavailable"):
        self.retry_at = retry_at
        super().__init__(reason)


def retry_delay(headers, fallback):
    value = headers.get("Retry-After") if headers else None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return fallback


def open_with_retry(request, *, opener, timeout=30):
    host = urlsplit(request.full_url).hostname
    boundary = _cooldowns.get(host, 0)
    if boundary > time.time():
        raise ProviderDeferred(datetime.fromtimestamp(boundary, timezone.utc).isoformat())
    # A 429 explicitly rejected the operation. Network/5xx failures may have
    # consumed a download quota: only replay those automatically for GET.
    safe = request.get_method() == "GET"
    for attempt in range(3):
        try:
            return opener(request, timeout=timeout)
        except HTTPError as exc:
            if exc.code != 429 and not (safe and exc.code in {500, 502, 503, 504}):
                raise
            delay = retry_delay(exc.headers, 2 ** (attempt + 1) + random.uniform(0, 1))
            exc.close()
        except (URLError, TimeoutError):
            if not safe:
                raise
            delay = 2 ** (attempt + 1) + random.uniform(0, 1)
        if attempt == 2 or delay > 10:
            _cooldowns[host] = time.time() + max(delay, 1)
            retry_at = datetime.fromtimestamp(_cooldowns[host], timezone.utc).isoformat()
            raise ProviderDeferred(retry_at)
        time.sleep(delay)
