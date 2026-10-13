"""The discovery loop.

Every model call is mocked. What is being tested is the control flow that
spends money and fills a library: which candidates reach stage 2, when the loop
gives up, what a dry run is allowed to touch, and whether every judgement can
be traced back to the brief that produced it.

The expensive mistakes here are all silent. A loop that never stops burns
credits; a loop that stops too early looks like a thin topic; a dry run that
writes decisions poisons the real run that follows it.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import json
import pytest
from conftest import make_ask_config, make_config, make_note_config

from marginalia.contracts import (
    STATUS_HAS_TRANSCRIPT,
    STATUS_NEEDS_ASR,
    STATUS_SEEN,
    STOP_MAX_COST,
    VERDICT_MAYBE,
    VERDICT_NO,
    VERDICT_YES,
    Brief,
    EVIDENCE_CLEAR,
    EVIDENCE_DEGRADED,
    FullVerdict,
    MetadataVerdict,
    ORIGIN_BRIEF,
    ORIGIN_PLANNER,
    ORIGIN_TERRITORY,
    ORIGIN_THREAD,
    RECOMMEND_CONTINUE,
    RECOMMEND_STOP,
    REVISE_ADD,
    REVISE_VOCABULARY,
    CoverageItem,
    LibraryCoverage,
    FilterRejection,
    PlannedQuery,
    Recency,
    RoundPlan,
    RoundReport,
    SearchFilters,
    TerritoryMap,
    TerritoryRevision,
    Usage,
)
from marginalia.flow.discover import (
    STOP_COVERAGE_SATISFIED,
    STOP_DRY_RUN,
    STOP_QUOTA,
    STOP_MAX_REQUESTS,
    STOP_MAX_ROUNDS,
    STOP_MAX_VIDEOS,
    STOP_NO_NEW_VIDEOS,
    discover,
)
from marginalia.core.ledger import (
    STAGE_FULL,
    STAGE_METADATA,
    STAGE_SEARCH,
    Ledger,
    in_library,
    skip_reason,
)
from marginalia.core.library import Library
from marginalia.llm import LlmClient
from marginalia.providers.base import (
    PermanentError,
    SearchResult,
    TranscriptLine,
    TranscriptResult,
    TransientError,
)

TOPIC = "prompt caching"
BRIEF = Brief(topic=TOPIC, text="INTENT\nLearn how prompt caching works.\n" + "x" * 200, version="1/1")


def result(
    video_id: str,
    title: str = "A video",
    *,
    has_captions: bool | None = None,
    chapters: list[dict] | None = None,
) -> SearchResult:
    return SearchResult(
        video_id=video_id,
        title=title,
        channel="Chan",
        description="",
        duration_s=600,
        published_at="2026-01-01T00:00:00Z",
        view_count=10,
        provider="fake",
        chapters=chapters or [],
        # `None` by default, matching a provider that cannot answer. A test
        # that wants the skip has to ask for it.
        has_captions=has_captions,
    )


def transcript(text: str = "hello world") -> TranscriptResult:
    return TranscriptResult(
        lines=[TranscriptLine(0, 1000, text)],
        language="en",
        source="captions",
        title="A video",
        channel="Chan",
        duration_s=600,
        upload_date="20260101",
        channel_id="UCtestchannelid",
    )


def transcript_without_channel(text: str = "hello world") -> TranscriptResult:
    """A provider that did not report a channel. `channel_id` defaults to "",
    and the store path must turn that into `None` so the ledger's COALESCE
    keeps whatever an earlier fetch recorded."""
    return TranscriptResult(
        lines=[TranscriptLine(0, 1000, text)],
        language="en",
        source="captions",
        title="A video",
        channel="Chan",
        duration_s=600,
        upload_date="20260101",
    )


class FakePrompt:
    version = "1"


class FakeJudge:
    """Scripted verdicts. Records what it was asked, so routing is assertable.

    `openings` keeps its name because the tests using it are about *routing to
    stage 2*, which has not changed — only the evidence stage 2 reads. A value
    is `(verdict, reason, concepts)` or `(verdict, reason, concepts, evidence)`.
    """

    def __init__(self, *, queries=None, metadata=None, openings=None) -> None:
        self._queries = list(queries or [["q1"]])
        self._metadata = dict(metadata or {})
        self._openings = dict(openings or {})
        self.opening_calls: list[str] = []
        self.transcripts_seen: list[str] = []
        self.query_calls: list[tuple[int, tuple[str, ...]]] = []
        self.batch_sizes: list[int] = []
        self.unparsed: set[str] = set()
        # What the judge was shown of the territory map, at each stage. Both
        # stages receive it since B4, and the tests assert that rather than
        # trusting it.
        self.territory_seen: list[object] = []
        self.full_territory_seen: list[object] = []
        self.brief_max_tokens_seen: int | None = None

    def prompt(self, name):
        return FakePrompt()

    def write_brief(self, topic: str, *, max_tokens=None) -> Brief:
        self.brief_max_tokens_seen = max_tokens
        return BRIEF

    def expand_queries(self, brief, *, count=9, concepts=()):
        self.query_calls.append((len(self.query_calls) + 1, tuple(concepts)))
        return self._queries.pop(0) if self._queries else []

    def judge_metadata(self, brief, candidates, *, batch_size, territory=None):
        self.batch_sizes.append(batch_size)
        self.territory_seen.append(territory)
        return [
            MetadataVerdict(
                c.video_id, *self._metadata.get(c.video_id, (VERDICT_YES, "kept"))
            )
            for c in candidates
        ]

    def judge_full(
        self, brief, candidate, *, transcript, chapters=(), duration_s=None, territory=None
    ):
        self.opening_calls.append(candidate.video_id)
        self.transcripts_seen.append(transcript)
        self.full_territory_seen.append(territory)
        scripted = self._openings.get(candidate.video_id, (VERDICT_YES, "good", []))
        verdict, reason, concepts = scripted[:3]
        evidence = scripted[3] if len(scripted) > 3 else EVIDENCE_CLEAR
        return FullVerdict(
            candidate.video_id,
            verdict,
            evidence=evidence,
            reason=reason,
            concepts=list(concepts),
            parsed=candidate.video_id not in self.unparsed,
        )


class FakeTranscripts:
    name = "fake_transcripts"

    def __init__(self, *, failures=None, missing=()) -> None:
        self._failures = dict(failures or {})
        self._missing = set(missing)
        self.fetched: list[str] = []
        # Overridable so a test can put content at a known timestamp and assert
        # stage 2 actually saw it.
        self.lines: list[TranscriptLine] | None = None

    def fetch(self, video_id: str):
        self.fetched.append(video_id)
        if video_id in self._failures:
            raise self._failures[video_id]
        if video_id in self._missing:
            return None
        if self.lines is not None:
            return replace(transcript(), lines=list(self.lines))
        return transcript()


@pytest.fixture()
def library(tmp_path: Path) -> Library:
    instance = Library(tmp_path / "library")
    instance.ensure()
    return instance


@pytest.fixture()
def ledger(library: Library) -> Iterator[Ledger]:
    with Ledger(library.ledger_path) as instance:
        yield instance


@pytest.fixture()
def config(tmp_path: Path) -> object:
    # The fixtures' transcripts are a few words long. With the note worker's
    # real floor they would all be (correctly) unnoteable, and every test about
    # coverage would be testing the floor instead; the floor has its own test.
    return make_config(
        tmp_path, search_order=("fake",), note=make_note_config(min_transcript_chars=0)
    )


@pytest.fixture()
def client() -> LlmClient:
    from conftest import make_llm_config

    return LlmClient(make_llm_config(), transport=lambda **_: None)


@pytest.fixture()
def searches(monkeypatch: pytest.MonkeyPatch):
    """Control what each query returns, without touching search.py's gating."""
    table: dict[str, list[SearchResult]] = {}
    calls: list[str] = []

    def fake_provider(name, **_):
        class P:
            name = "fake"

            last_filters = None

            def search(self, query, limit=25, filters=None):
                calls.append(query)
                self.last_filters = filters
                return list(table.get(query, table.get("*", [])))

            def quota_remaining(self):
                return None

            def units_spent(self):
                return 101

            def units_per_search(self):
                return 101

            def applied_filters(self):
                # This fake stands in for a provider that can filter. The
                # overflow provider cannot, and says so — which is the whole
                # point of the method existing.
                return self.last_filters or SearchFilters()

        return P()

    monkeypatch.setattr("marginalia.flow.search.get_search_provider", fake_provider)
    table["calls"] = calls  # type: ignore[assignment]
    return table


class FakeMapper:
    """A scripted territory map, or none at all.

    ``None`` is the default and it matters: every test written before B4 exists
    ran without a map, and each of them must keep passing unchanged. A map that
    is absent has to leave the loop exactly as it was, or the new step would be
    able to change behaviour on a topic it failed to map.
    """

    def __init__(self, territory: TerritoryMap | None = None) -> None:
        self.territory = territory or TerritoryMap(topic=TOPIC, version="1")
        self.calls = 0
        self.max_tokens_seen: int | None = None

    def map(self, brief, *, count=8, max_tokens=None):
        self.calls += 1
        self.max_tokens_seen = max_tokens
        return self.territory


def mapped(*queries: str, concepts=("caching",), vocabulary=("kv cache",), **kwargs):
    """A map whose queries are exactly these, all of territory origin."""
    return TerritoryMap(
        topic=TOPIC,
        version="1",
        concepts=list(concepts),
        vocabulary=list(vocabulary),
        queries=[
            PlannedQuery(text=text, origin=ORIGIN_TERRITORY, origin_detail="vocabulary: kv cache")
            for text in queries
        ],
        **kwargs,
    )


def run(
    config, ledger, library, client, judge,
    provider=None, topic=TOPIC, mapper=None, planner=None, **kwargs,
):
    return discover(
        config, ledger, library, client, topic,
        judge=judge, mapper=mapper or FakeMapper(), planner=planner or FakePlanner(),
        provider=provider or FakeTranscripts(), **kwargs,
    )


# -- routing: which verdicts reach stage 2 ---------------------------------


def test_a_maybe_routes_to_stage_two(config, ledger, library, client, searches) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")})

    run(config, ledger, library, client, judge, max_rounds=1)

    assert judge.opening_calls == ["aaaaaaaaaaa"]


def test_a_yes_is_read_before_it_is_kept(config, ledger, library, client, searches) -> None:
    """Nothing enters the library unread.

    This asserts the opposite of what it used to. A stage-1 YES skipped stage 2
    on the grounds that it was the cheaper keeper — 0.09¢ and 2.5s cheaper —
    and what that bought was a video stored on the strength of its title. B3
    measured this judge inventing transcript content from metadata, so the
    saving was being spent on the one check that catches it.
    """
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_YES, "clear")})

    run(config, ledger, library, client, judge, max_rounds=1)

    assert judge.opening_calls == ["aaaaaaaaaaa"]
    assert [row.stage for row in ledger.decisions_for("aaaaaaaaaaa")] == [
        STAGE_SEARCH,
        STAGE_METADATA,
        STAGE_FULL,
    ]


def test_a_stage_one_yes_the_transcript_contradicts_is_not_stored(
    config, ledger, library, client, searches
) -> None:
    """The failure this change exists for, end to end.

    A confident metadata verdict, a transcript that does not support it, and no
    note — so `/ask` never answers from it and the planner never counts it as
    coverage. The transcript is kept, because B2's argument is unchanged: the
    bytes are in hand and acquisition is the fragile step.
    """
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(
        metadata={"aaaaaaaaaaa": (VERDICT_YES, "shows connection pooling with real setup")},
        openings={"aaaaaaaaaaa": (VERDICT_NO, "the transcript never sets anything up", [])},
    )

    outcome = run(config, ledger, library, client, judge, max_rounds=1)

    assert outcome.stored_ids == []
    assert outcome.rounds[0].overturned == 1
    # Not folded into the routine flip. A MAYBE stage 2 declines is stage 2
    # working; a YES it declines is stage 1 having been wrong, and one integer
    # holding both would hide the second.
    assert outcome.rounds[0].flipped_to_no == 0
    assert library.has_transcript("aaaaaaaaaaa")
    assert ledger.get("aaaaaaaaaaa").noted_at is None


def test_a_maybe_stage_two_declines_is_still_a_routine_flip(
    config, ledger, library, client, searches
) -> None:
    """The other side of the same split, so neither counter absorbs the other."""
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(
        metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "title is ambiguous")},
        openings={"aaaaaaaaaaa": (VERDICT_NO, "off topic once read", [])},
    )

    outcome = run(config, ledger, library, client, judge, max_rounds=1)

    assert outcome.rounds[0].flipped_to_no == 1
    assert outcome.rounds[0].overturned == 0


def test_a_no_is_never_fetched(config, ledger, library, client, searches) -> None:
    """Fetching is the expensive, blockable step. A NO must not reach it."""
    searches["*"] = [result("aaaaaaaaaaa")]
    provider = FakeTranscripts()
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_NO, "off topic")})

    run(config, ledger, library, client, judge, provider=provider, max_rounds=1)

    assert provider.fetched == []
    assert judge.opening_calls == []


def test_a_maybe_that_flips_to_no_keeps_its_transcript(
    config, ledger, library, client, searches
) -> None:
    """B2. The bytes stay; the note is what decides membership.

    Acquisition is the fragile step, and fragile here is measured rather than
    theoretical — thirteen of thirteen fetches died in nine seconds when YouTube
    throttled a run. Discarding a transcript already in hand means paying that
    risk a second time for content we have.
    """
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(
        metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")},
        openings={"aaaaaaaaaaa": (VERDICT_NO, "actually a product demo", [])},
    )

    outcome = run(config, ledger, library, client, judge, max_rounds=1)

    # Not a keeper: this run stored nothing it would answer a question from.
    assert outcome.stored_ids == []
    assert outcome.rounds[0].flipped_to_no == 1

    # But the transcript is on disk and the row says so.
    assert library.has_transcript("aaaaaaaaaaa")
    row = ledger.get("aaaaaaaaaaa")
    assert row is not None and row.status == STATUS_HAS_TRANSCRIPT


def test_a_flipped_video_gets_no_note_and_so_is_not_in_the_library(
    config, ledger, library, client, searches
) -> None:
    """The whole mechanism, in one assertion pair. `/ask` loads notes, so no
    note means the video cannot reach an answer — which is what a rejection was
    always for. Nothing else has to enforce it."""
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(
        metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")},
        openings={"aaaaaaaaaaa": (VERDICT_NO, "off topic", [])},
    )

    run(config, ledger, library, client, judge, max_rounds=1)

    assert not library.has_note("aaaaaaaaaaa")
    assert in_library(ledger.get("aaaaaaaaaaa")) is False


def test_the_rejection_is_still_logged_against_its_topic(
    config, ledger, library, client, searches
) -> None:
    """Keeping the transcript must not blur what was decided. The NO is a fact
    about one brief and stays in `decisions` where a later topic can disagree
    with it."""
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(
        metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")},
        openings={"aaaaaaaaaaa": (VERDICT_NO, "actually a product demo", [])},
    )

    run(config, ledger, library, client, judge, max_rounds=1)

    decision = ledger.latest_decision("aaaaaaaaaaa", "prompt caching", verdict=VERDICT_NO)
    assert decision is not None
    assert decision.stage == STAGE_FULL
    assert decision.reason == "actually a product demo"


def test_a_rejected_video_costs_no_network_on_a_later_topic(
    config, ledger, library, client, searches
) -> None:
    """The payoff, and the reason to keep the bytes at all.

    Rejected for prompt caching, wanted later for Redis: Gate 1 lets it through
    because it has no note, and the fetch-step check finds the file. The
    provider is never asked a second time.
    """
    searches["*"] = [result("aaaaaaaaaaa")]
    provider = FakeTranscripts()
    rejecting = FakeJudge(
        metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")},
        openings={"aaaaaaaaaaa": (VERDICT_NO, "off topic for this brief", [])},
    )
    run(config, ledger, library, client, rejecting, max_rounds=1, provider=provider)
    assert provider.fetched == ["aaaaaaaaaaa"]

    second = run(
        config,
        ledger,
        library,
        client,
        FakeJudge(),
        topic="redis",
        max_rounds=1,
        provider=provider,
    )

    assert provider.fetched == ["aaaaaaaaaaa"], "the transcript was fetched twice"
    assert second.stored_ids == []


def test_a_flipped_video_is_never_refetched_even_by_the_same_topic(
    config, ledger, library, client, searches
) -> None:
    """`write_transcript` refuses to overwrite, so a second attempt would raise
    rather than corrupt. It must not get that far."""
    searches["*"] = [result("aaaaaaaaaaa")]
    provider = FakeTranscripts()
    judge = FakeJudge(
        metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")},
        openings={"aaaaaaaaaaa": (VERDICT_NO, "off topic", [])},
    )

    run(config, ledger, library, client, judge, max_rounds=1, provider=provider)
    run(config, ledger, library, client, judge, max_rounds=1, provider=provider)

    assert provider.fetched == ["aaaaaaaaaaa"]


def test_a_flipped_video_keeps_its_metadata(
    config, ledger, library, client, searches
) -> None:
    """`meta.json` too, exactly as a keeper gets. A transcript with no metadata
    beside it is a file nobody can identify later."""
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(
        metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")},
        openings={"aaaaaaaaaaa": (VERDICT_NO, "off topic", [])},
    )

    run(config, ledger, library, client, judge, max_rounds=1)

    meta = library.read_meta("aaaaaaaaaaa")
    assert meta is not None
    assert meta["video_id"] == "aaaaaaaaaaa"
    assert meta["line_count"] == 1


# -- B2: the caption hint ---------------------------------------------------


def trusting(config):
    """The same config with the caption hint believed.

    Every test of the skip has to ask for it, because the shipped default is not
    to trust it — see `DEFAULT_TRUST_CAPTION_HINT`. Measured against this
    library, the API called 31 of 37 working videos captionless.
    """
    return replace(config, discover=replace(config.discover, trust_caption_hint=True))


def test_no_caption_track_means_no_fetch_attempt(
    config, ledger, library, client, searches
) -> None:
    """With the hint trusted: the provider said so in the `videos.list` call
    already made, and trying anyway costs a request and lands on `failed` on the
    way to the right answer."""
    searches["*"] = [result("aaaaaaaaaaa", has_captions=False)]
    provider = FakeTranscripts()

    run(trusting(config), ledger, library, client, FakeJudge(), max_rounds=1, provider=provider)

    assert provider.fetched == []
    row = ledger.get("aaaaaaaaaaa")
    assert row is not None and row.status == STATUS_NEEDS_ASR


def test_the_hint_is_ignored_unless_the_config_asks_for_it(
    config, ledger, library, client, searches
) -> None:
    """The shipped default. `contentDetails.caption` counts only manually
    uploaded tracks, so a `false` says nothing about the auto-captions yt-dlp
    actually reads — 31 of the 37 working videos in this library are reported
    captionless. Believing it by default would route most of a run into a queue
    whose worker does not exist until M4."""
    searches["*"] = [result("aaaaaaaaaaa", has_captions=False)]
    provider = FakeTranscripts()

    run(config, ledger, library, client, FakeJudge(), max_rounds=1, provider=provider)

    assert provider.fetched == ["aaaaaaaaaaa"]
    assert library.has_transcript("aaaaaaaaaaa")


def test_the_caption_hint_never_rejects_a_video(
    config, ledger, library, client, searches
) -> None:
    """A caption fact, not a content fact. The video is still relevant; it is
    just unreadable today. Its YES stands, and `needs_asr` is unfinished work
    rather than a verdict."""
    searches["*"] = [result("aaaaaaaaaaa", has_captions=False)]

    run(trusting(config), ledger, library, client, FakeJudge(), max_rounds=1)

    decision = ledger.latest_decision("aaaaaaaaaaa", "prompt caching")
    assert decision is not None and decision.verdict != VERDICT_NO


@pytest.mark.parametrize("hint", [True, None])
def test_anything_but_an_explicit_false_still_fetches(
    config, ledger, library, client, searches, hint
) -> None:
    """Tri-state, and the third state is why. yt-dlp search cannot report
    captions at all — if `None` behaved like `False`, that provider would route
    every video it found straight to ASR."""
    searches["*"] = [result("aaaaaaaaaaa", has_captions=hint)]
    provider = FakeTranscripts()

    run(trusting(config), ledger, library, client, FakeJudge(), max_rounds=1, provider=provider)

    assert provider.fetched == ["aaaaaaaaaaa"]


def test_a_caption_hint_that_is_wrong_costs_only_an_asr_pass(
    config, ledger, library, client, searches
) -> None:
    """When the hint *is* trusted, being wrong is bounded: the video lands in
    `needs_asr`, which is a queue rather than a grave. Visible in the ledger and
    re-attemptable by name — which is the only reason trusting it could ever be
    a reasonable setting."""
    searches["*"] = [result("aaaaaaaaaaa", has_captions=False)]

    run(trusting(config), ledger, library, client, FakeJudge(), max_rounds=1)

    row = ledger.get("aaaaaaaaaaa")
    assert skip_reason(row, transcript_present=False) is None


def test_a_maybe_that_survives_is_stored(config, ledger, library, client, searches) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")})

    outcome = run(config, ledger, library, client, judge, max_rounds=1)

    assert outcome.stored_ids == ["aaaaaaaaaaa"]
    assert library.has_transcript("aaaaaaaaaaa")
    assert ledger.get("aaaaaaaaaaa").status == STATUS_HAS_TRANSCRIPT


def test_a_stored_video_carries_the_hash_of_its_file(
    config, ledger, library, client, searches
) -> None:
    """Rule 3 made checkable on the discovery path. The hash is of the file
    as it landed, so it must equal `transcript_digest` read back afterwards —
    that is the value `marg doctor` will compare against, and a hash computed
    from the in-memory lines would record what was meant rather than what is.
    Rule 25: one of three store paths, each tested on its own."""
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")})

    run(config, ledger, library, client, judge, max_rounds=1)

    row = ledger.get("aaaaaaaaaaa")
    assert row.content_hash is not None
    assert row.content_hash == library.transcript_digest("aaaaaaaaaaa")
    assert len(row.content_hash) == 64  # sha256 hex, the one convention


def test_the_configured_batch_size_is_used(config, ledger, library, client, searches) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    config = replace(config, llm=replace(config.llm, judge_batch_size=1))
    judge = FakeJudge()

    run(config, ledger, library, client, judge, max_rounds=1)

    assert judge.batch_sizes == [1]


# -- fetch failures ---------------------------------------------------------


def test_a_permanently_unavailable_video_is_recorded_not_stored(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    provider = FakeTranscripts(failures={"aaaaaaaaaaa": PermanentError("private")})

    outcome = run(config, ledger, library, client, FakeJudge(), provider=provider, max_rounds=1)

    assert outcome.stored_ids == []
    assert ledger.get("aaaaaaaaaaa").status == "failed"
    assert outcome.rounds[0].failed == 1


def test_a_video_without_captions_is_queued_not_failed(
    config, ledger, library, client, searches
) -> None:
    """No captions is a route, not a failure. M4 picks these up."""
    searches["*"] = [result("aaaaaaaaaaa")]
    provider = FakeTranscripts(missing={"aaaaaaaaaaa"})

    run(config, ledger, library, client, FakeJudge(), provider=provider, max_rounds=1)

    assert ledger.get("aaaaaaaaaaa").status == "needs_asr"


def test_one_failure_does_not_abandon_the_round(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa"), result("bbbbbbbbbbb")]
    provider = FakeTranscripts(failures={"aaaaaaaaaaa": TransientError("timeout")})

    outcome = run(config, ledger, library, client, FakeJudge(), provider=provider, max_rounds=1)

    assert outcome.stored_ids == ["bbbbbbbbbbb"]


# -- threads: the loop's own findings --------------------------------------


def test_concepts_from_stage_two_feed_the_next_round(
    config, ledger, library, client, searches
) -> None:
    """The loop-back is the intelligence. A video that says "KV cache" without
    explaining it has written round two's query."""
    searches["q1"] = [result("aaaaaaaaaaa")]
    searches["q2"] = [result("bbbbbbbbbbb")]
    judge = FakeJudge(
        queries=[["q1"], ["q2"]],
        metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")},
        openings={"aaaaaaaaaaa": (VERDICT_YES, "good", ["kv cache eviction", "prefix matching"])},
    )

    outcome = run(config, ledger, library, client, judge, max_rounds=2)

    assert judge.query_calls[0][1] == ()
    assert judge.query_calls[1][1] == ("kv cache eviction", "prefix matching")
    assert outcome.rounds[1].from_threads is True


def test_repeated_queries_are_not_searched_twice(
    config, ledger, library, client, searches
) -> None:
    searches["q1"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(queries=[["q1"], ["q1"]])

    outcome = run(config, ledger, library, client, judge, max_rounds=2)

    assert searches["calls"].count("q1") == 1
    assert outcome.stopped_because == STOP_NO_NEW_VIDEOS


# -- stopping conditions ---------------------------------------------------


def test_max_rounds_fires_and_is_recorded(config, ledger, library, client, searches) -> None:
    searches["q1"] = [result("aaaaaaaaaaa")]
    searches["q2"] = [result("bbbbbbbbbbb")]
    searches["q3"] = [result("ccccccccccc")]
    judge = FakeJudge(queries=[["q1"], ["q2"], ["q3"]])

    outcome = run(config, ledger, library, client, judge, max_rounds=3)

    assert outcome.stopped_because == STOP_MAX_ROUNDS
    assert ledger.get_run(outcome.run_id).stopped_because == STOP_MAX_ROUNDS


def test_quiet_rounds_stop_the_loop(config, ledger, library, client, searches) -> None:
    """Two rounds finding nothing new is the signal that a topic is exhausted."""
    searches["q1"] = [result("aaaaaaaaaaa")]
    searches["q2"] = []
    searches["q3"] = []
    judge = FakeJudge(queries=[["q1"], ["q2"], ["q3"], ["q4"]])

    outcome = run(config, ledger, library, client, judge, max_rounds=9)

    assert outcome.stopped_because == STOP_NO_NEW_VIDEOS
    assert len(outcome.rounds) == 3


def test_max_videos_fires(config, ledger, library, client, searches) -> None:
    searches["*"] = [result(f"{c}{c}{c}{c}{c}{c}{c}{c}{c}{c}{c}") for c in "abcde"]

    outcome = run(config, ledger, library, client, FakeJudge(), max_rounds=5, max_videos=2)

    assert outcome.stopped_because == STOP_MAX_VIDEOS
    assert len(outcome.stored_ids) == 2


def test_max_requests_fires_before_spending(config, ledger, library, client, searches) -> None:
    """The credit guard. It has to stop the round starting, not notice
    afterwards that it overspent."""
    searches["*"] = [result("aaaaaaaaaaa")]
    client.usage = Usage(requests=100)

    outcome = run(config, ledger, library, client, FakeJudge(), max_rounds=3, max_requests=100)

    assert outcome.stopped_because == STOP_MAX_REQUESTS
    assert outcome.rounds == []


def test_every_stop_reason_is_written_to_the_run_row(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    outcome = run(config, ledger, library, client, FakeJudge(), max_rounds=1)

    row = ledger.get_run(outcome.run_id)
    assert row is not None
    assert row.stopped_because
    assert row.finished_at is not None


# -- the run row ------------------------------------------------------------


def test_the_run_row_exists_before_judging(config, ledger, library, client, searches) -> None:
    """Same ordering rule as the ledger: a run with no finished_at is a run
    that crashed, and that is diagnosable."""
    searches["*"] = [result("aaaaaaaaaaa")]
    seen: list[bool] = []

    class Watcher(FakeJudge):
        def judge_metadata(self, brief, candidates, *, batch_size, territory=None):
            seen.append(bool(ledger.list_runs()))
            return super().judge_metadata(
                brief, candidates, batch_size=batch_size, territory=territory
            )

    run(config, ledger, library, client, Watcher(), max_rounds=1)
    assert seen == [True]


def test_the_brief_is_stored_on_the_run(config, ledger, library, client, searches) -> None:
    """When a library looks wrong the first question is what it thought the
    topic meant, and a topic string cannot answer that."""
    searches["*"] = [result("aaaaaaaaaaa")]
    outcome = run(config, ledger, library, client, FakeJudge(), max_rounds=1)

    row = ledger.get_run(outcome.run_id)
    assert row.brief == BRIEF.text
    assert row.brief_version == "1/1"
    assert row.model == client.model


def test_a_crashed_run_leaves_a_row_without_finished_at(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]

    class Exploding(FakeJudge):
        def judge_metadata(self, brief, candidates, *, batch_size, territory=None):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        run(config, ledger, library, client, Exploding(), max_rounds=1)

    row = ledger.list_runs()[0]
    assert row.stopped_because == "incomplete"


def test_requests_and_tokens_are_recorded(config, ledger, library, client, searches) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    client.usage = Usage(requests=7, input_tokens=900, output_tokens=120)

    outcome = run(config, ledger, library, client, FakeJudge(), max_rounds=1)

    row = ledger.get_run(outcome.run_id)
    assert (row.requests_made, row.input_tokens, row.output_tokens) == (7, 900, 120)


# -- decision logging -------------------------------------------------------


def test_every_judged_candidate_has_a_decision_row_with_a_run_id(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa"), result("bbbbbbbbbbb"), result("ccccccccccc")]
    judge = FakeJudge(
        metadata={
            "aaaaaaaaaaa": (VERDICT_YES, "clear"),
            "bbbbbbbbbbb": (VERDICT_NO, "off topic"),
            "ccccccccccc": (VERDICT_MAYBE, "unclear"),
        }
    )

    outcome = run(config, ledger, library, client, judge, max_rounds=1)

    rows = ledger.decisions_for_run(outcome.run_id)
    metadata_rows = {r.video_id: r for r in rows if r.stage == STAGE_METADATA}
    assert set(metadata_rows) == {"aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"}
    assert metadata_rows["bbbbbbbbbbb"].verdict == VERDICT_NO
    assert metadata_rows["bbbbbbbbbbb"].reason == "off topic"
    assert all(r.run_id == outcome.run_id for r in rows)
    assert all(r.model == client.model for r in rows if r.stage != "search")


def test_yes_and_maybe_are_logged_not_only_no(
    config, ledger, library, client, searches
) -> None:
    """A judge that wrongly keeps things is as broken as one that wrongly
    rejects, and only the full log shows it."""
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_YES, "clear")})

    outcome = run(config, ledger, library, client, judge, max_rounds=1)

    verdicts = {r.verdict for r in ledger.decisions_for_run(outcome.run_id)}
    assert VERDICT_YES in verdicts


def test_stage_two_writes_its_own_row(config, ledger, library, client, searches) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(
        metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")},
        openings={"aaaaaaaaaaa": (VERDICT_NO, "product demo", [])},
    )

    outcome = run(config, ledger, library, client, judge, max_rounds=1)

    stages = [(r.stage, r.verdict) for r in ledger.decisions_for_run(outcome.run_id)]
    assert (STAGE_METADATA, VERDICT_MAYBE) in stages
    assert (STAGE_FULL, VERDICT_NO) in stages


# -- dry run ----------------------------------------------------------------


def test_a_dry_run_writes_nothing(config, ledger, library, client, searches) -> None:
    """This is how the judge gets tuned without burning credits or filling the
    library with mistakes, so it has to be genuinely inert."""
    searches["*"] = [result("aaaaaaaaaaa"), result("bbbbbbbbbbb")]
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_YES, "clear")})

    outcome = run(config, ledger, library, client, judge, max_rounds=1, dry_run=True)

    assert outcome.stopped_because == STOP_DRY_RUN
    assert outcome.stored_ids == []
    assert ledger.list_by_status() == [], "no videos rows"
    assert ledger.decisions_for_run(outcome.run_id) == [], "no decisions rows"
    assert not any(library.videos_dir.iterdir()), "no library files"


def test_a_dry_run_still_judges_and_reports(config, ledger, library, client, searches) -> None:
    searches["*"] = [result("aaaaaaaaaaa"), result("bbbbbbbbbbb")]
    judge = FakeJudge(
        metadata={
            "aaaaaaaaaaa": (VERDICT_YES, "clear"),
            "bbbbbbbbbbb": (VERDICT_NO, "off topic"),
        }
    )

    outcome = run(config, ledger, library, client, judge, max_rounds=1, dry_run=True)

    assert outcome.rounds[0].verdict_yes == 1
    assert outcome.rounds[0].verdict_no == 1
    assert outcome.judged == 2


def test_a_dry_run_never_fetches(config, ledger, library, client, searches) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    provider = FakeTranscripts()

    run(config, ledger, library, client, FakeJudge(), provider=provider, max_rounds=1, dry_run=True)

    assert provider.fetched == []


def test_a_dry_run_does_not_poison_gate_2(config, ledger, library, client, searches) -> None:
    """A dry run that logged `no` rows would make the real run that follows it
    skip everything the tuning pass rejected — silently shrinking the library
    the tuning was meant to improve."""
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_NO, "off topic")})

    run(config, ledger, library, client, judge, max_rounds=1, dry_run=True)
    real = run(config, ledger, library, client, FakeJudge(), max_rounds=1)

    assert real.stored_ids == ["aaaaaaaaaaa"]


# -- the gates still hold across runs --------------------------------------


def test_a_second_run_stores_nothing_new(config, ledger, library, client, searches) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]

    first = run(config, ledger, library, client, FakeJudge(), max_rounds=1)
    second = run(config, ledger, library, client, FakeJudge(), max_rounds=1)

    assert first.stored_ids == ["aaaaaaaaaaa"]
    assert second.stored_ids == []
    # `already_held`, not `known`: the video has no note, so Gate 1 let it
    # through as a candidate. What stopped it was the transcript being on disk,
    # which is a different fact with a different counter since B3.
    assert second.rounds[0].already_held == 1
    assert second.rounds[0].known == 0


def test_a_rejected_video_is_not_rejudged_on_the_same_topic(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_NO, "off topic")})

    run(config, ledger, library, client, judge, max_rounds=1)
    second_judge = FakeJudge()
    outcome = run(config, ledger, library, client, second_judge, max_rounds=1)

    assert outcome.rounds[0].rejected_before == 1
    assert second_judge.batch_sizes == [], "gate 2 dropped it before any model call"


# -- the cost guard ---------------------------------------------------------
#
# Under per-token billing a request count stops being a guard: one batch of
# fifty candidates and one of five cost the same number of requests and wildly
# different money.


def test_max_cost_stops_the_loop(config, ledger, library, client, searches) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    config = replace(config, discover=replace(config.discover, max_cost_usd=0.01))
    # $3/M in and $15/M out: 1M output tokens is far past a one-cent ceiling.
    client.usage = Usage(requests=3, input_tokens=0, output_tokens=1_000_000)

    outcome = run(config, ledger, library, client, FakeJudge(), max_rounds=2)

    assert outcome.stopped_because == STOP_MAX_COST
    assert ledger.get_run(outcome.run_id).stopped_because == STOP_MAX_COST


def test_a_cheap_run_is_not_stopped(config, ledger, library, client, searches) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    client.usage = Usage(requests=3, input_tokens=1000, output_tokens=200)

    outcome = run(config, ledger, library, client, FakeJudge(), max_rounds=1)

    assert outcome.stopped_because != STOP_MAX_COST


def test_a_zero_ceiling_means_no_cost_limit(config, ledger, library, client, searches) -> None:
    """Opting out has to be possible without editing code."""
    searches["*"] = [result("aaaaaaaaaaa")]
    config = replace(config, discover=replace(config.discover, max_cost_usd=0.0))
    client.usage = Usage(requests=1, input_tokens=0, output_tokens=100_000_000)

    outcome = run(config, ledger, library, client, FakeJudge(), max_rounds=1)

    assert outcome.stopped_because != STOP_MAX_COST


def test_the_run_records_what_it_cost(config, ledger, library, client, searches) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    client.usage = Usage(requests=4, input_tokens=1_000_000, output_tokens=1_000_000)

    outcome = run(config, ledger, library, client, FakeJudge(), max_rounds=1)

    assert outcome.cost_usd == pytest.approx(18.0)


# -- fetch pacing -----------------------------------------------------------


def test_fetches_are_paced_apart(config, ledger, library, client, searches, monkeypatch) -> None:
    """Observed: a run that pulled six videos quickly had its next thirteen
    fetches all fail inside nine seconds. Real backoff is M3; not provoking
    the throttle is cheap and belongs here."""
    slept: list[float] = []
    monkeypatch.setattr("marginalia.flow.discover.time.sleep", lambda s: slept.append(s))
    searches["*"] = [result("aaaaaaaaaaa"), result("bbbbbbbbbbb"), result("ccccccccccc")]
    config = replace(config, discover=replace(config.discover, fetch_delay_s=2.5))

    run(config, ledger, library, client, FakeJudge(), max_rounds=1)

    assert slept == [2.5, 2.5], "paced between fetches, not before the first"


def test_no_delay_is_configurable(config, ledger, library, client, searches, monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("marginalia.flow.discover.time.sleep", lambda s: slept.append(s))
    searches["*"] = [result("aaaaaaaaaaa"), result("bbbbbbbbbbb")]

    run(config, ledger, library, client, FakeJudge(), max_rounds=1)

    assert slept == []


def test_a_dry_run_never_waits(config, ledger, library, client, searches, monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("marginalia.flow.discover.time.sleep", lambda s: slept.append(s))
    searches["*"] = [result("aaaaaaaaaaa"), result("bbbbbbbbbbb")]
    config = replace(config, discover=replace(config.discover, fetch_delay_s=9.0))

    run(config, ledger, library, client, FakeJudge(), max_rounds=1, dry_run=True)

    assert slept == [], "nothing is fetched, so nothing needs pacing"


# -- threads have to be auditable, not just asserted ------------------------


def test_extracted_concepts_are_recorded_on_the_round(
    config, ledger, library, client, searches
) -> None:
    """`from_threads` alone only says concepts were passed in. Proving the
    loop-back actually fed forward needs to know *what* was extracted — a real
    run produced round-2 queries that were equally explicable by the brief, and
    the question was unanswerable because nothing recorded them."""
    searches["q1"] = [result("aaaaaaaaaaa")]
    searches["q2"] = [result("bbbbbbbbbbb")]
    judge = FakeJudge(
        queries=[["q1"], ["q2"]],
        metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")},
        openings={"aaaaaaaaaaa": (VERDICT_YES, "good", ["kv cache eviction", "prefix matching"])},
    )

    outcome = run(config, ledger, library, client, judge, max_rounds=2)

    assert outcome.rounds[0].concepts == ["kv cache eviction", "prefix matching"]
    assert outcome.rounds[1].concepts == []


def test_a_round_that_found_nothing_records_no_concepts(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_YES, "clear")})

    outcome = run(config, ledger, library, client, judge, max_rounds=1)

    assert outcome.rounds[0].concepts == []


# -- B3: stage 2 reads everything -------------------------------------------


def test_stage_two_receives_the_whole_transcript(
    config, ledger, library, client, searches
) -> None:
    """The change the phase is for. An opening cannot tell a video that delivers
    from one that promises well and never arrives — for a thousand words they
    are the same video, and padding is exactly what stage 2 exists to catch."""
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")})
    provider = FakeTranscripts()
    provider.lines = [
        TranscriptLine(0, 2000, "the opening promise"),
        TranscriptLine(600_000, 602_000, "the payoff ten minutes later"),
    ]

    run(config, ledger, library, client, judge, max_rounds=1, provider=provider)

    seen = judge.transcripts_seen[0]
    assert "the opening promise" in seen
    assert "the payoff ten minutes later" in seen


def test_the_transcript_reaches_the_judge_with_timestamps(
    config, ledger, library, client, searches
) -> None:
    """The judge is asked to name *where* the value is. A clock it cannot see is
    a clock it will invent."""
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")})
    provider = FakeTranscripts()
    provider.lines = [TranscriptLine(3_723_000, 3_725_000, "an hour and two minutes in")]

    run(config, ledger, library, client, judge, max_rounds=1, provider=provider)

    assert judge.transcripts_seen[0].startswith("[01:02:03.000] ")


def test_the_verdict_is_logged_under_the_full_stage(
    config, ledger, library, client, searches
) -> None:
    """A different name for different evidence. Recording a whole-transcript
    verdict as `opening` would make the log claim evidence never read."""
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")})

    run(config, ledger, library, client, judge, max_rounds=1)

    stages = [d.stage for d in ledger.decisions_for("aaaaaaaaaaa")]
    assert STAGE_FULL in stages


def test_the_prompt_version_is_recorded(config, ledger, library, client, searches) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")})

    run(config, ledger, library, client, judge, max_rounds=1)

    stage_two = [d for d in ledger.decisions_for("aaaaaaaaaaa") if d.stage == STAGE_FULL]
    assert stage_two[0].prompt_version == "1"


# -- B3: degraded evidence --------------------------------------------------


def test_degraded_evidence_keeps_the_video_and_the_transcript(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(
        metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")},
        openings={"aaaaaaaaaaa": (VERDICT_YES, "captions unreadable", [], EVIDENCE_DEGRADED)},
    )

    outcome = run(config, ledger, library, client, judge, max_rounds=1)

    assert outcome.stored_ids == ["aaaaaaaaaaa"]
    assert library.has_transcript("aaaaaaaaaaa")
    assert outcome.rounds[0].degraded == 1


def test_degraded_evidence_blocks_the_note(config, ledger, library, client, searches) -> None:
    """The whole point of the flag. The transcript is worth keeping — it is what
    ASR will replace — but a note built from it would be built from noise."""
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(
        metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")},
        openings={"aaaaaaaaaaa": (VERDICT_YES, "transcript is garbled", [], EVIDENCE_DEGRADED)},
    )

    run(config, ledger, library, client, judge, max_rounds=1)

    assert ledger.videos_needing_note(version="1") == []


def test_clear_evidence_leaves_the_note_queued(config, ledger, library, client, searches) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")})

    run(config, ledger, library, client, judge, max_rounds=1)

    assert [r.video_id for r in ledger.videos_needing_note(version="1")] == ["aaaaaaaaaaa"]


def test_the_evidence_grade_is_written_to_the_decision(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(
        metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")},
        openings={"aaaaaaaaaaa": (VERDICT_YES, "captions are noise", [], EVIDENCE_DEGRADED)},
    )

    run(config, ledger, library, client, judge, max_rounds=1)

    stage_two = [d for d in ledger.decisions_for("aaaaaaaaaaa") if d.stage == STAGE_FULL]
    assert stage_two[0].evidence == EVIDENCE_DEGRADED


# -- B3: the counters say which thing happened ------------------------------


def test_three_outcomes_land_on_three_counters(
    config, ledger, library, client, searches
) -> None:
    """A caption skip, a disk hit and a Gate 1 hit used to share two buckets.
    B5's planner reads these; a sum of three causes cannot be planned on."""
    in_library_already = "iiiiiiiiiii"
    on_disk = "ddddddddddd"
    no_captions = "nnnnnnnnnnn"

    # Already in the library: has a note, so Gate 1 drops it.
    ledger.mark_pending(in_library_already)
    ledger.mark_has_transcript(in_library_already, source="captions", provider="ytdlp")
    ledger.mark_noted(in_library_already, version="1", model="m")

    # On disk with no note: a candidate, but the bytes are already here.
    library.write_transcript(on_disk, [TranscriptLine(0, 1000, "already fetched")])
    ledger.mark_pending(on_disk)
    ledger.mark_has_transcript(on_disk, source="captions", provider="ytdlp")

    searches["*"] = [
        result(in_library_already),
        result(on_disk),
        result(no_captions, has_captions=False),
        result("aaaaaaaaaaa"),
    ]

    trusting = replace(config, discover=replace(config.discover, trust_caption_hint=True))
    outcome = run(trusting, ledger, library, client, FakeJudge(), max_rounds=1)
    report = outcome.rounds[0]

    assert report.known == 1, "Gate 1: already in the library"
    assert report.already_held == 1, "the transcript was already on disk"
    assert report.no_captions == 1, "routed to ASR without a fetch"
    assert report.failed == 0, "nothing actually failed"
    assert report.stored == 1


def test_a_real_fetch_failure_still_counts_as_failed(
    config, ledger, library, client, searches
) -> None:
    """The counter separation must not lose the thing it was originally for."""
    searches["*"] = [result("aaaaaaaaaaa")]
    provider = FakeTranscripts(failures={"aaaaaaaaaaa": TransientError("network")})

    outcome = run(config, ledger, library, client, FakeJudge(), max_rounds=1, provider=provider)

    assert outcome.rounds[0].failed == 1
    assert outcome.rounds[0].no_captions == 0
    assert outcome.rounds[0].already_held == 0


def test_an_unparsed_reply_is_counted(config, ledger, library, client, searches) -> None:
    """Counted, not swallowed. The same shape citation parsing reports, for the
    same reason: `/ask` once said 2 citations out of 104 because a parser threw
    away what it could not read and said nothing."""
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")})
    judge.unparsed = {"aaaaaaaaaaa"}

    outcome = run(config, ledger, library, client, judge, max_rounds=1)

    assert outcome.rounds[0].unparsed == 1
    assert outcome.stored_ids == ["aaaaaaaaaaa"], "an unreadable reply must never reject"


def test_no_transcript_content_reaches_the_report(
    config, ledger, library, client, searches, capsys
) -> None:
    """Hard rule 1, at the one place B3 newly put a whole transcript in flight.

    Stage 2 now reads every word of every video it judges. The report that comes
    back must still be counts and verdicts — the entire reason marginalia is a
    CLI is that heavy text never enters the conversation, and a judge that reads
    more is exactly where that would start to leak.
    """
    secret = "a phrase that appears only inside the transcript"
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")})
    provider = FakeTranscripts()
    provider.lines = [TranscriptLine(0, 2000, secret)]

    outcome = run(config, ledger, library, client, judge, max_rounds=1, provider=provider)

    assert secret in judge.transcripts_seen[0], "the judge should have read it"
    assert secret not in capsys.readouterr().out
    assert secret not in repr(outcome)
    assert all(secret not in " ".join(r.concepts) for r in outcome.rounds)


# -- B4: the territory map -------------------------------------------------


def test_the_map_is_stored_on_the_run(config, ledger, library, client, searches) -> None:
    """Same argument as storing the brief. When results look wrong the first
    question is *what did it think this topic covered?*, and a list of the
    queries that ran is the conclusion rather than the reasoning."""
    searches["*"] = [result("aaaaaaaaaaa")]
    mapper = FakeMapper(mapped("kv cache reuse llm"))

    outcome = run(config, ledger, library, client, FakeJudge(), max_rounds=1, mapper=mapper)

    row = ledger.get_run(outcome.run_id)
    assert row.territory_version == "1"
    assert "kv cache" in row.territory_map


def test_the_map_is_stored_before_any_judging(config, ledger, library, client, searches) -> None:
    """The same ordering rule the brief follows. A crash mid-run must leave the
    reasoning behind it readable, not a run whose map was never written down."""
    searches["*"] = [result("aaaaaaaaaaa")]
    seen: list[str | None] = []

    class Watcher(FakeJudge):
        def judge_metadata(self, brief, candidates, *, batch_size, territory=None):
            seen.append(ledger.list_runs()[0].territory_map)
            return super().judge_metadata(
                brief, candidates, batch_size=batch_size, territory=territory
            )

    run(config, ledger, library, client, Watcher(), max_rounds=1,
        mapper=FakeMapper(mapped("kv cache reuse llm")))

    assert seen and seen[0] is not None


def test_a_map_that_failed_leaves_the_loop_exactly_as_it_was(
    config, ledger, library, client, searches
) -> None:
    """The new step must not be able to break a loop that worked without it."""
    searches["*"] = [result("aaaaaaaaaaa")]
    empty = TerritoryMap(topic=TOPIC, version="1", parsed=False, problem="mapping call failed")

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"]]),
                  max_rounds=1, mapper=FakeMapper(empty))

    assert outcome.stored_ids == ["aaaaaaaaaaa"]
    assert outcome.rounds[0].queries == ["q1"]


# -- B4: queries are recorded, with their origins --------------------------


def test_every_query_is_recorded_with_its_origin(
    config, ledger, library, client, searches
) -> None:
    """Gate 5. Without these rows a run's queries are a list with no causal
    story, and "why did it search for that?" has no answer at all."""
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(queries=[["prompt caching tutorial"]])

    outcome = run(config, ledger, library, client, judge, max_rounds=1,
                  mapper=FakeMapper(mapped("kv cache reuse llm")))

    rows = ledger.queries_for_run(outcome.run_id)
    assert [(row.query, row.origin) for row in rows] == [
        ("prompt caching tutorial", ORIGIN_BRIEF),
        ("kv cache reuse llm", ORIGIN_TERRITORY),
    ]
    assert all(row.round == 1 for row in rows)


def test_a_recorded_query_says_what_it_found_and_cost(
    config, ledger, library, client, searches
) -> None:
    """The other half of the row. "Why did it search for that?" is one question
    and "what did that buy?" is the other, and B5's planner reads both."""
    searches["*"] = [result("aaaaaaaaaaa"), result("bbbbbbbbbbb")]

    outcome = run(config, ledger, library, client, FakeJudge(), max_rounds=1,
                  mapper=FakeMapper())

    row = ledger.queries_for_run(outcome.run_id)[0]
    assert row.results_count == 2
    assert row.provider == "fake"
    assert row.quota_units == 101


def test_a_thread_query_is_recorded_as_a_thread(
    config, ledger, library, client, searches
) -> None:
    """Round two's queries come from what stage 2 found unexplained, and the
    origin has to say so — otherwise the loop-back is invisible in the ledger."""
    searches["q1"] = [result("aaaaaaaaaaa")]
    searches["q2"] = [result("bbbbbbbbbbb")]
    judge = FakeJudge(
        queries=[["q1"], ["q2"]],
        metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")},
        openings={"aaaaaaaaaaa": (VERDICT_YES, "good", ["kv cache"])},
    )

    outcome = run(config, ledger, library, client, judge, max_rounds=2, mapper=FakeMapper())

    origins = {row.query: row.origin for row in ledger.queries_for_run(outcome.run_id)}
    assert origins["q1"] == ORIGIN_BRIEF
    assert origins["q2"] == ORIGIN_THREAD


def test_the_map_only_contributes_queries_in_round_one(
    config, ledger, library, client, searches
) -> None:
    """B4 builds the map. Revising it between rounds is B5's planner, and a map
    re-run every round would be that, arrived at by accident."""
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(queries=[["q1"], ["q2"]])
    mapper = FakeMapper(mapped("kv cache reuse llm"))

    outcome = run(config, ledger, library, client, judge, max_rounds=2, mapper=mapper)

    rows = ledger.queries_for_run(outcome.run_id)
    territory_rounds = {row.round for row in rows if row.origin == ORIGIN_TERRITORY}
    assert territory_rounds == {1}
    assert mapper.calls == 1


def test_a_query_that_failed_is_still_recorded(
    config, ledger, library, client, searches, monkeypatch
) -> None:
    """A query that failed is a query that ran. A table holding only the
    successful ones answers "what did this round search for?" wrongly."""

    def exploding(*args, **kwargs):
        raise TransientError("search is down")

    monkeypatch.setattr("marginalia.flow.discover.run_search", exploding)
    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"]]),
                  max_rounds=1, mapper=FakeMapper())

    rows = ledger.queries_for_run(outcome.run_id)
    assert [(row.query, row.results_count) for row in rows] == [("q1", None)]


def test_a_dry_run_records_its_queries_but_not_its_decisions(
    config, ledger, library, client, searches
) -> None:
    """Two different kinds of record. A `no` row poisons Gate 2 for the real run
    that follows; a queries row poisons nothing and says what a dry run really
    did search for."""
    searches["*"] = [result("aaaaaaaaaaa")]

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"]]),
                  max_rounds=1, dry_run=True, mapper=FakeMapper())

    assert [row.query for row in ledger.queries_for_run(outcome.run_id)] == ["q1"]
    assert ledger.decisions_for_run(outcome.run_id) == []


# -- B4: filters reach the provider, validated -----------------------------


def _with_filters(territory, filters, reasons=None):
    """The same map with one filtered query. Written out because a frozen
    dataclass holding a frozen dataclass needs two replaces."""
    return replace(
        territory,
        queries=[
            replace(territory.queries[0], filters=filters, reasons=reasons or {})
        ],
    )


def test_a_validated_filter_reaches_the_search(
    config, ledger, library, client, searches, monkeypatch
) -> None:
    from marginalia.flow import discover as discover_module

    seen: list[object] = []
    real = discover_module.run_search

    def watching(*args, **kwargs):
        seen.append(kwargs.get("filters"))
        return real(*args, **kwargs)

    monkeypatch.setattr(discover_module, "run_search", watching)
    searches["*"] = [result("aaaaaaaaaaa")]
    territory = _with_filters(mapped("kv cache reuse llm"), SearchFilters(order="date"))

    run(config, ledger, library, client, FakeJudge(queries=[[]]), max_rounds=1,
        mapper=FakeMapper(territory))

    assert seen[-1] == SearchFilters(order="date")


def test_a_bad_filter_is_rejected_before_the_search_and_counted(
    config, ledger, library, client, searches
) -> None:
    """Gate 3. The query still runs; only the filter is dropped, because
    rejecting toward width is the only safe direction."""
    searches["*"] = [result("aaaaaaaaaaa")]
    territory = _with_filters(
        mapped("kv cache reuse llm"), SearchFilters(published_after="not-a-date")
    )
    territory = replace(territory, recency=Recency(months=12, reason="turns over"))

    outcome = run(config, ledger, library, client, FakeJudge(queries=[[]]), max_rounds=1,
                  mapper=FakeMapper(territory))

    assert outcome.rounds[0].filters_rejected == 1
    assert outcome.rejected_filters[0].field == "publishedAfter"
    assert outcome.rounds[0].queries == ["kv cache reuse llm"], "the query still ran"
    row = ledger.queries_for_run(outcome.run_id)[0]
    # The model's unreadable date is gone from the row, and the window the map
    # reasoned is on it instead. The ledger says which of the two it was: a
    # derived filter names itself in its `why`, so "was this cutoff a judgement
    # or arithmetic?" is answerable without a rerun.
    assert row.filters_json is not None
    assert "not-a-date" not in row.filters_json
    assert "derived from the reasoned recency window" in row.filters_json


def test_a_filter_that_ran_is_recorded_with_its_reason(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    territory = _with_filters(
        mapped("kv cache reuse llm"),
        SearchFilters(order="date"),
        {"order": "the ecosystem turns over"},
    )

    outcome = run(config, ledger, library, client, FakeJudge(queries=[[]]), max_rounds=1,
                  mapper=FakeMapper(territory))

    row = ledger.queries_for_run(outcome.run_id)[0]
    assert "turns over" in row.filters_json
    assert '"date"' in row.filters_json


# -- B4: the judge receives the map ----------------------------------------


def test_both_judging_stages_receive_the_map(
    config, ledger, library, client, searches
) -> None:
    """Stage 1 especially: that is where a video whose title never says the
    topic's words is dropped before anything reads it."""
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")})
    territory = mapped("kv cache reuse llm")

    run(config, ledger, library, client, judge, max_rounds=1, mapper=FakeMapper(territory))

    assert judge.territory_seen[0].vocabulary == ["kv cache"]
    assert judge.full_territory_seen[0].vocabulary == ["kv cache"]


# -- B4: what the map surfaced alone ---------------------------------------


def test_a_candidate_only_the_map_found_is_attributed_to_the_map(
    config, ledger, library, client, searches
) -> None:
    """The phase's value, counted rather than claimed. A video the search never
    returns is invisible to every stage after it, so this set is the entire
    difference the map makes."""
    searches["q1"] = [result("aaaaaaaaaaa")]
    searches["kv cache reuse llm"] = [result("aaaaaaaaaaa"), result("bbbbbbbbbbb")]

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"]]), max_rounds=1,
                  mapper=FakeMapper(mapped("kv cache reuse llm")))

    report = outcome.rounds[0]
    assert report.only_from(ORIGIN_TERRITORY) == ["bbbbbbbbbbb"]
    assert report.surfaced_by["aaaaaaaaaaa"] == {ORIGIN_BRIEF, ORIGIN_TERRITORY}


def test_credit_is_not_first_past_the_post(
    config, ledger, library, client, searches
) -> None:
    """Recording only the first origin would answer "would the old expansion
    have found this?" with whichever set of queries happened to run earlier."""
    searches["q1"] = [result("aaaaaaaaaaa")]
    searches["kv cache reuse llm"] = [result("aaaaaaaaaaa")]

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"]]), max_rounds=1,
                  mapper=FakeMapper(mapped("kv cache reuse llm")))

    assert outcome.rounds[0].only_from(ORIGIN_TERRITORY) == []


# -- B5: the planner drives the next round ---------------------------------


class FakePlanner:
    """A scripted plan per round, or none at all.

    ``None`` is the default and it matters for the same reason `FakeMapper`'s
    empty map does: every test written before B5 ran without a planner, and each
    must keep passing unchanged. A planner that proposes nothing has to leave
    the loop exactly as it was.
    """

    def __init__(self, plans=None, baseline=()) -> None:
        self._plans = list(plans or [])
        self._baseline = list(baseline)
        self.assessed = 0
        self.baseline_seen: list[list] = []
        self.assess_max_tokens_seen = None
        self.notes_sent = None
        self.calls: list[int] = []
        self.seen: list[RoundReport] = []
        self.quota_seen: list[object] = []
        self.rounds_left_seen: list[object] = []
        self.kept_so_far_seen: list[list] = []
        self.max_tokens_seen: list[object] = []
        self.searched_seen: list[list] = []

    def prompt(self, name):
        return FakePrompt()

    def assess_library(self, brief, notes, *, notes_read=0, max_tokens=None):
        self.assessed += 1
        self.assess_max_tokens_seen = max_tokens
        self.notes_sent = notes
        return LibraryCoverage(items=list(self._baseline), notes_read=notes_read)

    def plan(self, brief, territory, report, **kwargs):
        self.calls.append(report.number)
        self.seen.append(report)
        self.quota_seen.append(kwargs.get("quota_remaining"))
        self.rounds_left_seen.append(kwargs.get("rounds_left"))
        self.kept_so_far_seen.append(list(kwargs.get("kept_so_far") or []))
        self.baseline_seen.append(list(kwargs.get("baseline") or []))
        self.max_tokens_seen.append(kwargs.get("max_tokens"))
        self.searched_seen.append(list(kwargs.get("searched") or []))
        if self._plans:
            return self._plans.pop(0)
        return RoundPlan(round_number=report.number)


def planned(*texts, recommendation=RECOMMEND_CONTINUE, revisions=(), coverage=()):
    return RoundPlan(
        round_number=1,
        coverage=list(coverage) or [CoverageItem("shows code", False, "nothing yet")],
        queries=[
            PlannedQuery(text=text, origin=ORIGIN_PLANNER, origin_detail="gap - shows code")
            for text in texts
        ],
        revisions=list(revisions),
        recommendation=recommendation,
        reason="scripted",
    )


def test_the_planner_runs_once_per_round_except_the_last(
    config, ledger, library, client, searches
) -> None:
    """Planning the end of the final round spends a request on advice nobody
    will read — the same discipline as checking the ceilings before a round."""
    searches["*"] = [result("aaaaaaaaaaa")]
    planner = FakePlanner()

    run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"], ["q3"]]),
        max_rounds=3, mapper=FakeMapper(), planner=planner)

    assert planner.calls == [1, 2]


def test_a_round_two_query_comes_from_the_planner(
    config, ledger, library, client, searches
) -> None:
    """Gate 1, in code. The query has to reach the search *and* be recorded as
    the planner's, or "why did it search for that?" has no answer."""
    searches["*"] = [result("aaaaaaaaaaa")]
    planner = FakePlanner([planned("prompt cache ttl expiry")])

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"]]),
                  max_rounds=2, mapper=FakeMapper(), planner=planner)

    rows = ledger.queries_for_run(outcome.run_id)
    planner_rows = [row for row in rows if row.origin == ORIGIN_PLANNER]
    assert [row.query for row in planner_rows] == ["prompt cache ttl expiry"]
    assert planner_rows[0].round == 2
    assert planner_rows[0].origin_detail == "gap - shows code"


def test_the_planner_s_queries_run_before_the_expansion_s(
    config, ledger, library, client, searches
) -> None:
    """They were written against the gaps this run actually left open; the
    expansion is still working from the brief alone."""
    searches["*"] = [result("aaaaaaaaaaa")]
    planner = FakePlanner([planned("from the planner")])

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"]]),
                  max_rounds=2, mapper=FakeMapper(), planner=planner)

    assert outcome.rounds[1].queries[0] == "from the planner"
    assert "q2" in outcome.rounds[1].queries, "expansion still runs alongside it"


def test_the_planner_sees_what_the_round_kept_and_rejected(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa", "A kept video"), result("bbbbbbbbbbb", "A reject")]
    judge = FakeJudge(
        metadata={
            "aaaaaaaaaaa": (VERDICT_MAYBE, "unclear"),
            "bbbbbbbbbbb": (VERDICT_NO, "vendor marketing"),
        },
        openings={"aaaaaaaaaaa": (VERDICT_YES, "walks through the config", [])},
    )
    planner = FakePlanner()

    run(config, ledger, library, client, judge, max_rounds=2,
        mapper=FakeMapper(), planner=planner)

    report = planner.seen[0]
    assert [video.title for video in report.kept] == ["A kept video"]
    assert [video.title for video in report.rejected] == ["A reject"]
    assert report.rejected[0].reason == "vendor marketing"


def test_a_stage_two_rejection_reaches_the_planner_with_its_stage(
    config, ledger, library, client, searches
) -> None:
    """Which stage decided is part of the evidence: a metadata NO saw a title
    and a full NO read every word, and they are not the same claim."""
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(
        metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")},
        openings={"aaaaaaaaaaa": (VERDICT_NO, "never pays off the opening", [])},
    )
    planner = FakePlanner()

    run(config, ledger, library, client, judge, max_rounds=2,
        mapper=FakeMapper(), planner=planner)

    assert planner.seen[0].rejected[0].stage == STAGE_FULL


def test_the_plan_is_kept_on_the_round_that_produced_it(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    planner = FakePlanner([planned("next query")])

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"]]),
                  max_rounds=2, mapper=FakeMapper(), planner=planner)

    assert outcome.rounds[0].plan is not None
    assert outcome.rounds[1].plan is None, "the last round is never planned"


# -- B5: stopping ----------------------------------------------------------


def test_coverage_satisfied_is_its_own_stop_reason(
    config, ledger, library, client, searches
) -> None:
    """A run that stopped because the brief was covered and one that ran out of
    money are opposite outcomes. One word for both would hide the interesting
    one — and a planner declaring satisfaction in round one is a real failure
    mode that is only findable if it is labelled."""
    searches["*"] = [result("aaaaaaaaaaa")]
    planner = FakePlanner([planned(recommendation=RECOMMEND_STOP)])

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"], ["q3"]]),
                  max_rounds=3, mapper=FakeMapper(), planner=planner)

    assert outcome.stopped_because == STOP_COVERAGE_SATISFIED
    assert len(outcome.rounds) == 1
    assert ledger.get_run(outcome.run_id).stopped_because == STOP_COVERAGE_SATISFIED


def test_the_planner_cannot_outvote_a_budget_ceiling(
    config, ledger, library, client, searches
) -> None:
    """Advisory, never sovereign. `max_videos` fires inside the round, before
    the planner is consulted at all."""
    searches["*"] = [result("aaaaaaaaaaa"), result("bbbbbbbbbbb")]
    planner = FakePlanner([planned("more please")])

    outcome = run(config, ledger, library, client, FakeJudge(), max_rounds=3,
                  max_videos=1, mapper=FakeMapper(), planner=planner)

    assert outcome.stopped_because == STOP_MAX_VIDEOS
    assert planner.calls == []


def test_a_planner_that_failed_does_not_stop_the_loop(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]

    class Exploding(FakePlanner):
        def plan(self, brief, territory, report, **kwargs):
            return RoundPlan(round_number=report.number, parsed=False, problem="call failed")

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"]]),
                  max_rounds=2, mapper=FakeMapper(), planner=Exploding())

    assert outcome.stopped_because == STOP_MAX_ROUNDS
    assert len(outcome.rounds) == 2


# -- B5: territory revision -------------------------------------------------


def test_a_revision_is_applied_and_versioned_on_the_run(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    revision = TerritoryRevision(REVISE_ADD, REVISE_VOCABULARY, "TTL", "kept videos use it")
    planner = FakePlanner([planned("next", revisions=[revision])])

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"]]),
                  max_rounds=2, mapper=FakeMapper(mapped("kv cache reuse llm")), planner=planner)

    assert outcome.territory.revision == 2
    assert "TTL" in outcome.territory.vocabulary
    row = ledger.get_run(outcome.run_id)
    assert row.territory_version == "1/v2"
    assert "TTL" in row.territory_map
    assert '"history"' in row.territory_map, "the previous version stays on the run"


def test_a_run_with_no_revision_keeps_revision_one(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"]]),
                  max_rounds=2, mapper=FakeMapper(mapped("kv cache")), planner=FakePlanner())

    assert outcome.territory.revision == 1
    assert ledger.get_run(outcome.run_id).territory_version == "1"


# -- B5: the quota guard ---------------------------------------------------


def test_a_round_that_quota_cannot_pay_for_stops_cleanly(
    config, ledger, library, client, searches, monkeypatch
) -> None:
    """Gate 5. Before the round, not inside it — a round that dies halfway
    leaves the planner reading half a round as though it were the whole one."""
    searches["*"] = [result("aaaaaaaaaaa")]
    monkeypatch.setattr(
        "marginalia.flow.discover.quota_headroom", lambda config, ledger: (50, 101)
    )

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"]]),
                  max_rounds=2, mapper=FakeMapper(), planner=FakePlanner())

    assert outcome.stopped_because == STOP_QUOTA
    assert outcome.rounds == [], "no round row for a round that never ran"
    assert ledger.queries_for_run(outcome.run_id) == []


def test_an_unmetered_provider_in_the_chain_is_not_a_shortage(
    config, ledger, library, client, searches, monkeypatch
) -> None:
    """The overflow provider exists so a run continues when the metered one runs
    dry. Stopping for want of quota while a free fallback sits in the chain
    would be inventing a ceiling that does not apply."""
    searches["*"] = [result("aaaaaaaaaaa")]
    monkeypatch.setattr(
        "marginalia.flow.discover.quota_headroom", lambda config, ledger: (None, 0)
    )

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"]]),
                  max_rounds=1, mapper=FakeMapper(), planner=FakePlanner())

    assert outcome.stopped_because == STOP_MAX_ROUNDS


def test_quota_is_enough_for_the_round_that_was_planned(
    config, ledger, library, client, searches, monkeypatch
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    monkeypatch.setattr(
        "marginalia.flow.discover.quota_headroom", lambda config, ledger: (5000, 101)
    )

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"]]),
                  max_rounds=1, mapper=FakeMapper(), planner=FakePlanner())

    assert outcome.stopped_because == STOP_MAX_ROUNDS
    assert outcome.rounds[0].candidates == 1


def test_the_planner_is_told_what_quota_is_left(
    config, ledger, library, client, searches, monkeypatch
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    monkeypatch.setattr(
        "marginalia.flow.discover.quota_headroom", lambda config, ledger: (4000, 101)
    )
    planner = FakePlanner()

    run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"]]),
        max_rounds=2, mapper=FakeMapper(), planner=planner)

    assert planner.quota_seen == [4000]


# -- B5: the queries row records what actually ran -------------------------


def test_a_filter_the_provider_could_not_apply_is_not_recorded_as_applied(
    config, ledger, library, client, searches, monkeypatch
) -> None:
    """The B4 defect, closed. The fallback ran the query unfiltered — the right
    direction — but the row said `order=date` ran. The planner reads that
    history to choose what to try next, and a record of an experiment that did
    not happen is worse than no record."""
    def unfiltered_provider(name, **_):
        class P:
            name = "cannot_filter"

            def search(self, query, limit=25, filters=None):
                return [result("aaaaaaaaaaa")]

            def quota_remaining(self):
                return None

            def units_spent(self):
                return 0

            def applied_filters(self):
                return SearchFilters()

        return P()

    monkeypatch.setattr("marginalia.flow.search.get_search_provider", unfiltered_provider)
    territory = mapped("kv cache reuse llm")
    territory = replace(
        territory,
        queries=[replace(territory.queries[0], filters=SearchFilters(order="date"))],
    )

    outcome = run(config, ledger, library, client, FakeJudge(queries=[[]]), max_rounds=1,
                  mapper=FakeMapper(territory), planner=FakePlanner())

    row = ledger.queries_for_run(outcome.run_id)[0]
    assert row.filters_json is None, "nothing ran, so nothing is recorded as having run"


def test_a_kept_video_carries_the_reason_of_the_stage_that_decided_it(
    config, ledger, library, client, searches
) -> None:
    """The planner builds coverage claims out of these sentences.

    A video routed through stage 2 was kept on what the transcript said, not on
    what its title suggested — and the two reasons are not interchangeable.
    Recording the stage-1 reason for a stage-2 keep would hand the planner "the
    title looks right" as evidence that part of the brief is satisfied. This
    test exists because that is exactly what the first version did.
    """
    searches["*"] = [result("aaaaaaaaaaa", "A video")]
    judge = FakeJudge(
        metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "title is ambiguous")},
        openings={"aaaaaaaaaaa": (VERDICT_YES, "the transcript shows the config", [])},
    )
    planner = FakePlanner()

    run(config, ledger, library, client, judge, max_rounds=2,
        mapper=FakeMapper(), planner=planner)

    kept = planner.seen[0].kept[0]
    assert kept.reason == "the transcript shows the config"
    assert kept.stage == STAGE_FULL


def test_a_stage_one_yes_reaches_the_planner_with_the_transcript_s_reason(
    config, ledger, library, client, searches
) -> None:
    """The planner is never shown a metadata reason for a kept video again.

    It used to be, for a stage-1 YES, and that was the honest label for the
    evidence at the time — the problem was the evidence, not the label. Now
    every keeper has been read, so what the planner sees for every one of them
    is what the transcript established. A coverage claim can rest on that.
    """
    searches["*"] = [result("aaaaaaaaaaa", "A video")]
    judge = FakeJudge(
        metadata={"aaaaaaaaaaa": (VERDICT_YES, "clearly on brief")},
        openings={"aaaaaaaaaaa": (VERDICT_YES, "the transcript walks through the setup", [])},
    )
    planner = FakePlanner()

    run(config, ledger, library, client, judge, max_rounds=2,
        mapper=FakeMapper(), planner=planner)

    kept = planner.seen[0].kept[0]
    assert kept.reason == "the transcript walks through the setup"
    assert kept.stage == STAGE_FULL


def test_the_planner_is_told_how_much_room_is_left_in_this_run(
    config, ledger, library, client, searches
) -> None:
    """From the ceiling the run is actually working to, not the configured one.

    `--max-rounds 2` against a config that says 3 means one more round, and a
    planner told it has two is writing for a round that will never happen.
    """
    searches["*"] = [result("aaaaaaaaaaa")]
    planner = FakePlanner()

    run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"], ["q3"]]),
        max_rounds=3, mapper=FakeMapper(), planner=planner)

    assert planner.rounds_left_seen == [2, 1]


def test_a_filter_the_planner_proposed_and_lost_is_counted_on_the_run_not_the_round(
    config, ledger, library, client, searches
) -> None:
    """It was rejected between rounds, for a query the next round would have run.

    Folding it into "filters rejected on the queries this round ran" would put
    two meanings in one integer — the thing every counter here is separated to
    avoid.
    """
    searches["*"] = [result("aaaaaaaaaaa")]
    plan = RoundPlan(
        round_number=1,
        coverage=[CoverageItem("a", False, "x")],
        queries=[PlannedQuery("next query", ORIGIN_PLANNER)],
        rejected_filters=[
            FilterRejection("next query", "videoDuration", "long", "never on a main query")
        ],
    )

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"]]),
                  max_rounds=2, mapper=FakeMapper(), planner=FakePlanner([plan]))

    assert outcome.rounds[0].filters_rejected == 0
    assert [r.field for r in outcome.rejected_filters] == ["videoDuration"]


# -- cleanup: a degraded keeper never counts as coverage ---------------------


def test_a_degraded_keeper_reaches_the_planner_labelled_as_such(
    config, ledger, library, client, searches
) -> None:
    """The gap this closes: `evidence` was graded, counted and used to suppress
    the note, and then dropped on the floor before the planner saw anything.

    A DEGRADED keeper is a video nobody could read. It is stored, it gets no
    note, and `/ask` will never answer from it — so a planner that reads it as
    an ordinary keeper concludes a part of the brief is satisfied by evidence
    that does not exist, and stops looking for the video that would satisfy it.
    """
    searches["*"] = [result("aaaaaaaaaaa", "A garbled video")]
    judge = FakeJudge(
        metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")},
        openings={
            "aaaaaaaaaaa": (VERDICT_YES, "captions are unreadable", [], EVIDENCE_DEGRADED)
        },
    )
    planner = FakePlanner()

    run(config, ledger, library, client, judge, max_rounds=2,
        mapper=FakeMapper(), planner=planner)

    kept = planner.seen[0].kept
    assert [video.title for video in kept] == ["A garbled video"]
    assert kept[0].evidence == EVIDENCE_DEGRADED
    assert kept[0].unreadable


def test_a_clean_keeper_carries_clear_evidence(
    config, ledger, library, client, searches
) -> None:
    """The other half. If everything read DEGRADED the split above would be
    a section heading that always fires, which is no signal at all."""
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(
        metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")},
        openings={"aaaaaaaaaaa": (VERDICT_YES, "walks through the config", [])},
    )
    planner = FakePlanner()

    run(config, ledger, library, client, judge, max_rounds=2,
        mapper=FakeMapper(), planner=planner)

    assert planner.seen[0].kept[0].evidence == EVIDENCE_CLEAR
    assert not planner.seen[0].kept[0].unreadable


def test_a_stage_one_yes_can_still_be_graded_degraded(
    config, ledger, library, client, searches
) -> None:
    """Every keeper now carries a grade stage 2 actually assigned.

    Before this, a stage-1 YES was `CLEAR` by definition — the honest default
    for a verdict with no transcript behind it, but also a grade no reader had
    earned. A confident title on a garbled transcript was stored, noted, and
    answered from. Now the same video is stored, graded `DEGRADED`, and not
    noted, which is the outcome rule 13 already specified for everything else.
    """
    searches["*"] = [result("aaaaaaaaaaa")]
    planner = FakePlanner()

    run(config, ledger, library, client,
        FakeJudge(
            metadata={"aaaaaaaaaaa": (VERDICT_YES, "obviously relevant")},
            openings={
                "aaaaaaaaaaa": (VERDICT_YES, "captions are mangled", [], EVIDENCE_DEGRADED)
            },
        ),
        max_rounds=2, mapper=FakeMapper(), planner=planner)

    kept = planner.seen[0].kept[0]
    assert kept.stage == STAGE_FULL
    assert kept.evidence == EVIDENCE_DEGRADED
    assert kept.unreadable


# -- cleanup: channel_id ----------------------------------------------------


def test_a_stored_video_records_its_channel_id(
    config, ledger, library, client, searches
) -> None:
    """Free with the fetch. Backfilling it later means one network call per
    video already in the library, which is why it goes in before anything reads
    it rather than after something wants it."""
    searches["*"] = [result("aaaaaaaaaaa")]

    run(config, ledger, library, client, FakeJudge(), mapper=FakeMapper())

    assert ledger.get("aaaaaaaaaaa").channel_id == "UCtestchannelid"
    assert library.read_meta("aaaaaaaaaaa")["channel_id"] == "UCtestchannelid"


def test_a_provider_that_reports_no_channel_id_does_not_erase_one(
    config, ledger, library, client, searches
) -> None:
    """`COALESCE` only helps if the caller passes `None` rather than `""`."""
    searches["*"] = [result("aaaaaaaaaaa")]
    ledger.mark_pending("aaaaaaaaaaa")
    ledger.mark_has_transcript(
        "aaaaaaaaaaa", source="captions", provider="ytdlp", channel_id="UCearlier"
    )
    ledger.mark_pending("aaaaaaaaaaa")

    from marginalia.contracts import ACQUIRED, Acquisition
    from marginalia.flow import discover as flow

    flow._store(
        library, ledger, result("aaaaaaaaaaa"),
        Acquisition(ACQUIRED, result=transcript_without_channel(), provider="ytdlp"),
    )

    assert ledger.get("aaaaaaaaaaa").channel_id == "UCearlier"


# -- Gate 1, run-scoped: nothing is judged twice in one run -------------------
#
# `in_library` asks whether a video has a note, and notes are written by a
# worker that starts after the run ends — so a video stored in round one is
# invisible to Gate 1 in round two and was judged all over again. Measured on
# run `19941ec23cb7`: `EGNykTqFDBs` carries two `metadata`/`yes` rows, one per
# round. The disk check meant no second download; nothing stopped the second
# model call, or the second copy of the video in the round's evidence.


def test_a_video_stored_in_round_one_is_not_judged_again_in_round_two(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(queries=[["q1"], ["q2"]])

    outcome = run(config, ledger, library, client, judge, max_rounds=2,
                  mapper=FakeMapper(), planner=FakePlanner())

    assert outcome.stored_ids == ["aaaaaaaaaaa"]
    # Stage 1 saw it once, stage 2 saw it once. Both used to run twice.
    assert judge.opening_calls == ["aaaaaaaaaaa"]
    stages = [row.stage for row in ledger.decisions_for("aaaaaaaaaaa")]
    assert stages.count(STAGE_METADATA) == 1
    assert stages.count(STAGE_FULL) == 1


def test_a_video_stored_this_run_gets_its_own_counter(
    config, ledger, library, client, searches
) -> None:
    """Not folded into `known`, under rule 14.

    `known` means Gate 1 found it in the library — a video with a note, from
    some earlier run. This is a video *this run* stored moments ago, which has
    no note and is a different fact about a different thing. The planner reads
    these numbers.
    """
    searches["*"] = [result("aaaaaaaaaaa")]

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"]]),
                  max_rounds=2, mapper=FakeMapper(), planner=FakePlanner())

    assert outcome.rounds[1].known_this_run == 1
    assert outcome.rounds[1].known == 0
    assert outcome.rounds[1].candidates == 1


def test_the_run_scoped_gate_writes_no_rejection(
    config, ledger, library, client, searches
) -> None:
    """Skipping is not judging, and the ledger must not say otherwise.

    Gate 1's library branch logs a `no`, which is right for a video that is
    genuinely in the library. Logging one here would tell Gate 2, in every
    later run on this topic, that a video this run *kept* had been turned down
    — rule 8's invisible error, arriving through a counter fix. So the row is a
    `maybe`, which is what every other un-judged candidate gets.
    """
    searches["*"] = [result("aaaaaaaaaaa")]

    run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"]]),
        max_rounds=2, mapper=FakeMapper(), planner=FakePlanner())

    search_rows = [
        row for row in ledger.decisions_for("aaaaaaaaaaa") if row.stage == STAGE_SEARCH
    ]
    assert [row.verdict for row in search_rows] == [VERDICT_MAYBE, VERDICT_MAYBE]
    assert search_rows[1].reason == "stored earlier in this run"


def test_a_video_the_round_rejected_is_still_stopped_by_gate_two(
    config, ledger, library, client, searches
) -> None:
    """The other half of "nothing is judged twice", and it already worked.

    A stage-2 NO writes a `no` for this topic, so Gate 2 catches it next round
    without any run-scoped help. Only *kept* videos leaked, which is why the
    fix keys on `stored_ids` and not on everything the run touched.
    """
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(
        queries=[["q1"], ["q2"]],
        openings={"aaaaaaaaaaa": (VERDICT_NO, "not about the brief", [])},
    )

    outcome = run(config, ledger, library, client, judge, max_rounds=2,
                  mapper=FakeMapper(), planner=FakePlanner())

    assert judge.opening_calls == ["aaaaaaaaaaa"]
    # This run's own round-1 NO, counted apart from a NO from an earlier run:
    # it used to be `rejected_before`, which the planner reads as "from an
    # earlier run" (§12.4, R1 F11).
    assert (outcome.rounds[1].rejected_this_run, outcome.rounds[1].rejected_before) == (1, 0)


def test_a_dry_run_stores_nothing_so_the_gate_holds_nothing(
    config, ledger, library, client, searches
) -> None:
    """`stored_ids` is empty on a dry run, which is the honest answer: a dry run
    stored nothing, so there is nothing for the run-scoped gate to skip."""
    searches["*"] = [result("aaaaaaaaaaa")]

    outcome = run(config, ledger, library, client, FakeJudge(), max_rounds=1, dry_run=True)

    assert outcome.stored_ids == []
    assert outcome.rounds[0].known_this_run == 0


# -- the plan is stored, not just its conclusions -----------------------------


def test_the_plan_is_stored_on_the_ledger_whole(
    config, ledger, library, client, searches
) -> None:
    """The queries a plan produced were recorded; the reasoning was not.

    So "why did the planner call that a gap?" had no answer from a stored run —
    the one question storing runs exists to make answerable.
    """
    searches["*"] = [result("aaaaaaaaaaa")]
    plan = planned(
        "pgbouncer listen notify transaction mode",
        coverage=[CoverageItem("SET/LISTEN pitfalls", False, "no kept video names them")],
    )
    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"]]),
                  max_rounds=2, mapper=FakeMapper(), planner=FakePlanner([plan]))

    rows = ledger.plans_for_run(outcome.run_id)
    assert [row.round for row in rows] == [1], "the last round is never planned"
    stored = json.loads(rows[0].plan_json)
    assert stored["coverage"] == [
        {"part": "SET/LISTEN pitfalls", "satisfied": False,
         "detail": "no kept video names them"}
    ]
    assert stored["recommendation"] == RECOMMEND_CONTINUE
    assert rows[0].prompt_version == "1"


def test_a_run_that_never_planned_stores_no_plan(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]

    outcome = run(config, ledger, library, client, FakeJudge(), max_rounds=1)

    assert ledger.plans_for_run(outcome.run_id) == []


# -- the planner is handed the run, not the round ------------------------------


def test_the_planner_is_given_every_video_kept_so_far(
    config, ledger, library, client, searches
) -> None:
    """The loop's half of the cumulative-coverage fix.

    Round 2's planning call must see round 1's keeper. Without it, coverage is
    assessed against six videos when the run has found twenty — which is how one
    run marked an item covered in round 1 and a gap in round 2.
    """
    searches["q1"] = [result("aaaaaaaaaaa", "Round one keeper")]
    searches["q2"] = [result("bbbbbbbbbbb", "Round two keeper")]
    planner = FakePlanner([planned("next"), planned("next again")])

    run(config, ledger, library, client,
        FakeJudge(queries=[["q1"], ["q2"], ["q3"]]),
        max_rounds=3, mapper=FakeMapper(), planner=planner)

    second = planner.kept_so_far_seen[1]
    assert [number for number, _ in second] == [1, 2]
    assert [video.title for _, video in second] == [
        "Round one keeper", "Round two keeper"
    ]


def test_the_first_plan_sees_only_the_first_round(
    config, ledger, library, client, searches
) -> None:
    """Cumulative does not mean inflated: in round one the two are the same set."""
    searches["*"] = [result("aaaaaaaaaaa", "The only one")]
    planner = FakePlanner([planned("next")])

    run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"]]),
        max_rounds=2, mapper=FakeMapper(), planner=planner)

    assert [number for number, _ in planner.kept_so_far_seen[0]] == [1]


def test_the_mapping_call_gets_its_own_ceiling(
    config, ledger, library, client, searches
) -> None:
    """The map is a long structured document and it overran the global 8,192,
    losing a whole run its territory queries."""
    searches["*"] = [result("aaaaaaaaaaa")]
    mapper = FakeMapper()

    run(config, ledger, library, client, FakeJudge(), max_rounds=1, mapper=mapper)

    assert mapper.max_tokens_seen == config.discover.territory_max_tokens
    assert config.discover.territory_max_tokens > config.llm.max_tokens


def test_the_planning_call_gets_its_own_ceiling(
    config, ledger, library, client, searches
) -> None:
    """Raised for a reason created by this change: a cumulative kept set makes
    both halves of the call bigger, and a truncated plan is discarded whole."""
    searches["*"] = [result("aaaaaaaaaaa")]
    planner = FakePlanner([planned("next")])

    run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"]]),
        max_rounds=2, mapper=FakeMapper(), planner=planner)

    assert planner.max_tokens_seen[0] == config.discover.planner_max_tokens


# -- a refused filter and a skipped duplicate are different things -------------


def test_a_duplicate_query_is_not_reported_as_a_rejected_filter(
    config, ledger, library, client, searches
) -> None:
    """Round 1 of a real run announced "8 filters rejected" when all eight were
    duplicate-query skips and not one filter had been refused. Rule 14, third
    occurrence, in the counter the planner reads."""
    searches["*"] = [result("aaaaaaaaaaa")]
    # The same query twice in one round: the second is a duplicate skip.
    judge = FakeJudge(queries=[["q1", "q1"]])

    outcome = run(config, ledger, library, client, judge, max_rounds=1,
                  mapper=FakeMapper())

    assert outcome.rounds[0].duplicates_skipped == 1
    assert outcome.rounds[0].filters_rejected == 0


def test_the_brief_calls_get_their_own_ceiling(
    config, ledger, library, client, searches
) -> None:
    """Fourth task on this provider to overrun the global ceiling, and the
    quietest: a truncated review falls back to the unreviewed draft with one
    warning line, so every verdict in that run is measured against a brief whose
    gaps were never closed."""
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge()

    run(config, ledger, library, client, judge, max_rounds=1)

    assert judge.brief_max_tokens_seen == config.discover.brief_max_tokens
    assert config.discover.brief_max_tokens > config.llm.max_tokens


# -- the library baseline, computed once before round one ----------------------


def test_the_library_is_assessed_before_the_first_round(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    library.write_note("bbbbbbbbbbb", "# An old note\n\nIt covers the mechanism in code.\n")
    planner = FakePlanner(baseline=[CoverageItem("shows code", True, "the old note")])

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"]]),
                  max_rounds=2, mapper=FakeMapper(), planner=planner)

    assert planner.assessed == 1, "assessed once, before any round"
    assert [i.part for i in outcome.baseline.items] == ["shows code"]


def test_the_baseline_reaches_the_planner(
    config, ledger, library, client, searches
) -> None:
    """The whole point. Without it the planner reads an empty shelf and names
    gaps for things the library can already answer."""
    searches["*"] = [result("aaaaaaaaaaa")]
    library.write_note("bbbbbbbbbbb", "# An old note\n\nCovers retrieval scoring.\n")
    planner = FakePlanner(baseline=[CoverageItem("retrieval scoring", True, "the old note")])

    run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"]]),
        max_rounds=2, mapper=FakeMapper(), planner=planner)

    assert [i.part for i in planner.baseline_seen[0]] == ["retrieval scoring"]


def test_the_baseline_is_sent_summary_lines_not_whole_notes(
    config, ledger, library, client, searches
) -> None:
    """v1 sent the library entire with no budget check. At 646 notes that asked
    for 1,833,240 tokens against a 1,048,576 ceiling and failed *open*, so the
    planner assumed nothing was covered and re-searched ground already held.

    Not the M7 selector, which was the obvious repair: selection would hit the
    same ceiling again eventually, and this is the second caller to hit it.
    Summary lines scale — 720 notes is ~48k tokens, 22x under the ceiling."""
    searches["*"] = [result("aaaaaaaaaaa")]
    body = "# A note\n\nIt covers the mechanism in code.\n" + ("filler line\n" * 400)
    library.write_note("bbbbbbbbbbb", body)
    planner = FakePlanner(baseline=[CoverageItem("shows code", True, "the old note")])

    run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"]]),
        max_rounds=2, mapper=FakeMapper(), planner=planner)

    sent = planner.notes_sent
    assert "filler line" not in sent, "the note body never reaches the assessment"
    assert "bbbbbbbbbbb" in sent, "but the id does"
    assert len(sent.splitlines()) == 1, "one line per note"
    assert len(sent) < len(body), "and it is smaller than the note it stands for"


def test_an_oversized_baseline_is_refused_rather_than_sent(
    config, ledger, library, client, searches
) -> None:
    """The guard the original never had. It fails open exactly as the provider's
    400 did — the planner still assumes nothing is covered — but it says so
    before spending the call, instead of being discovered from a rejection."""
    searches["*"] = [result("aaaaaaaaaaa")]
    for n in range(6):
        library.write_note(f"ccccccccc{n:02d}"[:11], f"# Note {n}\n\nA claim about things.\n")
    config = replace(config, ask=make_ask_config(max_input_tokens=1))
    planner = FakePlanner(baseline=[CoverageItem("shows code", True, "the old note")])

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"]]),
                  max_rounds=2, mapper=FakeMapper(), planner=planner)

    assert planner.assessed == 0, "no call was made"
    assert outcome.baseline.parsed is False
    assert "over the" in (outcome.baseline.problem or "")
    assert outcome.baseline.items == [], "and the planner sees nothing covered"


def test_an_empty_library_is_not_assessed_at_all(
    config, ledger, library, client, searches
) -> None:
    """A fresh topic on a fresh library. The call would spend the whole prompt
    to be told there is nothing there."""
    searches["*"] = [result("aaaaaaaaaaa")]
    planner = FakePlanner()

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"]]),
                  max_rounds=2, mapper=FakeMapper(), planner=planner)

    assert planner.assessed == 0
    assert outcome.baseline.items == []


def test_a_dry_run_does_not_assess_the_library(
    config, ledger, library, client, searches
) -> None:
    """A dry run plans nothing, so the baseline would be computed for a reader
    that never runs."""
    searches["*"] = [result("aaaaaaaaaaa")]
    library.write_note("bbbbbbbbbbb", "# A note\n\nSomething.\n")
    planner = FakePlanner()

    run(config, ledger, library, client, FakeJudge(), max_rounds=1,
        dry_run=True, mapper=FakeMapper(), planner=planner)

    assert planner.assessed == 0


def test_the_baseline_is_stored_on_the_run(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    library.write_note("bbbbbbbbbbb", "# A note\n\nCovers the write path.\n")
    planner = FakePlanner(baseline=[CoverageItem("write path", True, "the old note")])

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"]]),
                  max_rounds=2, mapper=FakeMapper(), planner=planner)

    stored = json.loads(ledger.get_run(outcome.run_id).library_baseline)
    assert stored["items"] == [
        {"part": "write path", "satisfied": True, "detail": "the old note"}
    ]
    assert stored["notes_read"] == 1


def test_the_assessment_call_gets_its_own_ceiling(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    library.write_note("bbbbbbbbbbb", "# A note\n\nSomething.\n")
    planner = FakePlanner()

    run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"]]),
        max_rounds=2, mapper=FakeMapper(), planner=planner)

    assert planner.assess_max_tokens_seen == config.discover.baseline_max_tokens


# -- stage 2's RELEVANT ranges reach the ledger ---------------------------------


def test_stage_two_stores_its_ranges_and_no_other_stage_does(
    config, ledger, library, client, searches
) -> None:
    """Stored for inspection: a range can be checked against the transcript it
    points at. Stage 1 and the search gates have no ranges and store NULL."""
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")})
    scripted = judge.judge_full
    judge.judge_full = lambda *a, **k: replace(
        scripted(*a, **k), relevant_text="00:12:30-00:18:45; 00:25:00-00:27:10"
    )

    run(config, ledger, library, client, judge, max_rounds=1)

    rows = ledger.decisions_for("aaaaaaaaaaa")
    stage_two = [row for row in rows if row.stage == STAGE_FULL]
    others = [row for row in rows if row.stage != STAGE_FULL]
    assert stage_two[0].relevant == "00:12:30-00:18:45; 00:25:00-00:27:10"
    assert {row.stage for row in others} >= {STAGE_SEARCH, "metadata"}
    assert all(row.relevant is None for row in others)


def test_a_stage_two_maybe_is_counted_as_unparsed_and_still_kept(
    config, ledger, library, client, searches
) -> None:
    """The real parser, not a scripted verdict: a MAYBE reply is kept and stored
    and counted in `unparsed` — and recorded as what it is, a judged MAYBE, not
    as the YES it used to be indistinguishable from. It is not coverage, and
    because it *was* judged the next run does not ask again."""
    from marginalia.brain.judge import parse_full

    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")})
    maybe = "\n".join([
        "VERDICT: MAYBE",
        "EVIDENCE: CLEAR",
        "REASON: walks through pool_mode settings.",
        "RELEVANT: 00:04:20-00:09:15",
        "UNEXPLAINED: NONE",
    ])
    judge.judge_full = lambda brief, candidate, **_: parse_full(maybe, candidate.video_id)

    outcome = run(config, ledger, library, client, judge, max_rounds=1)

    assert outcome.rounds[0].unparsed == 1
    assert outcome.rounds[0].uncertain == 1
    assert outcome.stored_ids == ["aaaaaaaaaaa"]
    assert [v.covers for v in outcome.rounds[0].kept] == [False]
    stage_two = [d for d in ledger.decisions_for("aaaaaaaaaaa") if d.stage == STAGE_FULL][0]
    assert (stage_two.verdict, stage_two.evidence) == ("maybe", "CLEAR")
    assert stage_two.outcome == "judged" and stage_two.is_judgement
    assert stage_two.reason == "walks through pool_mode settings."
    assert stage_two.relevant == "00:04:20-00:09:15"


# -- a fetched transcript is never thrown away ------------------------------
#
# Acquisition is the fragile step (rule 12), and rule 6 leaves `pending` only
# as the trace of a fetch that *died*. A ceiling that stops the run is not a
# fetch dying, so it must leave neither a discarded transcript nor a `pending`
# row behind it.


class _SpendingJudge(FakeJudge):
    """Stage 1 costs one request, as it does against a real client."""

    def __init__(self, client, **kwargs) -> None:
        super().__init__(**kwargs)
        self._client = client

    def judge_metadata(self, brief, candidates, *, batch_size, territory=None):
        self._client.usage = Usage(
            requests=self._client.usage.requests + 1,
            input_tokens=self._client.usage.input_tokens,
            output_tokens=self._client.usage.output_tokens + 1_000_000,
        )
        return super().judge_metadata(
            brief, candidates, batch_size=batch_size, territory=territory
        )


def test_the_request_ceiling_is_checked_before_a_fetch_not_after(
    config, ledger, library, client, searches
) -> None:
    """The ceiling used to be checked between the fetch and stage 2, so the
    transcript just paid for was dropped and the row stayed `pending` — the
    state that tells the next run a fetch crashed, when none did."""
    searches["*"] = [result("aaaaaaaaaaa")]
    client.usage = Usage(requests=99)
    provider = FakeTranscripts()

    outcome = run(
        config, ledger, library, client, _SpendingJudge(client),
        provider=provider, max_rounds=1, max_requests=100,
    )

    assert outcome.stopped_because == STOP_MAX_REQUESTS
    assert provider.fetched == [], "the ceiling was reached before the fetch"
    assert ledger.get("aaaaaaaaaaa") is None


def test_the_cost_ceiling_is_checked_before_a_fetch_not_after(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    config = replace(config, discover=replace(config.discover, max_cost_usd=1.0))
    provider = FakeTranscripts()

    outcome = run(
        config, ledger, library, client, _SpendingJudge(client),
        provider=provider, max_rounds=1,
    )

    assert outcome.stopped_because == STOP_MAX_COST
    assert provider.fetched == []
    assert ledger.get("aaaaaaaaaaa") is None


def test_a_refusal_during_stage_two_keeps_the_transcript_already_fetched(
    config, ledger, library, client, searches
) -> None:
    """A 402 at stage 2 stops the run (rule 24) and says nothing about the
    video. The transcript is already in hand, so it is stored before anything
    is asked of the judge: storing never depended on the verdict anyway."""
    from marginalia.llm import LlmBillingError

    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge()

    def refuse(*_args, **_kwargs):
        raise LlmBillingError("no balance")

    judge.judge_full = refuse

    with pytest.raises(LlmBillingError):
        run(config, ledger, library, client, judge, max_rounds=1)

    assert library.has_transcript("aaaaaaaaaaa")
    assert ledger.get("aaaaaaaaaaa").status == STATUS_HAS_TRANSCRIPT
    # No stage-2 row: nothing judged it, and inventing a verdict would be worse.
    assert STAGE_FULL not in {d.stage for d in ledger.decisions_for("aaaaaaaaaaa")}


def test_concepts_from_a_rejected_video_do_not_steer_the_next_round(
    config, ledger, library, client, searches
) -> None:
    """Both prompts that read these concepts say they come from videos that were
    kept. A rejected video's jargon is often the wrong meaning of a shared word —
    the failure the stage-2 prompt says cost eight good videos — so feeding it
    forward aims the next round at the subject the judge just turned down."""
    searches["q1"] = [result("aaaaaaaaaaa"), result("bbbbbbbbbbb")]
    searches["q2"] = [result("ccccccccccc")]
    judge = FakeJudge(
        queries=[["q1"], ["q2"]],
        openings={
            "aaaaaaaaaaa": (VERDICT_YES, "good", ["kv cache eviction"]),
            "bbbbbbbbbbb": (VERDICT_NO, "a different subject", ["semantic cache lookup"]),
        },
    )

    outcome = run(config, ledger, library, client, judge, max_rounds=2)

    assert outcome.rounds[0].concepts == ["kv cache eviction"]
    assert judge.query_calls[1][1] == ("kv cache eviction",)


def test_too_long_to_note_is_measured_as_the_note_worker_measures(
    config, ledger, library, client, searches
) -> None:
    """The worker refuses on the length of `transcript.txt`. Discovery used to
    measure the judge's shorter rendering, so a transcript between the two
    lengths was counted as coverage and then never noted."""
    from marginalia.core.library import render_transcript

    searches["*"] = [result("aaaaaaaaaaa")]
    provider = FakeTranscripts()
    provider.lines = [TranscriptLine(i * 1000, i * 1000 + 900, "word " * 4) for i in range(50)]
    stored = len(render_transcript(provider.lines))
    judged = sum(len(f"[00:00:00.000] {line.text}") + 1 for line in provider.lines)
    assert judged < stored
    config = replace(config, note=replace(config.note, max_transcript_chars=(judged + stored) // 2))

    outcome = run(config, ledger, library, client, FakeJudge(), provider=provider, max_rounds=1)

    assert outcome.rounds[0].too_long_to_note == 1
    assert len(library.read_transcript("aaaaaaaaaaa")) == stored


# -- a transcript on disk is judged for this topic, not skipped --------------


def _hold(library, ledger, video_id: str, text: str = "a stored sentence about prompt caching") -> None:
    from marginalia.flow.store import store_transcript

    ledger.mark_pending(video_id)
    store_transcript(
        library,
        ledger,
        video_id,
        replace(transcript(), lines=[TranscriptLine(5000, 9000, text)]),
        provider="ytdlp",
    )


def test_a_held_transcript_never_judged_for_this_topic_is_judged_from_disk(
    config, ledger, library, client, searches
) -> None:
    """It used to be skipped as "already on disk", so it never reached stage 2
    for this topic, never counted as coverage, and the planner could name as a
    gap a video the library was holding."""
    _hold(library, ledger, "aaaaaaaaaaa")
    searches["*"] = [result("aaaaaaaaaaa")]
    provider = FakeTranscripts()
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")})

    outcome = run(config, ledger, library, client, judge, provider=provider, max_rounds=1)

    assert provider.fetched == [], "read from disk, not fetched again"
    assert judge.opening_calls == ["aaaaaaaaaaa"]
    assert "[00:00:05.000] a stored sentence about prompt caching" in judge.transcripts_seen[0]
    assert "-->" not in judge.transcripts_seen[0], "the same shape a fresh fetch is shown in"
    round_one = outcome.rounds[0]
    assert (round_one.already_held, round_one.judged_from_disk) == (1, 1)
    assert [video.video_id for video in round_one.kept] == ["aaaaaaaaaaa"]
    assert outcome.stored_ids == [] and round_one.stored == 0, "nothing was downloaded"


def test_a_held_transcript_already_judged_for_this_topic_is_skipped(
    config, ledger, library, client, searches
) -> None:
    _hold(library, ledger, "aaaaaaaaaaa")
    ledger.log_decision("aaaaaaaaaaa", TOPIC, stage=STAGE_FULL, verdict=VERDICT_YES, reason="read")
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")})

    outcome = run(config, ledger, library, client, judge, max_rounds=1)

    assert judge.opening_calls == []
    assert (outcome.rounds[0].already_held, outcome.rounds[0].judged_from_disk) == (1, 0)


def test_a_transcript_stored_before_a_refusal_is_judged_on_the_next_run(
    config, ledger, library, client, searches
) -> None:
    """Storing before stage 2 keeps the bytes when the judge refuses. Without
    judging from disk, the next run found them held and skipped stage 2 for the
    topic for good."""
    from marginalia.llm import LlmBillingError

    searches["*"] = [result("aaaaaaaaaaa")]
    refusing = FakeJudge()

    def refuse(*_args, **_kwargs):
        raise LlmBillingError("no balance")

    refusing.judge_full = refuse
    with pytest.raises(LlmBillingError):
        run(config, ledger, library, client, refusing, max_rounds=1)

    provider = FakeTranscripts()
    judge = FakeJudge()
    run(config, ledger, library, client, judge, provider=provider, max_rounds=1)

    assert provider.fetched == []
    assert judge.opening_calls == ["aaaaaaaaaaa"]
    assert STAGE_FULL in {d.stage for d in ledger.decisions_for("aaaaaaaaaaa")}


def test_a_query_is_stamped_before_the_decisions_its_search_wrote(
    config, ledger, library, client, searches, monkeypatch
) -> None:
    """The export attributes a decision to the round whose queries had started.
    The queries row was stamped after its search returned, and the search writes
    its gate decisions as it goes — so whenever a second rolled over in between,
    a round's first decisions sorted before the round and were filed under the
    one before it."""
    ticks = iter(range(10_000))
    monkeypatch.setattr(
        "marginalia.core.ledger._utcnow",
        lambda: f"2026-01-01T00:{next(ticks) // 60:02d}:{next(ticks) % 60:02d}+00:00",
    )
    searches["*"] = [result("aaaaaaaaaaa"), result("bbbbbbbbbbb")]

    outcome = run(config, ledger, library, client, FakeJudge(), max_rounds=1)

    query = ledger.queries_for_run(outcome.run_id)[0]
    gate = [d for d in ledger.decisions_for_run(outcome.run_id) if d.stage == STAGE_SEARCH]
    assert gate and all(query.ran_at <= decision.decided_at for decision in gate)


# -- what the planner may count as coverage ------------------------------------
#
# The planner's KEPT SO FAR list is what it judges coverage against, and a video
# on it reads as "this part of the brief is satisfied". Two kinds of keeper were
# landing there that satisfy nothing: a transcript past the note ceiling (the
# counter was incremented and the flag never reached the planner), and a video
# stage 2 never gave a verdict (a failed call kept as a plain YES).


def _sections_shown(planner, title: str) -> list[str]:
    """Which of the planner's kept headings ``title`` was rendered under."""
    from marginalia.brain.planner import render_round

    rendered = render_round(planner.seen[-1], kept_so_far=planner.kept_so_far_seen[-1])
    cut = {
        "KEPT SO FAR": rendered.split("KEPT SO FAR IN THIS RUN")[1].split("KEPT BUT NOT ANSWERABLE")[0],
        "NOT ANSWERABLE": rendered.split("KEPT BUT NOT ANSWERABLE")[1]
        .split("KEPT BUT NOT JUDGED")[0]
        .split("REJECTED (")[0],
    }
    return [name for name, body in cut.items() if title in body]


class _RealStageTwo(FakeJudge):
    """Stage 1 scripted, stage 2 the real judge over a transport that raises."""

    def __init__(self, failure, **kwargs) -> None:
        super().__init__(**kwargs)
        from marginalia.brain.judge import Judge

        def transport(**_):
            raise failure

        self._real = Judge(LlmClient(make_config(Path(".")).llm, transport=transport, sleep=lambda _: None))

    def judge_full(self, brief, candidate, **kwargs):
        self.opening_calls.append(candidate.video_id)
        return self._real.judge_full(brief, candidate, **kwargs)


class _Overflow(Exception):
    status_code = 400

    def __str__(self) -> str:
        return "This model's maximum context length is 8192 tokens"


def test_a_video_too_long_to_note_is_not_coverage_for_the_planner(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    config = replace(config, note=replace(config.note, max_transcript_chars=10))
    planner = FakePlanner()

    outcome = run(config, ledger, library, client, FakeJudge(), planner=planner, max_rounds=2)

    assert outcome.rounds[0].too_long_to_note == 1
    kept = planner.kept_so_far_seen[-1]
    assert [video.too_long for _, video in kept] == [True]
    assert _sections_shown(planner, result("aaaaaaaaaaa").title) == ["NOT ANSWERABLE"]


@pytest.mark.parametrize(
    "failure", [ConnectionError("reset"), _Overflow()], ids=["call-failed", "context-window"]
)
def test_a_stage_two_call_with_no_verdict_is_not_coverage(
    failure, config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    planner = FakePlanner()

    outcome = run(config, ledger, library, client, _RealStageTwo(failure), planner=planner, max_rounds=2)

    assert [video.video_id for video in outcome.rounds[0].kept] == ["aaaaaaaaaaa"], "still kept"
    assert library.has_transcript("aaaaaaaaaaa"), "and its transcript stored"
    assert _sections_shown(planner, result("aaaaaaaaaaa").title) == []
    full = [d for d in ledger.decisions_for("aaaaaaaaaaa") if d.stage == STAGE_FULL]
    assert [d.verdict for d in full] == [VERDICT_MAYBE], "recorded as no verdict, not as a YES"


def test_an_unjudged_keeper_is_listed_apart_for_the_planner(
    config, ledger, library, client, searches
) -> None:
    from marginalia.brain.planner import render_round

    searches["*"] = [result("aaaaaaaaaaa")]
    planner = FakePlanner()

    outcome = run(config, ledger, library, client, _RealStageTwo(ConnectionError("x")), planner=planner, max_rounds=2)

    rendered = render_round(planner.seen[-1], kept_so_far=planner.kept_so_far_seen[-1])
    assert "KEPT BUT NOT JUDGED (1)" in rendered
    assert outcome.rounds[0].unjudged == 1


def test_a_failed_judgement_is_retried_by_the_next_run_on_the_topic(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    run(config, ledger, library, client, _RealStageTwo(ConnectionError("reset")), max_rounds=1)

    provider = FakeTranscripts()
    judge = FakeJudge()
    outcome = run(config, ledger, library, client, judge, provider=provider, max_rounds=1)

    assert judge.opening_calls == ["aaaaaaaaaaa"], "judged again, from disk"
    assert provider.fetched == []
    assert [video.video_id for video in outcome.rounds[0].kept] == ["aaaaaaaaaaa"]


def test_a_legacy_failed_yes_row_does_not_block_rejudging(
    config, ledger, library, client, searches
) -> None:
    """Rows written before unjudged calls were recorded as MAYBE are plain YES
    rows carrying the failure's reason. They must not count as a verdict."""
    _hold(library, ledger, "aaaaaaaaaaa")
    ledger.log_decision(
        "aaaaaaaaaaa", TOPIC, stage=STAGE_FULL, verdict=VERDICT_YES,
        reason="stage-2 judge failed, kept",
    )
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge()

    run(config, ledger, library, client, judge, max_rounds=1)

    assert judge.opening_calls == ["aaaaaaaaaaa"]


def test_a_transcript_too_large_for_the_model_is_not_retried_by_the_same_model(
    config, ledger, library, client, searches
) -> None:
    """The same request to the same model is refused the same way; retrying it
    every run would spend a call to learn nothing."""
    searches["*"] = [result("aaaaaaaaaaa")]
    run(config, ledger, library, client, _RealStageTwo(_Overflow()), max_rounds=1)

    judge = FakeJudge()
    run(config, ledger, library, client, judge, max_rounds=1)

    assert judge.opening_calls == []


def test_a_transcript_left_by_an_interrupted_store_is_reconciled_when_judged_from_disk(
    config, ledger, library, client, searches
) -> None:
    """The file landed and the row stayed `pending`. Judged from disk, it was
    counted as coverage while nothing would ever note a `pending` video."""
    ledger.mark_pending("aaaaaaaaaaa")
    library.write_transcript("aaaaaaaaaaa", [TranscriptLine(5000, 9000, "a stored sentence")])
    before = library.transcript_path("aaaaaaaaaaa").read_bytes()
    searches["*"] = [result("aaaaaaaaaaa")]

    run(config, ledger, library, client, FakeJudge(), max_rounds=1)

    row = ledger.get("aaaaaaaaaaa")
    assert row.status == STATUS_HAS_TRANSCRIPT
    assert row.content_hash == library.transcript_digest("aaaaaaaaaaa")
    assert library.transcript_path("aaaaaaaaaaa").read_bytes() == before, "bytes untouched"


# -- decision outcomes are written, not inferred ------------------------------


def test_every_judgement_row_a_run_writes_carries_its_outcome(
    config, ledger, library, client, searches
) -> None:
    """No later reader may have to infer whether a judge decided something.
    Search rows are the gates' bookkeeping and carry none."""
    searches["*"] = [result("aaaaaaaaaaa"), result("bbbbbbbbbbb"), result("ccccccccccc")]
    judge = FakeJudge(
        metadata={"ccccccccccc": (VERDICT_NO, "a cooking video")},
        openings={"bbbbbbbbbbb": (VERDICT_NO, "off topic", [])},
    )

    run(config, ledger, library, client, judge, max_rounds=1)

    rows = [d for v in ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc") for d in ledger.decisions_for(v)]
    judged = [d for d in rows if d.stage != STAGE_SEARCH]
    assert judged and all(d.outcome == "judged" for d in judged)
    assert all(d.outcome is None for d in rows if d.stage == STAGE_SEARCH)


def test_a_no_on_unreadable_evidence_is_stored_as_maybe_with_the_judges_word(
    config, ledger, library, client, searches
) -> None:
    """It was stored as `yes` — the ledger said the judge accepted a video it
    had refused. Now: MAYBE (kept, not a rejection, not coverage), DEGRADED (no
    note), and `said` keeps the judge's NO."""
    from marginalia.brain.judge import parse_full

    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge()
    reply = "\n".join([
        "VERDICT: NO",
        "EVIDENCE: DEGRADED",
        "REASON: the captions are garbled machine translation throughout",
        "RELEVANT: UNAVAILABLE",
        "UNEXPLAINED: NONE",
    ])
    judge.judge_full = lambda brief, candidate, **_: parse_full(reply, candidate.video_id)

    outcome = run(config, ledger, library, client, judge, max_rounds=1)

    (row,) = [d for d in ledger.decisions_for("aaaaaaaaaaa") if d.stage == STAGE_FULL]
    assert (row.verdict, row.said, row.evidence, row.outcome) == ("maybe", "no", "DEGRADED", "judged")
    assert outcome.rounds[0].degraded == 1
    assert outcome.rounds[0].rejected == [] or all(v.stage != STAGE_FULL for v in outcome.rounds[0].rejected)
    assert ledger.topic_rejection("aaaaaaaaaaa", TOPIC) is None, "not a rejection for the topic"


# -- held transcripts are read by stage 2, never re-screened by stage 1 -------


def test_a_held_transcript_is_judged_from_disk_without_a_stage_one_call(
    config, ledger, library, client, searches
) -> None:
    """R1 probe P1. A stored video whose stage-2 call failed is retried by the
    next run — but it went through stage 1 first, and a title-only NO then
    stopped stage 2 from reading the bytes on disk: in that run, and through
    Gate 2 in every later one. Stage 1 decides whether to fetch; held bytes
    need no fetch."""
    _hold(library, ledger, "aaaaaaaaaaa")
    ledger.log_decision(
        "aaaaaaaaaaa", TOPIC, stage=STAGE_FULL, verdict=VERDICT_MAYBE,
        reason="stage-2 judge failed, kept", outcome="call_failed",
    )
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_NO, "the title looks off-topic")})

    outcome = run(config, ledger, library, client, judge, max_rounds=1)

    assert judge.opening_calls == ["aaaaaaaaaaa"], "stage 2 read it from disk"
    assert judge.batch_sizes == [], "and stage 1 was never asked about it"
    stages = [d.stage for d in ledger.decisions_for("aaaaaaaaaaa") if d.stage != STAGE_SEARCH]
    assert stages == [STAGE_FULL, STAGE_FULL]
    assert outcome.rounds[0].judged_from_disk == 1


def test_a_stage_one_no_on_a_held_transcript_does_not_block_stage_two(
    config, ledger, library, client, searches
) -> None:
    """The ledgers the old ordering already wrote: a held video carrying a
    stage-1 NO for the topic. Gate 2 lets it through, and stage 2's reading
    supersedes the title's."""
    _hold(library, ledger, "aaaaaaaaaaa")
    ledger.log_decision(
        "aaaaaaaaaaa", TOPIC, stage=STAGE_METADATA, verdict=VERDICT_NO, reason="title"
    )
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge()

    run(config, ledger, library, client, judge, max_rounds=1)

    assert judge.opening_calls == ["aaaaaaaaaaa"]
    assert ledger.topic_rejection("aaaaaaaaaaa", TOPIC) is None


def test_a_stage_one_no_still_blocks_a_video_that_is_not_held(
    config, ledger, library, client, searches
) -> None:
    """The exemption is about bytes on disk. Without them, a stage-1 NO keeps
    meaning "never fetch" (rule 8's asymmetry is unchanged)."""
    ledger.log_decision(
        "aaaaaaaaaaa", TOPIC, stage=STAGE_METADATA, verdict=VERDICT_NO, reason="title"
    )
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge()
    provider = FakeTranscripts()

    outcome = run(config, ledger, library, client, judge, provider=provider, max_rounds=1)

    assert provider.fetched == [] and judge.opening_calls == []
    assert outcome.rounds[0].rejected_before == 1


def test_a_video_read_from_disk_is_not_screened_again_in_a_later_round(
    config, ledger, library, client, searches
) -> None:
    """R1 probe P2: round 2 re-judged the held keeper at stage 1 and wrote a
    second `metadata` row, which the planner read as a second finding."""
    _hold(library, ledger, "aaaaaaaaaaa")
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(queries=[["q1"], ["q2"]])

    outcome = run(config, ledger, library, client, judge, max_rounds=2)

    assert judge.opening_calls == ["aaaaaaaaaaa"], "read once"
    metadata_rows = [d for d in ledger.decisions_for("aaaaaaaaaaa") if d.stage == STAGE_METADATA]
    assert metadata_rows == []
    assert len(outcome.rounds) == 2 and outcome.rounds[1].known_this_run == 1


def test_three_long_transcripts_in_a_row_do_not_end_the_run(
    config, ledger, library, searches
) -> None:
    """R1 probe P4. Each stage-2 overflow is about that one transcript, so the
    client's "every request is too large" streak is reset per video, as the
    note drain does. It used to end the run as a config error after the third,
    with advice about settings discovery does not use."""
    from marginalia.brain.judge import Judge
    from conftest import make_llm_config

    shared = LlmClient(
        make_llm_config(),
        transport=lambda **_: (_ for _ in ()).throw(_Overflow()),
        sleep=lambda _: None,
    )
    ids = ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd"]
    searches["*"] = [result(video_id) for video_id in ids]

    class Overflowing(FakeJudge):
        real = Judge(shared)

        def judge_full(self, brief, candidate, **kwargs):
            self.opening_calls.append(candidate.video_id)
            return self.real.judge_full(brief, candidate, **kwargs)

    judge = Overflowing()
    outcome = run(config, ledger, library, shared, judge, max_rounds=1)

    assert judge.opening_calls == ids, "every video reached stage 2"
    assert outcome.rounds[0].unjudged == 4
    assert outcome.stopped_because != "incomplete"


# -- acquisition through discovery ----------------------------------------------


def test_a_captionless_video_is_counted_as_a_route_not_a_failure(
    config, ledger, library, client, searches
) -> None:
    """R1 probe P3: `needs_asr, failed=1, no_captions=0`. The planner read a
    round with captionless videos as a round of fetch failures."""
    searches["*"] = [result("aaaaaaaaaaa")]
    provider = FakeTranscripts(missing=("aaaaaaaaaaa",))

    outcome = run(config, ledger, library, client, FakeJudge(), provider=provider, max_rounds=1)

    assert (outcome.rounds[0].no_captions, outcome.rounds[0].failed) == (1, 0)


def test_a_video_already_recorded_as_gone_is_not_screened_or_fetched(
    config, ledger, library, client, searches
) -> None:
    """Ingest has always honoured a provider's permanent claim; discovery
    screened, fetched and counted a fresh failure every round."""
    from marginalia.core.ledger import FAIL_PERMANENT

    ledger.mark_pending("aaaaaaaaaaa")
    ledger.mark_failed("aaaaaaaaaaa", FAIL_PERMANENT)
    searches["*"] = [result("aaaaaaaaaaa")]
    provider = FakeTranscripts()
    judge = FakeJudge()

    outcome = run(config, ledger, library, client, judge, provider=provider, max_rounds=1)

    assert provider.fetched == [] and judge.batch_sizes == []
    assert (outcome.rounds[0].unavailable, outcome.rounds[0].failed) == (1, 0)


def test_discovery_records_the_provider_that_answered(
    config, ledger, library, client, searches
) -> None:
    """It recorded `transcript_order[0]` whichever provider fetched."""
    searches["*"] = [result("aaaaaaaaaaa")]

    run(config, ledger, library, client, FakeJudge(), max_rounds=1)

    assert ledger.get("aaaaaaaaaaa").provider == FakeTranscripts.name
    assert library.read_meta("aaaaaaaaaaa")["provider"] == FakeTranscripts.name


# -- coverage agrees with the note worker -----------------------------------------


def test_a_keeper_under_the_note_floor_is_not_coverage(
    config, ledger, library, client, searches
) -> None:
    """R1 probe P9: a 48-character transcript against a 1,200 floor counted as
    coverage, and `marg note` then refused it for good."""
    searches["*"] = [result("aaaaaaaaaaa")]
    config = replace(config, note=replace(config.note, min_transcript_chars=1_200))
    planner = FakePlanner()

    outcome = run(config, ledger, library, client, FakeJudge(), planner=planner, max_rounds=2)

    assert outcome.rounds[0].not_noteable == 1
    (_, video), = planner.kept_so_far_seen[-1]
    assert video.answerable is False and video.covers is False
    assert "floor" in video.unnoteable


def test_a_keeper_degraded_for_another_topic_is_not_coverage(
    config, ledger, library, client, searches
) -> None:
    """The note worker refuses a transcript any stage 2 called degraded (one
    transcript, every topic); discovery counted it as coverage for this one."""
    _hold(library, ledger, "aaaaaaaaaaa")
    ledger.log_decision(
        "aaaaaaaaaaa", "another topic", stage=STAGE_FULL, verdict=VERDICT_MAYBE,
        evidence=EVIDENCE_DEGRADED, reason="captions garbled", outcome="judged",
    )
    searches["*"] = [result("aaaaaaaaaaa")]
    planner = FakePlanner()

    outcome = run(config, ledger, library, client, FakeJudge(), planner=planner, max_rounds=2)

    (_, video), = planner.kept_so_far_seen[-1]
    assert video.covers is False and "degraded" in video.unnoteable
    assert outcome.rounds[0].not_noteable == 1


# -- what the planner is told about the round -------------------------------------


def test_the_gate_counters_count_videos_not_search_hits(
    config, ledger, library, client, searches
) -> None:
    """R1 probe P6: one video returned by three queries was three candidates."""
    ledger.mark_pending("aaaaaaaaaaa")
    ledger.mark_has_transcript("aaaaaaaaaaa", source="captions", provider="ytdlp")
    ledger.mark_noted("aaaaaaaaaaa", version="1", model="m")
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(queries=[["q1", "q2", "q3"]])

    outcome = run(config, ledger, library, client, judge, max_rounds=1)

    assert (outcome.rounds[0].candidates, outcome.rounds[0].known) == (1, 1)


def test_the_planner_is_shown_the_filters_that_ran(config, ledger, library, client, monkeypatch) -> None:
    """R1 F12: after a fall-through to a provider that cannot filter, the planner
    was told `order=date` had been applied to a search that was plain."""
    from marginalia.contracts import SearchFilters

    def plain_provider(name, **_):
        class P:
            name = "plain"

            def search(self, query, limit=25, filters=None):
                return [result("aaaaaaaaaaa")]

            def quota_remaining(self):
                return None

            def applied_filters(self):
                return SearchFilters()

        return P()

    monkeypatch.setattr("marginalia.flow.search.get_search_provider", plain_provider)
    planner = FakePlanner()
    territory = TerritoryMap(
        topic=TOPIC,
        version="1",
        concepts=["caching"],
        vocabulary=["kv cache"],
        queries=[
            PlannedQuery(text="q-dated", origin=ORIGIN_TERRITORY, filters=SearchFilters(order="date"))
        ],
    )

    run(
        config, ledger, library, client, FakeJudge(),
        mapper=FakeMapper(territory), planner=planner, max_rounds=2,
    )

    shown = {query.text: query.filters.as_dict() for query, _ in planner.searched_seen[0]}
    assert shown["q-dated"] == {}


def test_stage_one_stays_inside_the_request_budget(
    config, ledger, library, client, searches
) -> None:
    """R1 F17: a round admitted with one request of headroom ran every stage-1
    batch past `max_requests`, and the NOs it wrote are permanent."""
    from marginalia.contracts import Usage

    searches["*"] = [result(f"aaaaaaaaa{n:02d}") for n in range(25)]
    client.usage = Usage(requests=8)
    judge = FakeJudge()

    outcome = run(config, ledger, library, client, judge, max_requests=10, max_rounds=1)

    # Two requests of headroom pay for two batches: 20 of the 25 candidates.
    metadata_rows = [
        d for n in range(25) for d in ledger.decisions_for(f"aaaaaaaaa{n:02d}") if d.stage == "metadata"
    ]
    assert len(metadata_rows) == 2 * config.llm.judge_batch_size
    assert outcome.stopped_because == "max_requests"


def test_a_run_where_no_transcript_fits_stops_as_a_context_window_problem(
    config, ledger, library, searches
) -> None:
    """Independent review F2. Forgetting overflows per video (so three long videos
    do not end a run) removed the stop for a configuration in which *nothing*
    fits, which stored every candidate as `too_large`, unread. Five in a row with
    nothing fitting between them stops the run under its own reason."""
    from marginalia.brain.judge import Judge
    from conftest import make_llm_config

    shared = LlmClient(
        make_llm_config(),
        transport=lambda **_: (_ for _ in ()).throw(_Overflow()),
        sleep=lambda _: None,
    )
    ids = [f"{chr(97 + n) * 11}" for n in range(7)]
    searches["*"] = [result(video_id) for video_id in ids]
    provider = FakeTranscripts()

    class Overflowing(FakeJudge):
        real = Judge(shared)

        def judge_full(self, brief, candidate, **kwargs):
            self.opening_calls.append(candidate.video_id)
            return self.real.judge_full(brief, candidate, **kwargs)

    judge = Overflowing()
    outcome = run(config, ledger, library, shared, judge, provider=provider, max_rounds=1)

    assert outcome.stopped_because == "context_window"
    assert len(judge.opening_calls) == 5 and len(provider.fetched) == 5


def test_gate_two_reads_held_from_the_disk_not_the_row(config, ledger, library, client, searches) -> None:
    """Independent review F8: an interrupted store leaves bytes and a `pending`
    row; Gate 2 read the row, so a stage-1 NO still blocked bytes the
    partition treats as held."""
    ledger.mark_pending("aaaaaaaaaaa")
    library.write_transcript("aaaaaaaaaaa", [TranscriptLine(5000, 9000, "a stored sentence")])
    ledger.log_decision("aaaaaaaaaaa", TOPIC, stage=STAGE_METADATA, verdict=VERDICT_NO, reason="title")
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge()

    run(config, ledger, library, client, judge, max_rounds=1)

    assert judge.opening_calls == ["aaaaaaaaaaa"]


def test_a_held_video_already_judged_is_not_counted_again_next_round(
    config, ledger, library, client, searches
) -> None:
    """Independent review F9: skipped but not carried on the run, so it passed
    Gate 1 and was counted as a candidate again in every round."""
    _hold(library, ledger, "aaaaaaaaaaa")
    ledger.log_decision(
        "aaaaaaaaaaa", TOPIC, stage=STAGE_FULL, verdict=VERDICT_YES, reason="good", outcome="judged"
    )
    searches["*"] = [result("aaaaaaaaaaa")]

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["q1"], ["q2"]]), max_rounds=2)

    assert outcome.rounds[0].already_held == 1
    assert (outcome.rounds[1].already_held, outcome.rounds[1].known_this_run) == (0, 1)


# -- the overflow stop at exactly its threshold (second independent review, F) ----
#
# The stop was checked before the *next* candidate, so a fifth overflow with no
# candidate after it in the round never stopped anything: five candidates gave
# five overflows and `max_rounds`, and the run went on to search again.


def _overflowing_judge(fits=()):
    """A judge whose stage-2 call overflows the window, except for `fits`."""
    from marginalia.brain.judge import Judge
    from conftest import make_llm_config

    shared = LlmClient(
        make_llm_config(),
        transport=lambda **_: (_ for _ in ()).throw(_Overflow()),
        sleep=lambda _: None,
    )

    class Overflowing(FakeJudge):
        real = Judge(shared)

        def judge_full(self, brief, candidate, **kwargs):
            if candidate.video_id in fits:
                return super().judge_full(brief, candidate, **kwargs)
            self.opening_calls.append(candidate.video_id)
            return self.real.judge_full(brief, candidate, **kwargs)

    return shared, Overflowing


def _ids(count: int, first: int = 0) -> list[str]:
    return [chr(97 + n) * 11 for n in range(first, first + count)]


@pytest.mark.parametrize("count", [4, 5, 6])
def test_the_overflow_stop_fires_when_the_fifth_is_reached(
    config, ledger, library, searches, count: int
) -> None:
    shared, Overflowing = _overflowing_judge()
    ids = _ids(count)
    searches["q1"] = [result(video_id) for video_id in ids]
    judge = Overflowing(queries=[["q1"], ["q2"], ["q3"]])
    provider = FakeTranscripts()

    outcome = run(config, ledger, library, shared, judge, provider=provider, max_rounds=3)

    judged = min(count, 5)
    assert len(judge.opening_calls) == judged and len(provider.fetched) == judged
    if count < 5:
        assert outcome.stopped_because != "context_window"
        return
    assert outcome.stopped_because == "context_window"
    assert len(outcome.rounds) == 1 and "q2" not in searches["calls"]
    # The fifth is persisted and accounted for like the four before it.
    assert outcome.rounds[0].unjudged == 5 and outcome.rounds[0].stored == 5
    for video_id in ids[:5]:
        (row,) = [d for d in ledger.decisions_for(video_id) if d.stage == STAGE_FULL]
        assert row.outcome == "too_large"
        assert library.has_transcript(video_id)


def test_the_overflow_streak_reaches_its_stop_across_rounds(config, ledger, library, searches) -> None:
    """Three in round one and two in round two are five in a row: the run stops
    at the fifth, and round three is never searched."""
    shared, Overflowing = _overflowing_judge()
    searches["q1"] = [result(video_id) for video_id in _ids(3)]
    searches["q2"] = [result(video_id) for video_id in _ids(2, first=3)]
    searches["q3"] = [result(video_id) for video_id in _ids(1, first=5)]
    judge = Overflowing(queries=[["q1"], ["q2"], ["q3"]])

    outcome = run(config, ledger, library, shared, judge, max_rounds=3)

    assert outcome.stopped_because == "context_window"
    assert len(judge.opening_calls) == 5 and len(outcome.rounds) == 2
    assert "q3" not in searches["calls"]


def test_a_transcript_that_fits_resets_the_overflow_streak(config, ledger, library, searches) -> None:
    """Four overflows, one that fits, four more: never five in a row."""
    ids = _ids(9)
    shared, Overflowing = _overflowing_judge(fits={ids[4]})
    searches["q1"] = [result(video_id) for video_id in ids]
    judge = Overflowing()

    outcome = run(config, ledger, library, shared, judge, max_rounds=1)

    assert judge.opening_calls == ids
    assert outcome.stopped_because != "context_window"
