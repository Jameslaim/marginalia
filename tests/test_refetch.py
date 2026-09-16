"""Retrying the transcript fetches that failed transiently.

**What this command is really doing.** The system already recorded these videos
as retryable — rule 6's write-before-fetch ordering is what produces that state
— and nothing ever read the flag back. 119 accumulated on the real library, 108
of them on a single day in two sustained bursts of throttling.

So the tests below are mostly about *not making it worse*: a loop over 119
videos against a throttling provider is how the backlog was created, and no
outcome here may condemn a video, because a video written off is rule 8's
invisible error with extra steps.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from conftest import make_config
from marginalia.contracts import (
    GONE,
    NO_CAPTIONS,
    RECOVERED,
    STILL_FAILING,
    STOP_THROTTLED,
    TranscriptLine,
    TranscriptResult,
)
from marginalia.core.ledger import Ledger
from marginalia.core.library import Library
from marginalia.flow.refetch import refetch
from marginalia.providers.base import PermanentError, TransientError

A, B, C = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"


class FakeProvider:
    """Scripted per video: a result, an exception, or None for no captions."""

    name = "fake"

    def __init__(self, script: dict) -> None:
        self.script = script
        self.asked: list[str] = []

    def fetch(self, video_id: str):
        self.asked.append(video_id)
        item = self.script.get(video_id, TransientError("throttled"))
        if isinstance(item, Exception):
            raise item
        return item


def _result(video_id: str) -> TranscriptResult:
    return TranscriptResult(
        lines=[TranscriptLine(start_ms=0, end_ms=2000, text="hello")],
        language="en",
        source="captions",
        title=f"title {video_id}",
        channel="a channel",
        duration_s=60,
        upload_date="20260101",
        caption_kind="auto",
    )


def build(tmp_path: Path, failed, wanted=()):
    config = make_config(tmp_path)
    library = Library(config.library_path)
    library.ensure()
    ledger = Ledger(config.library_path / "ledger.db")
    ledger.start_run("run1", topic="t", brief="b", brief_version="1", model="m")
    for video_id in failed:
        ledger.mark_pending(video_id)
        ledger.mark_failed(video_id, "transient", provider="fake")
        if video_id in wanted:
            ledger.log_decision(video_id, "t", stage="full", verdict="yes",
                                reason="wanted", run_id="run1")
    return config, ledger, library


def run(config, ledger, library, provider, **kwargs):
    import marginalia.flow.refetch as module

    original = module.get_provider
    module.get_provider = lambda _name: provider
    try:
        return refetch(config, ledger, library, sleep=lambda _: None, **kwargs)
    finally:
        module.get_provider = original


def test_a_dry_run_lists_wanted_first_and_touches_nothing(tmp_path):
    config, ledger, library = build(tmp_path, [A, B, C], wanted=[C])
    provider = FakeProvider({})

    outcome = run(config, ledger, library, provider, dry_run=True)

    assert [r.video_id for r in outcome.results][0] == C, "judged YES goes first"
    assert outcome.results[0].wanted
    assert provider.asked == [], "no network call"
    assert ledger.get(A).attempts == 1, "and no attempt was spent"


def test_a_recovered_video_holds_a_transcript_and_awaits_a_note(tmp_path):
    config, ledger, library = build(tmp_path, [A])
    provider = FakeProvider({A: _result(A)})

    outcome = run(config, ledger, library, provider)

    assert outcome.count(RECOVERED) == 1
    row = ledger.get(A)
    assert row.status == "has_transcript"
    assert row.noted_at is None, "recovered, not yet understood"
    assert library.transcript_path(A).exists()
    assert library.read_meta(A)["title"] == f"title {A}"


def test_a_recovered_video_carries_the_hash_of_its_file(tmp_path):
    """Rule 25: the retry path stores a transcript through its own `_store`,
    and a hash written by `discover.py` alone would leave every recovered
    video looking like the old unhashed normal to `marg doctor`."""
    config, ledger, library = build(tmp_path, [A])
    provider = FakeProvider({A: _result(A)})

    run(config, ledger, library, provider)

    row = ledger.get(A)
    assert row.content_hash is not None
    assert row.content_hash == library.transcript_digest(A)


def test_a_throttle_stops_the_run_and_leaves_everything_retryable(
    tmp_path, caplog: pytest.LogCaptureFixture
):
    """A loop over 119 videos against a throttling provider is how the backlog
    was created. Three in a row is the signature of the condition rather than
    bad luck about one video, and under rule 24 a provider refusing a burst is
    being authoritative rather than flaky."""
    ids = [f"aaaaaaaaaa{n}" for n in range(1, 7)]
    config, ledger, library = build(tmp_path, ids)
    provider = FakeProvider({})

    with caplog.at_level(logging.WARNING, logger="marginalia.flow.refetch"):
        outcome = run(config, ledger, library, provider)

    assert outcome.stopped_because == STOP_THROTTLED
    assert len(provider.asked) == 3, "stopped at the streak, did not walk the rest"
    assert "still retryable" in caplog.text
    for video_id in ids:
        row = ledger.get(video_id)
        assert row.status == "failed" and row.fail_kind == "transient"


def test_a_success_resets_the_streak(tmp_path):
    ids = [f"aaaaaaaaaa{n}" for n in range(1, 7)]
    config, ledger, library = build(tmp_path, ids)
    provider = FakeProvider({ids[2]: _result(ids[2])})

    outcome = run(config, ledger, library, provider)

    assert outcome.count(RECOVERED) == 1
    assert outcome.stopped_because == STOP_THROTTLED
    assert len(provider.asked) == 6, "the success in the middle bought three more tries"


def test_a_video_that_fails_again_stays_transient_with_a_higher_count(tmp_path):
    """No threshold promotes `transient` to `permanent`. An attempt count cannot
    tell a deleted video from an unlucky one, so it is reported and never acted
    on."""
    config, ledger, library = build(tmp_path, [A])
    before = ledger.get(A).attempts

    outcome = run(config, ledger, library, FakeProvider({}))

    row = ledger.get(A)
    assert outcome.results[0].outcome == STILL_FAILING
    assert row.fail_kind == "transient", "still retryable"
    assert row.attempts == before + 1
    assert outcome.results[0].attempts == before + 1


def test_only_the_provider_may_call_a_video_gone(tmp_path):
    """`permanent` is the provider's claim — deleted, private, region-locked —
    and never an inference this command draws from a count."""
    config, ledger, library = build(tmp_path, [A])

    outcome = run(config, ledger, library, FakeProvider({A: PermanentError("deleted")}))

    assert outcome.results[0].outcome == GONE
    assert ledger.get(A).fail_kind == "permanent"


def test_no_captions_is_a_route_not_a_failure(tmp_path):
    """The fetch worked. The video has no caption track, which is M4's queue."""
    config, ledger, library = build(tmp_path, [A])

    outcome = run(config, ledger, library, FakeProvider({A: None}))

    assert outcome.results[0].outcome == NO_CAPTIONS
    assert ledger.get(A).status == "needs_asr"


def test_a_limit_stops_early_without_a_throttle(tmp_path):
    config, ledger, library = build(tmp_path, [A, B, C])
    provider = FakeProvider({A: _result(A), B: _result(B), C: _result(C)})

    outcome = run(config, ledger, library, provider, limit=2)

    assert len(provider.asked) == 2
    assert outcome.considered == 2
    assert outcome.stopped_because != STOP_THROTTLED


def test_permanent_failures_are_never_retried(tmp_path):
    """This list can only shrink by videos coming back. Re-asking about one the
    provider called gone is how a rule-8 error gets made twice."""
    config, ledger, library = build(tmp_path, [A])
    ledger.mark_failed(A, "permanent", provider="fake")
    provider = FakeProvider({})

    outcome = run(config, ledger, library, provider)

    assert outcome.considered == 0
    assert provider.asked == []
