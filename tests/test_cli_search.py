"""The shape of what `marg search` prints.

A skill parses this, so the format is a contract, not a presentation choice.
The two things that must hold: the verdict is always the first whitespace-
delimited token, and `--json` carries everything the text form does plus the
fields M2b needs and the text form deliberately omits.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from marginalia.cli import EXIT_OK, EXIT_USAGE, _duration, main
from marginalia.contracts import VERDICT_NO
from marginalia.core.ledger import STAGE_METADATA, Ledger
from marginalia.providers.base import SearchResult

A = "dQw4w9WgXcQ"
B = "jNQXAC9IVRw"
C = "aB_cD-eFgH1"


class FakeProvider:
    name = "fake_search"

    def __init__(self, results: list[SearchResult]) -> None:
        self._results = results

    def search(self, query: str, limit: int = 25, filters=None) -> list[SearchResult]:
        return self._results

    def quota_remaining(self) -> int | None:
        return None

    def units_spent(self) -> int:
        return 0


def result(video_id: str, title: str, duration_s: int | None, channel: str) -> SearchResult:
    return SearchResult(
        video_id=video_id,
        title=title,
        channel=channel,
        description="A description the terse output must never print.",
        duration_s=duration_s,
        published_at="2026-03-14T09:00:07Z",
        view_count=148_213,
        provider="fake_search",
    )


RESULTS = [
    result(A, "Prompt caching explained", 1080, "Some Channel"),
    result(B, "Agent cost control", 2520, "Another Channel"),
    result(C, "A video judged before", 300, "Third Channel"),
]


@pytest.fixture()
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "library"
    config = tmp_path / "config.toml"
    config.write_text(
        f'[library]\npath = "{root.as_posix()}"\n\n'
        '[providers]\nsearch_order = ["fake_search"]\n\n'
        '[llm]\nbase_url = "https://example.invalid/v1"\nmodel = "test-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MARGINALIA_CONFIG", str(config))
    monkeypatch.setattr(
        "marginalia.flow.search.get_search_provider", lambda name, **_: FakeProvider(RESULTS)
    )
    yield root


@pytest.fixture()
def gated(library: Path) -> Path:
    """One of each verdict: A new, B already in the library, C declined for the topic.

    B needs a *note*, not just a transcript: since B1 the library is what has
    notes, and Gate 1 asks about membership rather than about bytes on disk.
    """
    with Ledger(library / "ledger.db") as ledger:
        ledger.mark_pending(B)
        ledger.mark_has_transcript(B, source="captions", provider="ytdlp")
        ledger.mark_noted(B, version="1", model="m")
        ledger.log_decision(
            C, "prompt caching", stage=STAGE_METADATA, verdict=VERDICT_NO, reason="off topic"
        )
    return library


# -- the terse form ---------------------------------------------------------


def test_one_line_per_candidate_then_a_summary(gated: Path, capsys) -> None:
    assert main(["search", "prompt caching"]) == EXIT_OK

    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 4
    assert [line.split()[0] for line in lines[:3]] == ["new", "known", "rejected"]
    assert lines[3] == "3 found, 1 known, 1 previously rejected, 1 new — fake_search"


def test_a_new_candidate_carries_title_duration_and_channel(gated: Path, capsys) -> None:
    main(["search", "prompt caching"])
    line = capsys.readouterr().out.splitlines()[0]

    assert line.startswith(f"new       {A}")
    assert '"Prompt caching explained"' in line
    assert " 18m " in line
    assert line.rstrip().endswith("Some Channel")


def test_a_dropped_candidate_carries_its_reason(gated: Path, capsys) -> None:
    main(["search", "prompt caching"])
    known, rejected = capsys.readouterr().out.splitlines()[1:3]

    assert known == f"known     {B}  already in the library"
    assert rejected.startswith(f"rejected  {C}  no for this topic (20")


def test_the_verdict_is_always_the_first_token(gated: Path, capsys) -> None:
    """The one property a parser is allowed to depend on."""
    main(["search", "prompt caching"])
    for line in capsys.readouterr().out.splitlines()[:3]:
        assert line.split()[0] in {"new", "known", "rejected"}


def test_descriptions_stay_out_of_the_terse_output(gated: Path, capsys) -> None:
    """Cheap metadata is still context nobody asked for. `--json` is where the
    description goes, because M2b consumes that deliberately."""
    main(["search", "prompt caching"])
    assert "description the terse output" not in capsys.readouterr().out


def test_an_empty_library_finds_everything_new(library: Path, capsys) -> None:
    main(["search", "prompt caching"])
    out = capsys.readouterr().out
    assert out.strip().endswith("3 found, 0 known, 0 previously rejected, 3 new — fake_search")


def test_the_topic_flag_changes_which_rejections_apply(gated: Path, capsys) -> None:
    main(["search", "prompt caching", "--topic", "something else"])
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines[:3]] == ["new", "known", "new"]


def test_a_bad_query_is_a_usage_error(library: Path, monkeypatch) -> None:
    from marginalia.providers.base import PermanentError

    class Broken(FakeProvider):
        def search(self, query: str, limit: int = 25, filters=None):
            raise PermanentError("empty query")

    monkeypatch.setattr("marginalia.flow.search.get_search_provider", lambda name, **_: Broken([]))
    assert main(["search", "   "]) == EXIT_USAGE


# -- the structured form ----------------------------------------------------


def test_json_is_valid_and_complete(gated: Path, capsys) -> None:
    assert main(["search", "prompt caching", "--json"]) == EXIT_OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["query"] == "prompt caching"
    assert payload["topic"] == "prompt caching"
    assert payload["provider"] == "fake_search"
    assert payload["fallbacks"] == []
    assert payload["counts"] == {"found": 3, "known": 1, "rejected": 1, "stored_this_run": 0, "new": 1}
    assert len(payload["candidates"]) == 3


def test_json_carries_every_search_result_field(gated: Path, capsys) -> None:
    """M2b judges on this and nothing else, so a field missing here is a field
    the judge silently never sees."""
    main(["search", "prompt caching", "--json"])
    first = json.loads(capsys.readouterr().out)["candidates"][0]

    assert first == {
        "video_id": A,
        "verdict": "new",
        "reason": "new candidate",
        "title": "Prompt caching explained",
        "channel": "Some Channel",
        "description": "A description the terse output must never print.",
        "duration_s": 1080,
        "published_at": "2026-03-14T09:00:07Z",
        "view_count": 148_213,
        "provider": "fake_search",
    }


def test_json_and_text_agree_on_every_verdict(gated: Path, capsys) -> None:
    main(["search", "prompt caching"])
    text = [line.split()[0] for line in capsys.readouterr().out.splitlines()[:3]]
    main(["search", "prompt caching", "--json"])
    structured = [c["verdict"] for c in json.loads(capsys.readouterr().out)["candidates"]]

    assert text == structured


def test_json_emits_nothing_else(gated: Path, capsys) -> None:
    """It has to be parseable straight off stdout."""
    main(["search", "prompt caching", "--json"])
    json.loads(capsys.readouterr().out)  # raises if a report line leaked in


# -- duration formatting ----------------------------------------------------


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (None, "?"),
        (0, "?"),
        (-5, "?"),
        (45, "45s"),
        (60, "1m"),
        (1112, "18m"),
        (3599, "59m"),
        (3600, "1h00m"),
        (6129, "1h42m"),
        (36000, "10h00m"),
    ],
)
def test_durations_are_compact_and_fixed_vocabulary(seconds, expected: str) -> None:
    assert _duration(seconds) == expected
