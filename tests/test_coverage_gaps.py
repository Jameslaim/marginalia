"""Tests for paths the mutation run showed no test watched.

A partial review run mutated the baseline and re-ran the suite; these mutants
survived every test (scratchpad/reviews/recovery/mutations.py):

* M16 — a failed stage-1 batch rejecting its videos (a rule-8 violation);
* M08/M09 — transcript text logged at DEBUG/INFO during notes or discovery
  (hard rule 1's worst surface, `logs/`);
* M11 — reconcile overwriting an unreadable `meta.json` (now also fixed);
* M10 — `marg refetch` exiting 0 after a throttle stop;
* M12 — export attributing a later round's decisions to an earlier round.

Plus the provider fixes made with them: caption-track fallback and the YouTube
rate-limit classification.
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import pytest

from conftest import make_config, make_llm_config
from marginalia.brain.judge import Judge
from marginalia.contracts import (
    Brief,
    SearchResult,
    STATUS_HAS_TRANSCRIPT,
    TranscriptLine,
    VERDICT_MAYBE,
    VERDICT_NO,
)
from marginalia.core.ledger import Ledger
from marginalia.core.library import Library
from marginalia.llm import LlmClient

SENTINEL = "zebra kumquat sentinel transcript words"
BRIEF = Brief(
    topic="t",
    text="INTENT\nx\nREQUIRED OF EVERY VIDEO\n- x\nPARTS TO COVER\n- x\nEXCLUDED\n- y",
    version="1/1",
)


def _candidates(n: int) -> list[SearchResult]:
    return [
        SearchResult(f"vid{index:08d}", f"title {index}", "chan", "desc", 600, None, None)
        for index in range(n)
    ]


# -- M16: stage 1 never rejects on a failure ------------------------------------


def test_a_failed_stage_one_batch_keeps_every_video_as_maybe() -> None:
    def transport(**_):
        raise ConnectionError("reset")

    judge = Judge(LlmClient(make_llm_config(), transport=transport, sleep=lambda _: None))

    verdicts = judge.judge_metadata(BRIEF, _candidates(3), batch_size=10)

    assert [v.verdict for v in verdicts] == [VERDICT_MAYBE] * 3
    assert {v.outcome for v in verdicts} == {"call_failed"}


def test_a_position_missing_from_the_reply_is_maybe_not_no() -> None:
    class Reply:
        def __init__(self, text):
            message = type("M", (), {"content": text})()
            self.choices = [type("C", (), {"message": message, "finish_reason": "stop"})()]
            self.usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1})()
            self.model = "m"

    judge = Judge(
        LlmClient(make_llm_config(), transport=lambda **_: Reply("1: NO - off topic"), sleep=lambda _: None)
    )

    verdicts = judge.judge_metadata(BRIEF, _candidates(2), batch_size=10)

    assert [v.verdict for v in verdicts] == [VERDICT_NO, VERDICT_MAYBE]
    assert verdicts[1].outcome == "no_verdict"


# -- M08/M09: transcript text never reaches a log record, at any level ----------


def test_no_transcript_text_is_logged_by_a_discovery_run(tmp_path, caplog, monkeypatch) -> None:
    from conftest import make_note_config
    from marginalia.contracts import TranscriptResult
    from marginalia.flow.discover import discover

    import test_discover as td

    config = make_config(tmp_path, search_order=("fake",), note=make_note_config(min_transcript_chars=0))
    library = Library(config.library_path)
    library.ensure()

    class Provider:
        name = "fake"

        def fetch(self, video_id):
            return TranscriptResult(
                lines=[TranscriptLine(0, 1000, SENTINEL)], language="en", source="captions",
                title="t", channel="c", duration_s=60, upload_date=None,
            )

    def fake_search_provider(name, **_):
        class P:
            name = "fake"

            def search(self, query, limit=25, filters=None):
                return [td.result("aaaaaaaaaaa")]

            def quota_remaining(self):
                return None

        return P()

    monkeypatch.setattr("marginalia.flow.search.get_search_provider", fake_search_provider)
    caplog.set_level(logging.DEBUG)
    with Ledger(library.ledger_path) as ledger:
        discover(
            config, ledger, library, LlmClient(make_llm_config(), transport=lambda **_: None), "t",
            judge=td.FakeJudge(), mapper=td.FakeMapper(), planner=td.FakePlanner(), provider=Provider(),
        )

    assert library.has_transcript("aaaaaaaaaaa"), "the run did read and store it"
    assert all(SENTINEL not in record.getMessage() for record in caplog.records)
    assert caplog.records, "the capture saw the run's records"


def test_no_transcript_text_is_logged_by_the_note_drain(tmp_path, caplog) -> None:
    from marginalia.enrich.note import write_notes

    import test_note as tn

    config, library, ledger = tn.library_with(tmp_path, "aaaaaaaaaa1", text=(SENTINEL + " ") * 60)
    caplog.set_level(logging.DEBUG)

    write_notes(config, ledger, library, tn.client(tn.GOOD_NOTE), prompts_directory=tn.prompts(tmp_path))

    assert ledger.get("aaaaaaaaaa1").noted_at
    assert all(SENTINEL not in record.getMessage() for record in caplog.records)


# -- M11: reconcile never overwrites a meta.json it cannot read -----------------


def test_reconcile_leaves_an_unreadable_meta_json_untouched(tmp_path) -> None:
    from marginalia.flow.store import reconcile_stored

    library = Library(tmp_path / "library")
    library.ensure()
    with Ledger(library.ledger_path) as ledger:
        ledger.mark_pending("aaaaaaaaaaa")
        library.write_transcript("aaaaaaaaaaa", [TranscriptLine(0, 1000, "x")])
        library.meta_path("aaaaaaaaaaa").write_text('{"title": "Hand-kept title", ', encoding="utf-8")
        before = library.meta_path("aaaaaaaaaaa").read_bytes()

        assert reconcile_stored(library, ledger, "aaaaaaaaaaa") is True

        assert library.meta_path("aaaaaaaaaaa").read_bytes() == before
        assert ledger.get("aaaaaaaaaaa").status == STATUS_HAS_TRANSCRIPT


# -- M10: a throttled refetch exits non-zero --------------------------------------


def test_refetch_exits_non_zero_when_throttled(tmp_path, monkeypatch) -> None:
    from marginalia.cli import EXIT_FAILED, main
    from marginalia.contracts import STOP_THROTTLED, RefetchRun

    config = tmp_path / "config.toml"
    config.write_text(
        f'[library]\npath = "{(tmp_path / "library").as_posix()}"\n'
        '[llm]\nbase_url = "https://example.invalid/v1"\nmodel = "test-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MARGINALIA_CONFIG", str(config))
    monkeypatch.setattr(
        "marginalia.cli.refetch", lambda *a, **k: RefetchRun(considered=3, stopped_because=STOP_THROTTLED)
    )

    assert main(["refetch"]) == EXIT_FAILED


# -- M12: export attributes a decision to the round it happened in --------------


def test_export_does_not_pull_a_later_rounds_decisions_into_an_earlier_round() -> None:
    from marginalia.core.ledger import DecisionRow
    from marginalia.flow.export import _decisions_in

    def at(when: str) -> DecisionRow:
        return DecisionRow("aaaaaaaaaaa", "t", "metadata", "yes", "r", when)

    boundaries = {1: "2026-01-01T00:00:00+00:00", 2: "2026-01-01T01:00:00+00:00"}
    round_one, round_two = at("2026-01-01T00:30:00+00:00"), at("2026-01-01T01:30:00+00:00")

    assert _decisions_in([round_one, round_two], boundaries, 1) == [round_one]
    assert _decisions_in([round_one, round_two], boundaries, 2) == [round_two]


# -- caption tracks (R1 F2) ---------------------------------------------------------


def _fake_yt_dlp(info: dict, bodies: dict[str, bytes]) -> types.ModuleType:
    class YoutubeDL:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            return info

        def urlopen(self, url):
            module.opened.append(url)
            return types.SimpleNamespace(read=lambda: bodies[url])

    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = YoutubeDL
    module.opened = []
    return module


VTT = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhello from the auto track\n"


def test_a_chat_replay_is_never_chosen_as_a_caption_track(monkeypatch) -> None:
    from marginalia.providers.ytdlp import YtDlpProvider

    info = {
        "language": "en",
        "subtitles": {"live_chat": [{"url": "chat", "ext": "json"}]},
        "automatic_captions": {"en": [{"url": "auto", "ext": "vtt"}]},
        "title": "t", "duration": 60,
    }
    fake = _fake_yt_dlp(info, {"auto": VTT, "chat": b"{}"})
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)

    result = YtDlpProvider().fetch("aaaaaaaaaaa")

    assert result is not None and result.caption_kind == "auto"
    # Not merely survived by the zero-line fallback: never downloaded at all.
    assert fake.opened == ["auto"]


def test_a_manual_track_that_parses_to_nothing_falls_back_to_auto(monkeypatch) -> None:
    from marginalia.providers.ytdlp import YtDlpProvider

    info = {
        "language": "en",
        "subtitles": {"en": [{"url": "manual", "ext": "vtt"}]},
        "automatic_captions": {"en": [{"url": "auto", "ext": "vtt"}]},
        "title": "t", "duration": 60,
    }
    monkeypatch.setitem(
        sys.modules, "yt_dlp", _fake_yt_dlp(info, {"manual": b"WEBVTT\n\n", "auto": VTT})
    )

    result = YtDlpProvider().fetch("aaaaaaaaaaa")

    assert result is not None and result.caption_kind == "auto"
    assert [line.text for line in result.lines] == ["hello from the auto track"]


# -- YouTube API rate limits (R1 F8) ----------------------------------------------


@pytest.mark.parametrize(
    ("reason", "message", "expected"),
    [
        ("userRateLimitExceeded", "rate", "TransientError"),
        ("rateLimitExceeded", "Quota exceeded for quota metric 'Queries' and limit 'Queries per minute per user'", "TransientError"),
        ("rateLimitExceeded", "Quota exceeded for quota metric 'Search Queries' and limit 'Search Queries per day'", "QuotaExhausted"),
        ("quotaExceeded", "The request cannot be completed", "QuotaExhausted"),
    ],
)
def test_a_rate_limit_is_not_read_as_the_daily_quota(reason, message, expected) -> None:
    import json

    from marginalia.providers.youtube_api import HttpStatusError, _classify

    body = json.dumps({"error": {"message": message, "errors": [{"reason": reason}]}}).encode()
    assert type(_classify(HttpStatusError(403, body))).__name__ == expected
