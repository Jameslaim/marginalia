"""The two gates, the provider chain, and the decision log.

No network. Providers are fakes: the point of `SearchProvider` is that nothing
above it knows or cares which one ran.

The failures guarded against here are the invisible ones — a gate that drops a
video it should have kept, a fallback that fires silently, a decision that was
never written and therefore cannot be diagnosed later.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import make_config

from marginalia.config import Config
from marginalia.contracts import KNOWN, NEW, REJECTED, VERDICT_MAYBE, VERDICT_NO, VERDICT_YES
from marginalia.core.ledger import STAGE_METADATA, STAGE_SEARCH, Ledger
from marginalia.providers.base import (
    PermanentError,
    ProviderUnavailable,
    QuotaExhausted,
    SearchResult,
    TransientError,
)
from marginalia.flow.search import run_search

TOPIC = "prompt caching"

A = "dQw4w9WgXcQ"
B = "jNQXAC9IVRw"
C = "aB_cD-eFgH1"


def result(video_id: str, provider: str = "fake") -> SearchResult:
    return SearchResult(
        video_id=video_id,
        title=f"Video {video_id}",
        channel="Some Channel",
        description="",
        duration_s=1080,
        published_at="2026-01-01T00:00:00Z",
        view_count=1234,
        provider=provider,
    )


class FakeProvider:
    """Returns a canned list, or raises whatever it was handed."""

    def __init__(self, name: str, results=None, raises: Exception | None = None) -> None:
        self.name = name
        self._results = list(results or [])
        self._raises = raises
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, limit: int = 25) -> list[SearchResult]:
        self.calls.append((query, limit))
        if self._raises is not None:
            raise self._raises
        return [replace(item, provider=self.name) for item in self._results]

    def quota_remaining(self) -> int | None:
        return None


@pytest.fixture()
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    with Ledger(tmp_path / "library" / "ledger.db") as instance:
        yield instance


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    return make_config(tmp_path)


@pytest.fixture()
def providers(monkeypatch: pytest.MonkeyPatch) -> dict[str, FakeProvider]:
    """Swap the provider registry. `search.py` must never name a real one."""
    registry: dict[str, FakeProvider] = {}

    def factory(name: str, **_: object):
        try:
            provider = registry[name]
        except KeyError:
            raise KeyError(name) from None
        # An Exception in the registry stands for a provider that cannot even
        # be constructed here — no API key.
        if isinstance(provider, Exception):
            raise provider
        return provider

    monkeypatch.setattr("marginalia.flow.search.get_search_provider", factory)
    return registry


def install(providers: dict, config: Config, **named) -> Config:
    providers.update(named)
    return replace(config, search_order=tuple(named))


# -- Gate 1: is this video already known? -----------------------------------


def test_an_unknown_video_is_new(config, ledger, providers) -> None:
    config = install(providers, config, primary=FakeProvider("primary", [result(A)]))

    run = run_search(config, ledger, "prompt caching")

    assert [(c.video_id, c.verdict) for c in run.candidates] == [(A, NEW)]
    assert run.counts == {"found": 1, KNOWN: 0, REJECTED: 0, NEW: 1}


def test_a_video_in_the_library_is_known(config, ledger, providers) -> None:
    """In the library means *has a note*. Notes are what `/ask` loads, so
    note existence is membership — nothing else is visible to a question."""
    ledger.mark_pending(A)
    ledger.mark_has_transcript(A, source="captions", provider="ytdlp")
    ledger.mark_noted(A, version="1", model="m")
    config = install(providers, config, primary=FakeProvider("primary", [result(A)]))

    run = run_search(config, ledger, "prompt caching")

    assert run.candidates[0].verdict == KNOWN
    assert run.candidates[0].reason == "already in the library"


def test_a_transcript_without_a_note_is_not_known(config, ledger, providers) -> None:
    """The case B1 exists to separate.

    Holding the bytes is not membership. This video contributes nothing to
    any answer until a note is written, so hiding it from every future search
    would strand it: present in the ledger, absent from the library, invisible
    to both. It stays a candidate.

    Not re-fetched, though — `flow/discover.py` checks the disk before the
    network, and `write_transcript` refuses to overwrite regardless."""
    ledger.mark_pending(A)
    ledger.mark_has_transcript(A, source="captions", provider="ytdlp")
    config = install(providers, config, primary=FakeProvider("primary", [result(A)]))

    run = run_search(config, ledger, "prompt caching")

    assert run.candidates[0].verdict == NEW


def test_a_stale_note_is_still_a_note(config, ledger, providers) -> None:
    """`noted_at`, not `note_is_current`. A note written by last month's
    prompt is still loaded by `/ask`, so the video is still in the library.
    Staleness is the note worker's question, not Gate 1's."""
    ledger.mark_pending(A)
    ledger.mark_has_transcript(A, source="captions", provider="ytdlp")
    ledger.mark_noted(A, version="0-ancient", model="m")
    config = install(providers, config, primary=FakeProvider("primary", [result(A)]))

    assert run_search(config, ledger, "prompt caching").candidates[0].verdict == KNOWN


def test_a_duplicate_is_known_and_names_the_canonical_video(config, ledger, providers) -> None:
    ledger.mark_pending(A)
    ledger._conn.execute(
        "UPDATE videos SET status = 'duplicate', duplicate_of = ? WHERE video_id = ?", (B, A)
    )
    ledger._conn.commit()
    config = install(providers, config, primary=FakeProvider("primary", [result(A)]))

    run = run_search(config, ledger, "prompt caching")

    assert run.candidates[0].verdict == KNOWN
    assert run.candidates[0].reason == f"duplicate of {B}"


@pytest.mark.parametrize("status", ["pending", "needs_asr", "failed", "rejected"])
def test_unfinished_work_is_not_known(config, ledger, providers, status: str) -> None:
    """Only `stored` and `duplicate` mean "we have the content". A video that
    failed, or is waiting on ASR, must surface again — dropping it here is a
    silent, permanent hole."""
    ledger.mark_pending(A)
    ledger._conn.execute("UPDATE videos SET status = ? WHERE video_id = ?", (status, A))
    ledger._conn.commit()
    config = install(providers, config, primary=FakeProvider("primary", [result(A)]))

    assert run_search(config, ledger, "prompt caching").candidates[0].verdict == NEW


def test_gate_1_runs_before_gate_2(config, ledger, providers) -> None:
    """A video in the library that was also declined for this topic reports
    `known`. The order has to be stable — a skill reads this column."""
    ledger.mark_pending(A)
    ledger.mark_has_transcript(A, source="captions", provider="ytdlp")
    ledger.mark_noted(A, version="1", model="m")
    ledger.log_decision(A, TOPIC, stage=STAGE_METADATA, verdict=VERDICT_NO, reason="off topic")
    config = install(providers, config, primary=FakeProvider("primary", [result(A)]))

    assert run_search(config, ledger, TOPIC).candidates[0].verdict == KNOWN


# -- Gate 2: was this video judged before, for this topic? ------------------


def test_a_no_for_this_topic_is_dropped(config, ledger, providers) -> None:
    ledger.log_decision(A, TOPIC, stage=STAGE_METADATA, verdict=VERDICT_NO, reason="off topic")
    config = install(providers, config, primary=FakeProvider("primary", [result(A)]))

    run = run_search(config, ledger, TOPIC)

    assert run.candidates[0].verdict == REJECTED
    assert run.candidates[0].reason.startswith("no for this topic (")


def test_a_no_for_a_different_topic_is_kept(config, ledger, providers) -> None:
    """The invisible failure this guards against: a video rejected while
    searching "prompt caching" must not vanish from "agent architecture"."""
    ledger.log_decision(A, "prompt caching", stage=STAGE_METADATA, verdict=VERDICT_NO)
    config = install(providers, config, primary=FakeProvider("primary", [result(A)]))

    assert run_search(config, ledger, "agent architecture").candidates[0].verdict == NEW


def test_gate_2_matches_however_the_topic_was_typed(config, ledger, providers) -> None:
    ledger.log_decision(A, "Prompt  CACHING", stage=STAGE_METADATA, verdict=VERDICT_NO)
    config = install(providers, config, primary=FakeProvider("primary", [result(A)]))

    assert run_search(config, ledger, "  prompt caching ").candidates[0].verdict == REJECTED


def test_gate_2_matching_is_exact_and_nothing_more(config, ledger, providers) -> None:
    """Fuzzy topic similarity is deferred on purpose. A wrong fuzzy match hides
    a video permanently and invisibly; a missed exact match costs one cheap
    re-judge. See PLAN.md §6."""
    ledger.log_decision(A, "prompt caching", stage=STAGE_METADATA, verdict=VERDICT_NO)
    config = install(providers, config, primary=FakeProvider("primary", [result(A)]))

    for near_miss in ("prompt cache", "prompt caching explained", "caching"):
        assert run_search(config, ledger, near_miss).candidates[0].verdict == NEW


@pytest.mark.parametrize("verdict", [VERDICT_YES, VERDICT_MAYBE])
def test_only_a_no_drops_a_video(config, ledger, providers, verdict: str) -> None:
    ledger.log_decision(A, TOPIC, stage=STAGE_METADATA, verdict=verdict)
    config = install(providers, config, primary=FakeProvider("primary", [result(A)]))

    assert run_search(config, ledger, TOPIC).candidates[0].verdict == NEW


def test_a_later_yes_does_not_undo_an_earlier_no(config, ledger, providers) -> None:
    """Gate 2 asks whether a `no` exists for this topic, not what the most
    recent verdict was. Re-surfacing a video the judge already declined would
    make every search pay for it again."""
    ledger.log_decision(A, TOPIC, stage=STAGE_SEARCH, verdict=VERDICT_NO)
    ledger.log_decision(A, TOPIC, stage=STAGE_METADATA, verdict=VERDICT_YES)
    config = install(providers, config, primary=FakeProvider("primary", [result(A)]))

    assert run_search(config, ledger, TOPIC).candidates[0].verdict == REJECTED


def test_the_topic_defaults_to_the_query(config, ledger, providers) -> None:
    config = install(providers, config, primary=FakeProvider("primary", [result(A)]))
    assert run_search(config, ledger, "Prompt Caching").topic == "prompt caching"


def test_an_explicit_topic_overrides_the_query(config, ledger, providers) -> None:
    ledger.log_decision(A, "cost control", stage=STAGE_METADATA, verdict=VERDICT_NO)
    config = install(providers, config, primary=FakeProvider("primary", [result(A)]))

    run = run_search(config, ledger, "how do I cut my agent bill", topic="Cost Control")

    assert run.topic == "cost control"
    assert run.candidates[0].verdict == REJECTED


# -- decision logging -------------------------------------------------------


def test_every_candidate_produces_a_decision_row(config, ledger, providers) -> None:
    """Kept or dropped, at every outcome. The log cannot be reconstructed
    retroactively, and M2b's judge is diagnosable only from what was written."""
    ledger.mark_pending(B)
    ledger.mark_has_transcript(B, source="captions", provider="ytdlp")
    ledger.mark_noted(B, version="1", model="m")
    ledger.log_decision(C, TOPIC, stage=STAGE_METADATA, verdict=VERDICT_NO)
    config = install(
        providers, config, primary=FakeProvider("primary", [result(A), result(B), result(C)])
    )

    run = run_search(config, ledger, TOPIC)

    assert [c.verdict for c in run.candidates] == [NEW, KNOWN, REJECTED]
    for video_id in (A, B, C):
        rows = [
            row
            for row in ledger.decisions_for(video_id)
            if row.stage == STAGE_SEARCH and row.topic == TOPIC
        ]
        assert len(rows) == 1, f"no search-stage decision logged for {video_id}"


def test_a_survivor_is_logged_maybe_not_yes(config, ledger, providers) -> None:
    """This stage decides whether a video is *known*, never whether it is good.
    Logging `yes` here would fake a judgement nobody made."""
    config = install(providers, config, primary=FakeProvider("primary", [result(A)]))

    run_search(config, ledger, TOPIC)

    row = ledger.latest_decision(A, TOPIC)
    assert row is not None
    assert (row.stage, row.verdict) == (STAGE_SEARCH, VERDICT_MAYBE)
    assert row.reason == "new candidate"


@pytest.mark.parametrize(
    "setup,reason",
    [("in_library", "already in the library"), ("declined_before", None)],
)
def test_a_dropped_candidate_is_logged_no_with_its_reason(
    config, ledger, providers, setup: str, reason: str | None
) -> None:
    if setup == "in_library":
        ledger.mark_pending(A)
        ledger.mark_has_transcript(A, source="captions", provider="ytdlp")
        ledger.mark_noted(A, version="1", model="m")
    else:
        ledger.log_decision(A, TOPIC, stage=STAGE_METADATA, verdict=VERDICT_NO)
    config = install(providers, config, primary=FakeProvider("primary", [result(A)]))

    run_search(config, ledger, TOPIC)

    row = ledger.latest_decision(A, TOPIC)
    assert row is not None and row.stage == STAGE_SEARCH and row.verdict == VERDICT_NO
    if reason is not None:
        assert row.reason == reason


def test_the_logged_topic_is_normalised(config, ledger, providers) -> None:
    config = install(providers, config, primary=FakeProvider("primary", [result(A)]))
    run_search(config, ledger, "  Prompt   Caching  ")
    assert ledger.decisions_for(A)[0].topic == "prompt caching"


# -- the provider chain -----------------------------------------------------


def test_the_first_working_provider_wins(config, ledger, providers) -> None:
    overflow = FakeProvider("overflow", [result(B)])
    config = install(
        providers, config, primary=FakeProvider("primary", [result(A)]), overflow=overflow
    )

    run = run_search(config, ledger, TOPIC)

    assert run.provider == "primary"
    assert run.fallbacks == []
    assert overflow.calls == []


def test_quota_exhaustion_falls_through(config, ledger, providers) -> None:
    config = install(
        providers,
        config,
        primary=FakeProvider("primary", raises=QuotaExhausted("quota exhausted: 10000/10000")),
        overflow=FakeProvider("overflow", [result(B)]),
    )

    run = run_search(config, ledger, TOPIC)

    assert run.provider == "overflow"
    assert [c.video_id for c in run.candidates] == [B]
    assert run.fallbacks == [("primary", "quota exhausted: 10000/10000")]


def test_an_unavailable_provider_falls_through(config, ledger, providers) -> None:
    """No API key configured is not the same as a key YouTube rejected. The
    first should reach the overflow provider — while naming itself, so the
    degradation is never silent."""
    config = install(
        providers,
        config,
        primary=ProviderUnavailable("no API key"),
        overflow=FakeProvider("overflow", [result(B)]),
    )

    run = run_search(config, ledger, TOPIC)

    assert run.provider == "overflow"
    assert run.fallbacks == [("primary", "no API key")]


def test_a_transient_failure_falls_through(config, ledger, providers) -> None:
    config = install(
        providers,
        config,
        primary=FakeProvider("primary", raises=TransientError("timeout")),
        overflow=FakeProvider("overflow", [result(B)]),
    )

    run = run_search(config, ledger, TOPIC)

    assert run.provider == "overflow"
    assert run.fallbacks == [("primary", "timeout")]


def test_a_bad_query_does_not_fall_through(config, ledger, providers) -> None:
    """It would fail identically on the second provider. Hiding it behind a
    fallback turns a fixable mistake into a mystery."""
    overflow = FakeProvider("overflow", [result(B)])
    config = install(
        providers,
        config,
        primary=FakeProvider("primary", raises=PermanentError("empty query")),
        overflow=overflow,
    )

    with pytest.raises(PermanentError):
        run_search(config, ledger, "   ")
    assert overflow.calls == []


def test_an_unknown_provider_name_surfaces(config, ledger, providers) -> None:
    """A config error, not a reason to quietly use something else."""
    config = replace(config, search_order=("nonexistent",))
    with pytest.raises(PermanentError) as caught:
        run_search(config, ledger, TOPIC)
    assert "nonexistent" in str(caught.value)


def test_every_provider_failing_is_an_error_not_an_empty_result(
    config, ledger, providers
) -> None:
    """Zero results and "nothing could answer" must not look the same."""
    config = install(
        providers,
        config,
        primary=FakeProvider("primary", raises=QuotaExhausted("out of quota")),
        overflow=FakeProvider("overflow", raises=TransientError("dns failure")),
    )

    with pytest.raises(TransientError) as caught:
        run_search(config, ledger, TOPIC)
    assert "out of quota" in str(caught.value)
    assert "dns failure" in str(caught.value)


def test_the_limit_reaches_the_provider(config, ledger, providers) -> None:
    primary = FakeProvider("primary", [result(A)])
    config = install(providers, config, primary=primary)

    run_search(config, ledger, TOPIC, limit=7)

    assert primary.calls == [(TOPIC, 7)]


# -- result hygiene ---------------------------------------------------------


def test_repeated_ids_are_collapsed(config, ledger, providers) -> None:
    config = install(
        providers, config, primary=FakeProvider("primary", [result(A), result(B), result(A)])
    )

    run = run_search(config, ledger, TOPIC)

    assert [c.video_id for c in run.candidates] == [A, B]
    assert len(ledger.decisions_for(A)) == 1


def test_rank_order_is_preserved(config, ledger, providers) -> None:
    """Search rank is meaning, and M2b judges in this order."""
    config = install(
        providers, config, primary=FakeProvider("primary", [result(C), result(A), result(B)])
    )
    assert [c.video_id for c in run_search(config, ledger, TOPIC).candidates] == [C, A, B]


def test_an_empty_result_set_is_not_an_error(config, ledger, providers) -> None:
    config = install(providers, config, primary=FakeProvider("primary", []))

    run = run_search(config, ledger, TOPIC)

    assert run.candidates == []
    assert run.counts == {"found": 0, KNOWN: 0, REJECTED: 0, NEW: 0}
    assert run.provider == "primary"


def test_the_producing_provider_is_recorded_on_every_result(config, ledger, providers) -> None:
    config = install(providers, config, primary=FakeProvider("primary", [result(A), result(B)]))

    run = run_search(config, ledger, TOPIC)

    assert {c.result.provider for c in run.candidates} == {"primary"}
