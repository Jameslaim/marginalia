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

import pytest
from conftest import make_config

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
    Usage,
)
from marginalia.flow.discover import (
    STOP_DRY_RUN,
    STOP_MAX_REQUESTS,
    STOP_MAX_ROUNDS,
    STOP_MAX_VIDEOS,
    STOP_NO_NEW_VIDEOS,
    discover,
)
from marginalia.core.ledger import (
    STAGE_FULL,
    STAGE_METADATA,
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

    def prompt(self, name):
        return FakePrompt()

    def write_brief(self, topic: str) -> Brief:
        return BRIEF

    def expand_queries(self, brief, *, count=9, concepts=()):
        self.query_calls.append((len(self.query_calls) + 1, tuple(concepts)))
        return self._queries.pop(0) if self._queries else []

    def judge_metadata(self, brief, candidates, *, batch_size):
        self.batch_sizes.append(batch_size)
        return [
            MetadataVerdict(
                c.video_id, *self._metadata.get(c.video_id, (VERDICT_YES, "kept"))
            )
            for c in candidates
        ]

    def judge_full(self, brief, candidate, *, transcript, chapters=(), duration_s=None):
        self.opening_calls.append(candidate.video_id)
        self.transcripts_seen.append(transcript)
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
    return make_config(tmp_path, search_order=("fake",))


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

            def search(self, query, limit=25):
                calls.append(query)
                return list(table.get(query, table.get("*", [])))

            def quota_remaining(self):
                return None

        return P()

    monkeypatch.setattr("marginalia.flow.search.get_search_provider", fake_provider)
    table["calls"] = calls  # type: ignore[assignment]
    return table


def run(config, ledger, library, client, judge, provider=None, topic=TOPIC, **kwargs):
    return discover(
        config, ledger, library, client, topic,
        judge=judge, provider=provider or FakeTranscripts(), **kwargs,
    )


# -- routing: which verdicts reach stage 2 ---------------------------------


def test_a_maybe_routes_to_stage_two(config, ledger, library, client, searches) -> None:
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_MAYBE, "unclear")})

    run(config, ledger, library, client, judge, max_rounds=1)

    assert judge.opening_calls == ["aaaaaaaaaaa"]


def test_a_yes_does_not_pay_for_stage_two(config, ledger, library, client, searches) -> None:
    """Stage 2 is one request per video. A candidate already judged relevant
    does not need re-litigating."""
    searches["*"] = [result("aaaaaaaaaaa")]
    judge = FakeJudge(metadata={"aaaaaaaaaaa": (VERDICT_YES, "clear")})

    run(config, ledger, library, client, judge, max_rounds=1)

    assert judge.opening_calls == []


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
        def judge_metadata(self, brief, candidates, *, batch_size):
            seen.append(bool(ledger.list_runs()))
            return super().judge_metadata(brief, candidates, batch_size=batch_size)

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
        def judge_metadata(self, brief, candidates, *, batch_size):
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
