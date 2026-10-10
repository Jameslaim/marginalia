"""Search providers, against recorded fixtures. No network, no yt-dlp.

Live-network checks belong to acceptance, not to the suite: a test that needs
a key and an internet connection stops being run, and a test that stops being
run stops protecting anything.

What matters here is the shape of the seam. Everything above `providers/` sees
`SearchResult` and nothing else, so both providers must produce the same shape
from very different payloads — and the quota arithmetic must refuse a request
*before* it is sent, rather than discovering the ceiling as an opaque 403.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from marginalia.contracts import SearchFilters
from marginalia.providers import get_search_provider
from marginalia.providers.base import (
    PermanentError,
    ProviderUnavailable,
    QuotaExhausted,
    SearchResult,
    TransientError,
)
from marginalia.providers.ytdlp_search import YtDlpSearchProvider
from marginalia.providers.youtube_api import (
    SEARCH_COST,
    VIDEOS_COST,
    HttpStatusError,
    YouTubeApiProvider,
    _classify,
    _duration_seconds,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeQuota:
    """The few lines of the ledger a metered provider is allowed to see."""

    def __init__(self, used: int = 0, exhausted: bool = False) -> None:
        self.used = used
        self.exhausted = exhausted

    def quota_used(self, provider: str) -> int:
        return self.used

    def spend_quota(self, provider: str, units: int) -> int:
        self.used += units
        return self.used

    def quota_exhausted(self, provider: str) -> bool:
        return self.exhausted

    def mark_quota_exhausted(self, provider: str) -> None:
        self.exhausted = True


class FakeTransport:
    """Replays recorded responses and records the URLs asked for."""

    def __init__(self, *responses: bytes | Exception) -> None:
        self._responses = list(responses)
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        response = self._responses.pop(0) if self._responses else b"{}"
        if isinstance(response, Exception):
            raise response
        return response


def api(*responses: bytes | Exception, quota=None, daily_units: int = 10_000):
    transport = FakeTransport(*responses)
    provider = YouTubeApiProvider(
        "test-key", quota=quota, daily_units=daily_units, transport=transport
    )
    return provider, transport


def error(status: int, reason: str) -> HttpStatusError:
    body = json.dumps(
        {"error": {"code": status, "message": "…", "errors": [{"reason": reason}]}}
    ).encode()
    return HttpStatusError(status, body)


# -- youtube_api: the two-call shape ---------------------------------------


def test_search_returns_results_from_the_recorded_payloads() -> None:
    provider, _ = api(fixture("youtube_search.json"), fixture("youtube_videos.json"))

    results = provider.search("prompt caching")

    assert [r.video_id for r in results] == ["dQw4w9WgXcQ", "jNQXAC9IVRw"]
    first = results[0]
    assert first.title == "Prompt caching explained"
    assert first.channel == "Some Channel"
    assert first.description.startswith("What actually gets cached")
    assert first.published_at == "2026-03-14T09:00:07Z"


def test_details_come_from_the_second_call() -> None:
    """`search.list` carries neither duration nor view count. That is the
    entire reason `videos.list` exists here."""
    provider, _ = api(fixture("youtube_search.json"), fixture("youtube_videos.json"))

    results = provider.search("prompt caching")

    assert results[0].duration_s == 18 * 60 + 32
    assert results[0].view_count == 148_213
    assert results[1].duration_s == 3600 + 42 * 60 + 9
    assert results[1].view_count == 3_907


def test_details_are_fetched_in_one_batched_call() -> None:
    """Not one call per video: fifty ids cost 1 unit together and 50 apart."""
    provider, transport = api(fixture("youtube_search.json"), fixture("youtube_videos.json"))

    provider.search("prompt caching")

    assert len(transport.urls) == 2
    assert "/videos?" in transport.urls[1]
    assert "dQw4w9WgXcQ%2CjNQXAC9IVRw" in transport.urls[1]


def test_a_details_call_that_fails_keeps_the_search_it_followed() -> None:
    """`search.list` is paid for and in hand. A transient failure of the
    `videos.list` after it used to discard the results, leave the units
    unrecorded on the query, and send the chain to the overflow provider, which
    applies no filters."""
    provider, _ = api(fixture("youtube_search.json"), error(500, "backendError"))

    results = provider.search("prompt caching", filters=SearchFilters(order="date"))

    assert [r.video_id for r in results] == ["dQw4w9WgXcQ", "jNQXAC9IVRw"]
    assert results[0].duration_s is None, "details are what was lost, and only that"
    assert provider.units_spent() >= 100
    assert provider.applied_filters().as_dict() == {"order": "date"}


def test_non_video_items_are_dropped() -> None:
    """`type=video` is requested, but channel results have been seen to leak
    through — and a channel id is not something we can ingest."""
    provider, _ = api(fixture("youtube_search.json"), fixture("youtube_videos.json"))

    assert all(len(r.video_id) == 11 for r in provider.search("prompt caching"))


def test_every_result_names_its_provider() -> None:
    provider, _ = api(fixture("youtube_search.json"), fixture("youtube_videos.json"))
    assert {r.provider for r in provider.search("x")} == {"youtube_api"}


def test_html_entities_are_unescaped() -> None:
    """The API HTML-escapes every snippet string; yt-dlp does not. Leaving it
    would mean the same video carries a different title depending on which
    provider answered — which defeats the point of the SearchProvider seam,
    and puts raw `&#39;` in front of M2b's judge."""
    provider, _ = api(fixture("youtube_search.json"), fixture("youtube_videos.json"))

    second = provider.search("agent cost control")[1]

    assert second.title == "Agent cost control: faster & cheaper, here's how"
    assert second.description == "Budgets, caching & when not to bother."
    assert "&amp;" not in second.title
    assert "&#39;" not in second.title


def test_unescaping_happens_exactly_once() -> None:
    """A title with a literal `&amp;` in it arrives double-escaped. A second
    pass would silently corrupt it into `&`."""
    payload = json.dumps(
        {
            "items": [
                {
                    "id": {"videoId": "dQw4w9WgXcQ"},
                    "snippet": {"title": "Escaping &amp;amp; you", "channelTitle": "c"},
                }
            ]
        }
    ).encode()
    provider, _ = api(payload, b'{"items": []}')

    assert provider.search("x")[0].title == "Escaping &amp; you"


def test_both_providers_agree_on_the_shape_of_a_title(ytdlp_search) -> None:
    """The seam's whole purpose: nothing above providers/ can tell which one
    ran. Whitespace collapsed, entities resolved, both of them."""
    api_provider, _ = api(fixture("youtube_search.json"), fixture("youtube_videos.json"))

    from_api = api_provider.search("prompt caching")[0].title
    from_ytdlp = ytdlp_search.search("prompt caching")[0].title

    assert from_api == from_ytdlp == "Prompt caching explained"


def test_an_empty_result_set_skips_the_second_call() -> None:
    provider, transport = api(b'{"items": []}')

    assert provider.search("nothing at all") == []
    assert len(transport.urls) == 1


def test_the_key_is_sent_and_the_query_is_encoded() -> None:
    provider, transport = api(fixture("youtube_search.json"), fixture("youtube_videos.json"))

    provider.search("prompt caching & cost")

    assert "key=test-key" in transport.urls[0]
    assert "q=prompt+caching+%26+cost" in transport.urls[0]
    assert "type=video" in transport.urls[0]


def test_the_limit_is_clamped_to_the_api_page_size(caplog: pytest.LogCaptureFixture) -> None:
    """Asking for 200 and silently getting 50 would report coverage we never
    had, so the clamp is logged rather than hidden."""
    import logging

    provider, transport = api(fixture("youtube_search.json"), fixture("youtube_videos.json"))
    with caplog.at_level(logging.WARNING, logger="marginalia.providers.youtube_api"):
        provider.search("prompt caching", limit=200)

    assert "maxResults=50" in transport.urls[0]
    assert "returning 50" in caplog.text


# -- youtube_api: quota ----------------------------------------------------


def test_quota_is_charged_before_the_request_is_sent() -> None:
    """Over-counting on a crash is recoverable; believing you still have quota
    you already spent earns an opaque 403."""
    quota = FakeQuota()
    provider, _ = api(error(500, "backendError"), quota=quota)

    with pytest.raises(TransientError):
        provider.search("prompt caching")

    assert quota.used == SEARCH_COST


def test_both_calls_are_charged() -> None:
    quota = FakeQuota()
    provider, _ = api(fixture("youtube_search.json"), fixture("youtube_videos.json"), quota=quota)

    provider.search("prompt caching")

    assert quota.used == SEARCH_COST + VIDEOS_COST


def test_quota_exhaustion_is_raised_before_the_request() -> None:
    quota = FakeQuota(used=9_950)
    provider, transport = api(fixture("youtube_search.json"), quota=quota, daily_units=10_000)

    with pytest.raises(QuotaExhausted) as caught:
        provider.search("prompt caching")

    assert transport.urls == [], "the request must not be sent"
    assert quota.used == 9_950, "and must not be charged"
    assert "9950/10000" in str(caught.value)


def test_a_zero_cap_forces_the_fallback() -> None:
    """How the config option is meant to be used, and how acceptance forces
    the overflow path without waiting for a real quota to run out."""
    provider, transport = api(fixture("youtube_search.json"), quota=FakeQuota(), daily_units=0)

    with pytest.raises(QuotaExhausted):
        provider.search("prompt caching")
    assert transport.urls == []


def test_exactly_enough_quota_is_allowed() -> None:
    quota = FakeQuota(used=10_000 - SEARCH_COST - VIDEOS_COST)
    provider, _ = api(fixture("youtube_search.json"), fixture("youtube_videos.json"), quota=quota)

    assert len(provider.search("prompt caching")) == 2
    assert quota.used == 10_000


def test_quota_running_out_between_the_two_calls_keeps_the_results() -> None:
    """The search is already paid for. Losing duration is a smaller loss than
    discarding results that cost 100 units."""
    quota = FakeQuota(used=10_000 - SEARCH_COST)
    provider, _ = api(fixture("youtube_search.json"), fixture("youtube_videos.json"), quota=quota)

    results = provider.search("prompt caching")

    assert [r.video_id for r in results] == ["dQw4w9WgXcQ", "jNQXAC9IVRw"]
    assert results[0].duration_s is None
    assert results[0].view_count is None
    assert results[0].title == "Prompt caching explained"


def test_quota_remaining_counts_down() -> None:
    quota = FakeQuota(used=400)
    provider, _ = api(quota=quota, daily_units=10_000)
    assert provider.quota_remaining() == 9_600


def test_quota_remaining_never_goes_negative() -> None:
    provider, _ = api(quota=FakeQuota(used=99_999), daily_units=10_000)
    assert provider.quota_remaining() == 0


def test_an_unmetered_provider_reports_no_quota() -> None:
    provider, _ = api()
    assert provider.quota_remaining() is None


# -- youtube_api: what falls through, and what surfaces --------------------


@pytest.mark.parametrize("reason", ["quotaExceeded", "dailyLimitExceeded"])
def test_a_quota_403_falls_through(reason: str) -> None:
    provider, _ = api(error(403, reason))
    with pytest.raises(QuotaExhausted):
        provider.search("prompt caching")


@pytest.mark.parametrize(
    "status,reason",
    [(403, "rateLimitExceeded"), (429, ""), (500, ""), (503, "backendError")],
)
def test_a_transient_failure_falls_through(status: int, reason: str) -> None:
    provider, _ = api(error(status, reason))
    with pytest.raises(TransientError):
        provider.search("prompt caching")


@pytest.mark.parametrize(
    "status,reason",
    [(400, "badRequest"), (403, "keyInvalid"), (403, "accessNotConfigured"), (401, "")],
)
def test_a_rejected_key_or_bad_request_surfaces(status: int, reason: str) -> None:
    """These fail identically on every provider. Falling through would turn a
    fixable mistake into a mystery."""
    provider, _ = api(error(status, reason))
    with pytest.raises(PermanentError):
        provider.search("prompt caching")


def test_an_empty_query_surfaces_without_spending_anything() -> None:
    quota = FakeQuota()
    provider, transport = api(quota=quota)

    with pytest.raises(PermanentError):
        provider.search("   ")

    assert transport.urls == []
    assert quota.used == 0


def test_an_unreadable_response_is_transient() -> None:
    provider, _ = api(b"<html>a proxy login page</html>")
    with pytest.raises(TransientError):
        provider.search("prompt caching")


def test_no_api_key_is_unavailable_not_a_hard_error() -> None:
    """"You have not configured a key" is not "YouTube rejected your key".
    The first falls through to the overflow provider; the second must not."""
    with pytest.raises(ProviderUnavailable):
        YouTubeApiProvider("")
    with pytest.raises(ProviderUnavailable):
        get_search_provider("youtube_api", api_key=None)


# -- ISO 8601 durations ----------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("PT18M32S", 1112),
        ("PT1H42M9S", 6129),
        ("PT45S", 45),
        ("PT2M", 120),
        ("PT3H", 10800),
        ("P1DT2H", 93600),
        ("P0D", None),
        ("PT0S", None),
        ("", None),
        ("nonsense", None),
        (None, None),
        (17, None),
    ],
)
def test_iso8601_durations(raw: object, expected: int | None) -> None:
    assert _duration_seconds(raw) == expected


# -- ytdlp_search ----------------------------------------------------------


class FakeYoutubeDL:
    """Stands in for yt_dlp.YoutubeDL, replaying a recorded flat extraction."""

    last_options: dict = {}
    last_target: str = ""
    payload: object = None
    raises: Exception | None = None

    def __init__(self, options: dict) -> None:
        FakeYoutubeDL.last_options = options

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def extract_info(self, target: str, download: bool = True):
        FakeYoutubeDL.last_target = target
        if FakeYoutubeDL.raises is not None:
            raise FakeYoutubeDL.raises
        return FakeYoutubeDL.payload


@pytest.fixture()
def ytdlp_search(monkeypatch: pytest.MonkeyPatch):
    """Install a fake `yt_dlp` module, so the suite never needs the real one."""
    import sys
    import types

    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = FakeYoutubeDL  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yt_dlp", module)

    FakeYoutubeDL.payload = json.loads(fixture("ytdlp_search_flat.json"))
    FakeYoutubeDL.raises = None

    from marginalia.providers.ytdlp_search import YtDlpSearchProvider

    return YtDlpSearchProvider()


def test_ytdlp_search_maps_flat_entries(ytdlp_search) -> None:
    results = ytdlp_search.search("prompt caching")

    assert [r.video_id for r in results] == ["dQw4w9WgXcQ", "jNQXAC9IVRw"]
    assert isinstance(results[0], SearchResult)
    assert results[0].title == "Prompt caching explained"  # whitespace collapsed
    assert results[0].channel == "Some Channel"
    assert results[0].duration_s == 1112
    assert results[0].view_count == 148_213
    assert results[0].provider == "ytdlp_search"


def test_ytdlp_search_tolerates_the_fields_flat_mode_omits(ytdlp_search) -> None:
    """Flat extraction is thin by design — that is what makes it cheap. M2b's
    judge has to cope with an empty description."""
    second = ytdlp_search.search("prompt caching")[1]

    assert second.description == ""
    assert second.duration_s is None
    assert second.view_count is None
    assert second.channel == "Another Channel"  # fell back to `uploader`


def test_ytdlp_search_normalises_dates_to_the_api_shape(ytdlp_search) -> None:
    """A consumer must never have to ask which provider produced a result."""
    results = ytdlp_search.search("prompt caching")

    assert results[0].published_at == "2026-03-14T09:00:07Z"  # from `timestamp`
    assert results[1].published_at == "2026-05-02"  # from `upload_date`


def test_ytdlp_search_drops_entries_with_no_id(ytdlp_search) -> None:
    assert len(ytdlp_search.search("prompt caching")) == 2


def test_ytdlp_search_uses_flat_extraction(ytdlp_search) -> None:
    """Without this it resolves every hit individually — slow enough to be
    useless as an overflow path."""
    ytdlp_search.search("prompt caching", limit=8)

    assert FakeYoutubeDL.last_options["extract_flat"] is True
    assert FakeYoutubeDL.last_target == "ytsearch8:prompt caching"


def test_ytdlp_search_has_no_quota(ytdlp_search) -> None:
    assert ytdlp_search.quota_remaining() is None


def test_ytdlp_search_refuses_an_empty_query(ytdlp_search) -> None:
    with pytest.raises(PermanentError):
        ytdlp_search.search("   ")


def test_ytdlp_search_classifies_failures(ytdlp_search) -> None:
    FakeYoutubeDL.raises = RuntimeError("Unable to download webpage: HTTP Error 503")
    with pytest.raises(TransientError):
        ytdlp_search.search("prompt caching")


def test_ytdlp_search_writes_nothing_to_stdout(ytdlp_search) -> None:
    """A transcript line reaching the conversation is the one unrecoverable
    failure; yt-dlp printing to stdout is how it would happen."""
    ytdlp_search.search("prompt caching")

    assert FakeYoutubeDL.last_options["quiet"] is True
    assert FakeYoutubeDL.last_options["logger"] is not None


# -- B2: the caption hint and chapters, through the providers ---------------


def test_the_caption_flag_is_read_from_the_recorded_payload() -> None:
    """`contentDetails.caption` is a **string**, `"true"` or `"false"`. Reading
    it as a boolean would make `bool("false")` True and route every captioned
    video to ASR while looking like it worked."""
    provider, _ = api(fixture("youtube_search.json"), fixture("youtube_videos.json"))

    results = provider.search("prompt caching")

    assert results[0].has_captions is True
    assert results[1].has_captions is False


def test_the_caption_flag_costs_no_extra_call() -> None:
    """It rides along in the `videos.list` call already being made for duration.
    Two calls before B2, two after."""
    provider, transport = api(fixture("youtube_search.json"), fixture("youtube_videos.json"))

    provider.search("prompt caching")

    assert len(transport.urls) == 2
    assert "contentDetails" in transport.urls[1]


def test_an_unknown_caption_value_is_none_not_false() -> None:
    """"Nobody asked" and "there is no caption track" are different claims, and
    only the second may skip a fetch. A payload that omits the field, or ships a
    value this code has not seen, must land on the first."""
    videos = json.dumps(
        {
            "items": [
                {"id": "dQw4w9WgXcQ", "contentDetails": {"duration": "PT5M"}},
                {"id": "jNQXAC9IVRw", "contentDetails": {"duration": "PT5M", "caption": "maybe"}},
            ]
        }
    ).encode()
    provider, _ = api(fixture("youtube_search.json"), videos)

    results = provider.search("prompt caching")

    assert results[0].has_captions is None
    assert results[1].has_captions is None


def test_quota_exhaustion_leaves_the_caption_flag_unknown() -> None:
    """The search is already paid for. Losing `videos.list` costs duration and
    the caption hint, and the hint has to degrade to `None` — degrading to
    `False` would send a whole page of results to ASR unfetched."""
    quota = FakeQuota(used=10_000 - SEARCH_COST)
    provider, _ = api(fixture("youtube_search.json"), quota=quota, daily_units=10_000)

    results = provider.search("prompt caching")

    assert results
    assert all(r.has_captions is None for r in results)


def test_chapters_are_parsed_out_of_the_api_description() -> None:
    provider, _ = api(
        fixture("youtube_search_chaptered.json"), fixture("youtube_videos_chaptered.json")
    )

    chapters = provider.search("prompt caching")[0].chapters

    assert [c["title"] for c in chapters] == [
        "Intro",
        "What a cache hit costs",
        "Setting up your key",
        "cache_control breakpoints",
    ]
    assert chapters[0]["start_ms"] == 0


def test_a_description_without_chapters_yields_none_through_the_provider() -> None:
    """The recorded payload is an ordinary description. No chapters is the
    common case and must not become an empty-ish something."""
    provider, _ = api(fixture("youtube_search.json"), fixture("youtube_videos.json"))

    assert provider.search("prompt caching")[0].chapters == []


def test_the_ytdlp_search_provider_reports_captions_as_unknown() -> None:
    """Flat extraction never reports caption tracks. `None` is the truth, and it
    leaves the fetch path exactly as it was — this provider must not route
    videos to ASR by failing to know something the other one happens to know."""
    provider = YtDlpSearchProvider()
    result = provider._to_result({"id": "dQw4w9WgXcQ", "title": "A video"})

    assert result is not None
    assert result.has_captions is None


def test_the_ytdlp_search_provider_parses_chapters_when_it_has_them() -> None:
    """Both providers must agree on the shape of what they return. A truncated
    blurb usually loses the listing, but when the description survives, the same
    parser runs and the judge cannot tell which provider answered."""
    provider = YtDlpSearchProvider()
    result = provider._to_result(
        {
            "id": "dQw4w9WgXcQ",
            "title": "A video",
            "description": "0:00 Intro\n2:00 Middle\n5:00 Later\n9:00 End",
        }
    )

    assert result is not None
    assert [c["title"] for c in result.chapters] == ["Intro", "Middle", "Later", "End"]


# -- a 429 naming the daily cap stops the run spending ------------------------
#
# Run `7da933b180fd` paid 100 units for each of 25 refusals because Google's
# `rateLimitExceeded` was read as ordinary throttling. The local ledger said
# 3,308 of 10,000 while every request came back refused.


def _daily_quota_429() -> HttpStatusError:
    detail = (
        "Quota exceeded for quota metric 'Search Queries' and limit "
        "'Search Queries per day' of service 'youtube.googleapis.com'"
    )
    body = json.dumps(
        {
            "error": {
                "errors": [{"reason": "rateLimitExceeded", "message": detail}],
                "message": detail,
            }
        }
    ).encode()
    return HttpStatusError(429, body)


def test_a_daily_quota_429_is_exhaustion_not_throttling() -> None:
    """Google uses one reason string for both. Only the message separates them,
    and they need opposite responses: back off, or stop for the day."""
    assert isinstance(_classify(_daily_quota_429()), QuotaExhausted)


def test_an_ordinary_429_is_still_transient() -> None:
    body = json.dumps(
        {
            "error": {
                "errors": [{"reason": "rateLimitExceeded", "message": "Too many requests"}],
                "message": "Too many requests",
            }
        }
    ).encode()

    assert isinstance(_classify(HttpStatusError(429, body)), TransientError)


def test_the_run_stops_paying_for_refusals_after_a_daily_429(tmp_path) -> None:
    """The gate. One refusal is recorded, and every later search that day is
    refused locally — before a request is sent and before a unit is charged."""
    from marginalia.core.ledger import Ledger

    with Ledger(tmp_path / "ledger.db") as ledger:
        provider, transport = api(
            *[_daily_quota_429() for _ in range(6)], quota=ledger, daily_units=10_000
        )

        with pytest.raises(QuotaExhausted):
            provider.search("langchain", limit=10)
        charged_once = ledger.quota_used(provider.name)
        assert charged_once == SEARCH_COST, "the first attempt is charged, per rule 6"

        for _ in range(5):
            with pytest.raises(QuotaExhausted):
                provider.search("langchain", limit=10)

        assert len(transport.urls) == 1, "only the first refusal reaches the network"
        assert ledger.quota_used(provider.name) == charged_once, (
            "nothing further is charged once the API has said no"
        )
        assert ledger.quota_exhausted(provider.name) is True
        assert provider.quota_remaining() == 0, "the round-level guard sees it too"
