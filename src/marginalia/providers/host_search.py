"""The model host's web search API, for the start-of-run web lookup (step 4).

The first host with one is DeepSeek's Search API.

`POST {host}/v1/tools/search` with the model account's own bearer key; one
billed request per successful search with a non-empty `results` array ($0.75
per 1,000; failed, empty and timed-out searches are not billed). Each result
carries a `summary` written for a model to read, which is all the lookup uses
— never `content`, so no whole page is ever fetched or stored.

Plain ``urllib``, as the YouTube API is reached: one POST returning JSON, and
an HTTP library would buy nothing. The transport is injectable so tests never
touch the network.
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from ..contracts import WebResult
from .base import PermanentError, TransientError, WebSearchRefused

log = logging.getLogger(__name__)

__all__ = ["HostSearchProvider", "COST_PER_SEARCH", "MAX_QUERY_CHARS"]

COST_PER_SEARCH = 0.75 / 1000
# The API's own limit on a query, in Unicode characters.
MAX_QUERY_CHARS = 128
_MAX_COUNT = 50
_TIMEOUT_S = 30.0
# Refusals that a retry cannot change: no balance, a missing or rejected key.
_REFUSED = frozenset({401, 402, 403})
# What the docs say to wait on and retry.
_RETRYABLE = frozenset({408, 429, 500, 502, 503, 504})

# (url, body bytes, headers, timeout) -> (status, response bytes)
Transport = Callable[[str, bytes, dict, float], tuple[int, bytes]]


def _urllib_post(url: str, body: bytes, headers: dict, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""


class HostSearchProvider:
    name = "host_search"

    def __init__(self, api_key: str | None, base_url: str, *, transport: Transport | None = None) -> None:
        if not api_key:
            raise WebSearchRefused("no API key for the web search provider")
        parts = urllib.parse.urlsplit(base_url)
        # The model's base URL may carry a path (`/v1`); the search endpoint is
        # on the same host.
        self._url = f"{parts.scheme or 'https'}://{parts.netloc}/v1/tools/search"
        self._key = api_key
        self._post = transport or _urllib_post

    def cost_per_search(self) -> float:
        return COST_PER_SEARCH

    def search(self, query: str, count: int = 10) -> list[WebResult]:
        query = " ".join(query.split())[:MAX_QUERY_CHARS]
        if not query:
            raise PermanentError("an empty query")
        body = json.dumps({"query": query, "count": max(1, min(_MAX_COUNT, count))}).encode("utf-8")
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        try:
            status, raw = self._post(self._url, body, headers, _TIMEOUT_S)
        except (TimeoutError, socket.timeout) as exc:
            raise TransientError("timeout") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise TransientError(f"connection failed ({type(exc).__name__})") from exc
        # Status codes only, never the body: an error body can echo the request.
        if status in _REFUSED:
            raise WebSearchRefused(f"refused (HTTP {status})")
        if status in _RETRYABLE:
            raise TransientError(f"HTTP {status}")
        if status != 200:
            raise PermanentError(f"HTTP {status}")
        try:
            data = json.loads(raw.decode("utf-8"))
            items = data["results"]
            if not isinstance(items, list):
                raise TypeError("results is not a list")
        except (ValueError, KeyError, TypeError) as exc:
            raise PermanentError(f"unreadable response ({type(exc).__name__})") from exc
        return [result for result in (_result(item) for item in items) if result is not None]


def _result(item: Any) -> WebResult | None:
    if not isinstance(item, dict) or not item.get("url"):
        return None
    url = str(item["url"])
    domain = urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.")
    score = item.get("rerank_score")
    return WebResult(
        title=str(item.get("title") or "")[:300],
        url=url[:2000],
        domain=domain,
        summary=str(item.get("summary") or item.get("snippet") or "")[:1200],
        date=item.get("date") if isinstance(item.get("date"), str) else None,
        score=float(score) if isinstance(score, (int, float)) else None,
    )
