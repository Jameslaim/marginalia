"""`marg list` and `marg status` — the two commands that surface asset state.

Neither had a test before B1, which is how the status column got as far as a
vocabulary change without anything objecting. They are the user-facing edge of
the status/relevance split, so they are where the split is visible or isn't.

Both print a report a skill reads. The contract is deliberately narrow: **the
first whitespace-separated token is the state**, and nothing else is promised —
not column alignment, not field count. `has-transcript` is five characters wider
than the column `stored` used to fit in, and that is fine precisely because
alignment was never the contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from marginalia.cli import EXIT_OK, EXIT_USAGE, main
from marginalia.contracts import (
    STATUS_DUPLICATE,
    STATUS_FAILED,
    STATUS_HAS_TRANSCRIPT,
    STATUS_NEEDS_ASR,
    STATUS_SEEN,
    VIDEO_STATUSES,
)
from marginalia.core.ledger import FAIL_PERMANENT, Ledger

A = "aaaaaaaaaaa"
B = "bbbbbbbbbbb"
C = "ccccccccccc"
D = "ddddddddddd"


@pytest.fixture()
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "library"
    config = tmp_path / "config.toml"
    config.write_text(
        f'[library]\npath = "{root.as_posix()}"\n\n'
        '[llm]\nbase_url = "https://example.invalid/v1"\nmodel = "test-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MARGINALIA_CONFIG", str(config))
    return root


@pytest.fixture()
def populated(library: Path) -> Path:
    """One video in each of the states B1 cares about most."""
    library.mkdir(parents=True, exist_ok=True)
    with Ledger(library / "ledger.db") as ledger:
        ledger.mark_pending(A)
        ledger.mark_has_transcript(A, source="captions", provider="ytdlp")

        # Fetched for some brief, declined for that brief. Holding nothing.
        ledger.mark_pending(B)
        ledger.mark_seen(B)

        ledger.mark_pending(C)
        ledger.mark_needs_asr(C)

        ledger.mark_pending(D)
        ledger.mark_failed(D, FAIL_PERMANENT)
    return library


# -- marg list ---------------------------------------------------------------


def test_list_defaults_to_videos_holding_a_transcript(populated: Path, capsys) -> None:
    assert main(["list"]) == EXIT_OK
    lines = capsys.readouterr().out.splitlines()

    assert len(lines) == 1
    assert lines[0].split()[:2] == ["has-transcript", A]


def test_list_shows_the_new_vocabulary(populated: Path, capsys) -> None:
    """`stored` and `rejected` are gone from what a user sees."""
    assert main(["list", "--status", STATUS_SEEN]) == EXIT_OK
    assert capsys.readouterr().out.split()[:2] == ["seen", B]


def test_a_declined_video_is_listed_as_seen_not_rejected(populated: Path, capsys) -> None:
    """The point of the split, at the surface. B was fetched and declined for
    some brief. It is not globally `rejected` — that judgement was about one
    topic and lives in `decisions`. All the row says is that nothing is held."""
    main(["list", "--status", STATUS_SEEN])
    out = capsys.readouterr().out

    assert B in out
    assert "rejected" not in out


@pytest.mark.parametrize(
    "status,video",
    [
        (STATUS_HAS_TRANSCRIPT, A),
        (STATUS_SEEN, B),
        (STATUS_NEEDS_ASR, C),
        (STATUS_FAILED, D),
    ],
)
def test_each_state_is_listable(populated: Path, capsys, status: str, video: str) -> None:
    assert main(["list", "--status", status]) == EXIT_OK
    assert capsys.readouterr().out.split()[1] == video


def test_the_first_token_is_always_the_state(populated: Path, capsys) -> None:
    """The one property a parser is allowed to depend on — the same contract
    `marg search` keeps. `has-transcript` overflows the padding column and that
    changes nothing here, which is why the overflow was left alone."""
    printable = {status.replace("_", "-") for status in VIDEO_STATUSES}
    for status in (STATUS_HAS_TRANSCRIPT, STATUS_SEEN, STATUS_NEEDS_ASR, STATUS_FAILED):
        main(["list", "--status", status])
        for line in capsys.readouterr().out.splitlines():
            assert line.split()[0] in printable


def test_an_empty_result_prints_no_lines(populated: Path, capsys) -> None:
    """A skill parses this, so nothing found must be zero lines rather than a
    sentence it has to tell apart from a result."""
    assert main(["list", "--status", STATUS_DUPLICATE]) == EXIT_OK
    assert capsys.readouterr().out == ""


# -- marg status -------------------------------------------------------------


def test_status_reports_the_new_vocabulary(populated: Path, capsys) -> None:
    assert main(["status", A]) == EXIT_OK
    assert capsys.readouterr().out.split()[:2] == ["has-transcript", A]


def test_status_of_a_declined_video_is_seen(populated: Path, capsys) -> None:
    assert main(["status", B]) == EXIT_OK
    assert capsys.readouterr().out.split()[0] == "seen"


def test_status_of_an_unknown_video_says_so(library: Path, capsys) -> None:
    assert main(["status", "zzzzzzzzzzz"]) == EXIT_OK
    assert capsys.readouterr().out.split()[0] == "unknown"


def test_status_rejects_something_that_is_not_a_video(library: Path) -> None:
    assert main(["status", "not a video"]) == EXIT_USAGE
