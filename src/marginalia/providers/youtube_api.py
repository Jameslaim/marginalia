"""YouTube Data API v3 search — the primary search provider.

Two calls per search, because ``search.list`` returns thin results:

1. ``search.list``  — 100 units. Gives IDs, titles, channels, descriptions.
2. ``videos.list``  — 1 unit. Fills in ``duration_s`` and ``view_count`` for
   every ID at once. One batched call, never one per video.

The default allowance is 10,000 units a day, so roughly 100 searches. Spend is
tracked locally in the ledger's ``quota`` table and checked *before* each
request: a request that would exceed the cap raises :class:`QuotaExhausted`,
which the provider chain treats as "fall through to the overflow provider".
Firing it anyway would earn an opaque 403 that looks identical to a revoked
key.

HTTP is plain ``urllib`` from the standard library. The API is two GETs
returning JSON; a dependency would buy nothing.
"""

from __future__ import annotations

import html
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .base import (
    PermanentError,
    ProviderUnavailable,
    QuotaExhausted,
    QuotaLedger,
    SearchResult,
    TransientError,
)

log = logging.getLogger(__name__)

__all__ = ["YouTubeApiProvider", "SEARCH_COST", "VIDEOS_COST", "DEFAULT_DAILY_UNITS"]

API_ROOT = "https://www.googleapis.com/youtube/v3"

# Published costs. They are the API's, not ours — do not "tune" them.
SEARCH_COST = 100
VIDEOS_COST = 1
DEFAULT_DAILY_UNITS = 10_000

# The API's own per-page ceiling. Asking for more silently returns 50, so we
# clamp and say so rather than reporting a limit we did not honour.
_MAX_PAGE = 50

_TIMEOUT_S = 20

# PT1H2M3S -> hours, minutes, seconds. Days appear on nothing we search for,
# but P#DT#H is legal, so it is handled.
_DURATION_RE = re.compile(
    r"\AP(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?\Z"
)

# Google's error `reason` strings, and what each one means for the chain.
# Falling through on the wrong one hides a mistake the user has to fix.
_QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded", "userRateLimitExceeded"})
_TRANSIENT_REASONS = frozenset({"rateLimitExceeded", "backendError", "internalError"})


class HttpStatusError(Exception):
    """A non-2xx response, with its body — the body carries the real reason."""

    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


Transport = Callable[[str], bytes]


class YouTubeApiProvider:
    """Search via the official API. Metered, keyed, sanctioned."""

    name = "youtube_api"

    def __init__(
        self,
        api_key: str | None,
        *,
        quota: QuotaLedger | None = None,
        daily_units: int = DEFAULT_DAILY_UNITS,
        transport: Transport | None = None,
    ) -> None:
        if not api_key:
            raise ProviderUnavailable(
                "no API key: set YOUTUBE_API_KEY or providers.youtube_api_key in config.toml"
            )
        self._api_key = api_key
        self._quota = quota
        self._daily_units = daily_units
        self._get = transport or _urlopen

    # -- public API --------------------------------------------------------

    def search(self, query: str, limit: int = 25) -> list[SearchResult]:
        if not query.strip():
            # A bad query is the caller's mistake. Surfacing beats falling
            # through to a second provider that will fail the same way.
            raise PermanentError("empty query")

        wanted = max(1, int(limit))
        if wanted > _MAX_PAGE:
            log.warning(
                "youtube_api: asked for %d results, returning %d — "
                "each further page costs another %d units (pagination deferred)",
                wanted,
                _MAX_PAGE,
                SEARCH_COST,
            )
            wanted = _MAX_PAGE

        payload = self._call(
            "search",
            SEARCH_COST,
            part="snippet",
            type="video",
            maxResults=str(wanted),
            q=query,
        )

        results = [item for item in _items(payload) if _video_id(item)]
        if not results:
            return []

        details = self._details([_video_id(item) for item in results])

        return [self._to_result(item, details) for item in results]

    def quota_remaining(self) -> int | None:
        if self._quota is None:
            return None
        return max(0, self._daily_units - self._quota.quota_used(self.name))

    # -- internals ---------------------------------------------------------

    def _details(self, video_ids: list[str]) -> dict[str, dict[str, Any]]:
        """One batched ``videos.list`` for every ID. Never one call per video."""
        try:
            payload = self._call(
                "videos",
                VIDEOS_COST,
                part="contentDetails,statistics",
                id=",".join(video_ids),
            )
        except QuotaExhausted:
            # The search itself is already paid for and in hand. Losing
            # duration and view count is a smaller loss than discarding
            # results we spent 100 units on.
            log.warning("youtube_api: quota ran out before videos.list; results lack duration")
            return {}
        return {item.get("id"): item for item in _items(payload) if item.get("id")}

    def _to_result(self, item: dict[str, Any], details: dict[str, dict[str, Any]]) -> SearchResult:
        video_id = _video_id(item)
        assert video_id is not None  # filtered by the caller
        snippet = item.get("snippet") or {}
        detail = details.get(video_id) or {}

        return SearchResult(
            video_id=video_id,
            title=_text(snippet.get("title")),
            channel=_text(snippet.get("channelTitle")),
            description=_text(snippet.get("description")),
            duration_s=_duration_seconds((detail.get("contentDetails") or {}).get("duration")),
            published_at=snippet.get("publishedAt") or None,
            view_count=_int_or_none((detail.get("statistics") or {}).get("viewCount")),
            provider=self.name,
        )

    def _call(self, endpoint: str, cost: int, **params: str) -> dict[str, Any]:
        self._reserve(cost)
        url = f"{API_ROOT}/{endpoint}?" + urllib.parse.urlencode(
            {**params, "key": self._api_key}
        )
        try:
            raw = self._get(url)
        except HttpStatusError as exc:
            raise _classify(exc) from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise TransientError(f"unreadable response from {endpoint}.list") from exc
        if not isinstance(payload, dict):
            raise TransientError(f"unexpected response shape from {endpoint}.list")
        return payload

    def _reserve(self, cost: int) -> None:
        """Charge the cost before sending. Refuse rather than get a 403."""
        if self._quota is None:
            return
        used = self._quota.quota_used(self.name)
        if used + cost > self._daily_units:
            raise QuotaExhausted(
                f"quota exhausted: {used}/{self._daily_units} units used today, "
                f"this call costs {cost}"
            )
        self._quota.spend_quota(self.name, cost)


# -- parsing ---------------------------------------------------------------


def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("items")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _video_id(item: dict[str, Any]) -> str | None:
    identifier = item.get("id")
    if isinstance(identifier, dict):
        identifier = identifier.get("videoId")
    return identifier if isinstance(identifier, str) and identifier else None


def _text(value: Any) -> str:
    """Unescape, then collapse whitespace.

    The API HTML-escapes every ``snippet`` string: a title reads
    ``Faster &amp; Cheaper`` and an apostrophe arrives as ``&#39;``. yt-dlp does
    not do this, so leaving it would mean the same video carries a different
    title depending on which provider happened to answer — which defeats the
    entire point of the ``SearchProvider`` seam, and would put raw entities in
    front of M2b's judge.

    Unescaped exactly once. A title containing a literal ``&amp;`` arrives from
    the API as ``&amp;amp;``, so a second pass would corrupt it.
    """
    return " ".join(html.unescape(str(value)).split()) if value else ""


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _duration_seconds(value: Any) -> int | None:
    """``"PT18M32S"`` -> ``1112``. Live streams report ``P0D``; that is None."""
    if not isinstance(value, str):
        return None
    match = _DURATION_RE.match(value.strip())
    if match is None:
        log.debug("youtube_api: unparsable duration %r", value)
        return None
    parts = {key: float(raw) for key, raw in match.groupdict(default="0").items()}
    total = parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]
    return int(total) or None


def _classify(exc: HttpStatusError) -> Exception:
    """Map an HTTP failure onto the chain's fall-through/surface decision."""
    reason, message = _error_detail(exc.body)
    log.debug("youtube_api: HTTP %s reason=%s message=%s", exc.status, reason, message)

    if reason in _QUOTA_REASONS:
        return QuotaExhausted(f"quota exhausted ({reason})")
    if reason in _TRANSIENT_REASONS:
        return TransientError(f"rate limited ({reason})")
    if exc.status == 429:
        return TransientError("rate limited")
    if exc.status >= 500:
        return TransientError(f"server error ({exc.status})")
    if exc.status in (400, 401, 403, 404):
        # Bad query, rejected key, API not enabled. All are the user's to fix,
        # and all would fail identically on a retry — so they surface.
        return PermanentError(f"api rejected the request ({reason or exc.status})")
    return TransientError(f"unexpected status {exc.status}")


def _error_detail(body: bytes) -> tuple[str, str]:
    try:
        error = (json.loads(body.decode("utf-8")) or {}).get("error") or {}
    except (ValueError, UnicodeDecodeError, AttributeError):
        return "", ""
    errors = error.get("errors") or []
    reason = errors[0].get("reason", "") if errors and isinstance(errors[0], dict) else ""
    return str(reason), str(error.get("message", ""))


def _urlopen(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            return bytes(response.read())
    except urllib.error.HTTPError as exc:
        raise HttpStatusError(exc.code, exc.read()) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise TransientError(f"network error: {exc}") from exc
