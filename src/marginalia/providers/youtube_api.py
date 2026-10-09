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

from ..contracts import FILTER_PUBLISHED_AFTER
from .base import (
    PermanentError,
    ProviderUnavailable,
    QuotaExhausted,
    QuotaLedger,
    SearchFilters,
    SearchResult,
    TransientError,
)
from .chapters import parse_description_chapters

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

# Google's daily-cap message, e.g. "Quota exceeded for quota metric 'Search
# Queries' and limit 'Search Queries per day'". Matched on the message because
# the `reason` field does not distinguish it from ordinary throttling.
_MENTIONS_DAILY_QUOTA = re.compile(r"quota exceeded|per day|daily limit", re.IGNORECASE)


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
        self._units_spent = 0
        self._applied = SearchFilters()

    # -- public API --------------------------------------------------------

    def search(
        self, query: str, limit: int = 25, filters: SearchFilters | None = None
    ) -> list[SearchResult]:
        if not query.strip():
            # A bad query is the caller's mistake. Surfacing beats falling
            # through to a second provider that will fail the same way.
            raise PermanentError("empty query")

        # Reset per call: `units_spent` answers "what did *this* query cost",
        # which is the number a queries row records. Running totals are the
        # quota table's job and are already kept there.
        self._units_spent = 0
        self._applied = filters or SearchFilters()
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
            **_filter_params(filters),
        )

        results = [item for item in _items(payload) if _video_id(item)]
        if not results:
            return []

        details = self._details([_video_id(item) for item in results])

        return [self._to_result(item, details) for item in results]

    def quota_remaining(self) -> int | None:
        if self._quota is None:
            return None
        if self._quota.quota_exhausted(self.name):
            # Zero, whatever the local estimate says. `quota_headroom` reads
            # this to decide whether the next round is affordable, and a round
            # planned against an allowance the API has withdrawn is a round of
            # searches that can only fail.
            return 0
        return max(0, self._daily_units - self._quota.quota_used(self.name))

    def units_per_search(self) -> int:
        """One ``search.list`` plus the batched ``videos.list`` that follows it."""
        return SEARCH_COST + VIDEOS_COST

    def applied_filters(self) -> SearchFilters:
        """Everything it was given. This provider applies all four."""
        return self._applied

    def units_spent(self) -> int:
        """What the last search cost. 100 for the search, plus 1 if details ran.

        Counted from what was actually reserved rather than from the published
        costs, so a search whose ``videos.list`` was refused for want of quota
        reports 100 and not 101. The number is written into the queries row, and
        a cost that says what it would usually have been is not a record.
        """
        return self._units_spent

    # -- internals ---------------------------------------------------------

    def _details(self, video_ids: list[str]) -> dict[str, dict[str, Any]]:
        """One batched ``videos.list`` for every ID. Never one call per video."""
        try:
            payload = self._call(
                "videos",
                VIDEOS_COST,
                # `contentDetails` already carries `caption`, so knowing whether
                # a video has a caption track costs nothing beyond the call
                # that was being made anyway for duration.
                part="contentDetails,statistics",
                id=",".join(video_ids),
            )
        except QuotaExhausted:
            # The search itself is already paid for and in hand. Losing
            # duration and view count is a smaller loss than discarding
            # results we spent 100 units on.
            log.warning(
                "youtube_api: quota ran out before videos.list; "
                "results lack duration and caption status"
            )
            return {}
        return {item.get("id"): item for item in _items(payload) if item.get("id")}

    def _to_result(self, item: dict[str, Any], details: dict[str, dict[str, Any]]) -> SearchResult:
        video_id = _video_id(item)
        assert video_id is not None  # filtered by the caller
        snippet = item.get("snippet") or {}
        detail = details.get(video_id) or {}

        # Chapters come off the description *before* `_text` flattens it: the
        # listing is lines, and collapsing whitespace destroys it irrecoverably.
        # The stored description stays flattened, which is what the judge reads
        # as prose and what keeps both providers agreeing on the field.
        raw_description = snippet.get("description")
        description = _text(raw_description)

        return SearchResult(
            video_id=video_id,
            title=_text(snippet.get("title")),
            channel=_text(snippet.get("channelTitle")),
            description=description,
            duration_s=_duration_seconds((detail.get("contentDetails") or {}).get("duration")),
            published_at=snippet.get("publishedAt") or None,
            view_count=_int_or_none((detail.get("statistics") or {}).get("viewCount")),
            provider=self.name,
            chapters=parse_description_chapters(_lines(raw_description)),
            has_captions=_caption_flag((detail.get("contentDetails") or {}).get("caption")),
        )

    def _call(self, endpoint: str, cost: int, **params: str) -> dict[str, Any]:
        self._reserve(cost)
        url = f"{API_ROOT}/{endpoint}?" + urllib.parse.urlencode(
            {**params, "key": self._api_key}
        )
        try:
            raw = self._get(url)
        except HttpStatusError as exc:
            failure = _classify(exc)
            if isinstance(failure, QuotaExhausted) and self._quota is not None:
                # Written down, not just raised. The next call in this run — and
                # the next run today — must not rediscover it at 100 units a go.
                self._quota.mark_quota_exhausted(self.name)
            raise failure from exc
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
            # Still counted. An unmetered *caller* — no ledger passed — does not
            # make the call free, and the queries row should say what it cost.
            self._units_spent += cost
            return
        if self._quota.quota_exhausted(self.name):
            # The API already refused today. Charging and sending again would
            # buy another 429 at 100 units a time.
            raise QuotaExhausted(
                f"{self.name} reported its daily quota exhausted; not sending again today"
            )
        used = self._quota.quota_used(self.name)
        if used + cost > self._daily_units:
            raise QuotaExhausted(
                f"quota exhausted: {used}/{self._daily_units} units used today, "
                f"this call costs {cost}"
            )
        self._quota.spend_quota(self.name, cost)
        self._units_spent += cost


# -- parsing ---------------------------------------------------------------


def _filter_params(filters: SearchFilters | None) -> dict[str, str]:
    """Validated filters, in the shape ``search.list`` wants them.

    Nothing is *decided* here — `brain/territory.py` already refused every
    filter the rules would not allow, and a second opinion in the provider
    would be a rule in a place nobody would think to look for one. What happens
    here is translation, and exactly one field needs it.

    ``publishedAfter`` must be RFC 3339. The map writes a plain ``YYYY-MM-DD``,
    because that is what a model reliably produces and what a person reading
    the ledger can check at a glance; sending it unconverted earns a 400 that
    reads as a rejected key. Midnight UTC, which is the wider end of the day.
    """
    if filters is None:
        return {}
    params = filters.as_dict()
    published = params.get(FILTER_PUBLISHED_AFTER)
    if published and len(published) == 10:
        params[FILTER_PUBLISHED_AFTER] = f"{published}T00:00:00Z"
    return params


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


def _lines(value: Any) -> str:
    """Unescaped, with line structure intact.

    `_text` is right for everything a reader sees as prose and wrong for
    anything read as *lines*. The API escapes descriptions like everything
    else, so the unescaping still has to happen — just not the flattening.
    """
    return html.unescape(str(value)) if value else ""


def _caption_flag(raw: Any) -> bool | None:
    """``contentDetails.caption`` — the string ``"true"`` or ``"false"``.

    A string, not a boolean, which is why this exists at all: ``bool("false")``
    is ``True``, and that mistake would route every captioned video to ASR while
    looking like it worked.

    Anything else — absent, null, a value the API has not shipped yet — is
    ``None``, meaning *nobody asked*. Only an explicit false may skip a fetch.
    """
    if isinstance(raw, bool):  # a future API that fixes the type
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return None


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
        # `rateLimitExceeded` is Google's reason string for *both* "slow down"
        # and "you have spent the day's Search Queries". The two need opposite
        # responses — back off, or stop entirely — and only the message tells
        # them apart. Run `7da933b180fd` took the daily kind for the transient
        # kind and paid 100 units for each of 25 refusals.
        if _MENTIONS_DAILY_QUOTA.search(message or ""):
            return QuotaExhausted(f"daily quota exhausted ({reason}: {message[:90]})")
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
    first = errors[0] if errors and isinstance(errors[0], dict) else {}
    reason = first.get("reason", "")
    # Google populates both the top-level message and the per-error one, and
    # which carries the detail has varied. The daily-quota distinction is made
    # from this string, so read whichever is present rather than assuming.
    message = error.get("message") or first.get("message", "")
    return str(reason), str(message)


def _urlopen(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            return bytes(response.read())
    except urllib.error.HTTPError as exc:
        raise HttpStatusError(exc.code, exc.read()) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise TransientError(f"network error: {exc}") from exc
