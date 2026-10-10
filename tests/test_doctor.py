"""`marg doctor`: the library on disk, held against the ledger.

Rule 2 cannot be enforced from inside a program, so what it gets is detection.
These tests pin the seven checks, and — more important than any of them — the
two shapes that are *correct* and must never be reported: a decision with no
`videos` row (rule 11) and a `needs_asr` row with no directory (rule 6). A
doctor that cried wolf on either would be worse than none, because its reader
would learn to skip the output.

Rule 1 is tested here directly: the transcript and note fixtures carry phrases
that must not appear in anything the command prints, in either mode.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from marginalia.cli import EXIT_FAILED, EXIT_OK, main
from marginalia.contracts import (
    CHECK_NOTE_INVALID,
    CHECK_NOTE_MISSING,
    CHECK_NOTE_UNRECORDED,
    CHECK_RUN_UNFINISHED,
    CHECK_TRANSCRIPT_MISSING,
    CHECK_TRANSCRIPT_MODIFIED,
    CHECK_UNTRACKED_DIR,
    DOCTOR_CHECKS,
    DOCTOR_PROBLEMS,
    STATUS_HAS_TRANSCRIPT,
    STATUS_NEEDS_ASR,
    VERDICT_NO,
    TranscriptLine,
    VideoRow,
)
from marginalia.core.ledger import Ledger, RunRow
from marginalia.core.library import Library
from marginalia.enrich.note import REQUIRED_SECTIONS
from marginalia.flow.doctor import check_directories, check_runs, diagnose

A = "aaaaaaaaaaa"
B = "bbbbbbbbbbb"
C = "ccccccccccc"
D = "ddddddddddd"

# Phrases that must never reach stdout or stderr. Distinctive on purpose.
TRANSCRIPT_SECRET = "the transcript said porcupine ledger seventeen"
NOTE_SECRET = "the note claims marmalade quadratic"
GOOD_NOTE = "\n".join(
    f"## {section}\n{NOTE_SECRET} under {section.lower()}\n" for section in REQUIRED_SECTIONS
)
BAD_NOTE = f"## Claims\n{NOTE_SECRET}\n## Leaves out\n"  # most sections missing


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    library = tmp_path / "library"
    config = tmp_path / "config.toml"
    config.write_text(
        f'[library]\npath = "{library.as_posix()}"\n\n'
        '[llm]\nbase_url = "https://example.invalid/v1"\nmodel = "test-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MARGINALIA_CONFIG", str(config))
    return library


def _store(library: Library, ledger: Ledger, video_id: str, *, hash_it: bool = False) -> None:
    """A video the CLI would have stored: row first, then files, then status."""
    ledger.mark_pending(video_id)
    library.write_transcript(video_id, [TranscriptLine(0, 1000, TRANSCRIPT_SECRET)])
    library.write_meta(video_id, {"title": "t"})
    ledger.mark_has_transcript(
        video_id,
        source="captions",
        provider="ytdlp",
        content_hash=library.transcript_digest(video_id) if hash_it else None,
    )


def _note(library: Library, ledger: Ledger, video_id: str, text: str = GOOD_NOTE) -> None:
    library.write_note(video_id, text)
    ledger.mark_noted(video_id, version="v1", model="m")


def _run(*argv: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(["doctor", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _snapshot(root: Path) -> set[Path]:
    return {p.relative_to(root) for p in root.rglob("*")} if root.exists() else set()


# -- clean, empty, and the two correct shapes ----------------------------------


def test_empty_library_is_clean_and_leaves_no_footprint(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real library right now: `ledger.db` and an empty `videos/`. Exit 0,
    one line, and — the part that matters — nothing created: no `logs/`, no
    new ledger, nothing."""
    root.mkdir()
    (root / "videos").mkdir()
    with Ledger(root / "ledger.db"):
        pass
    before = _snapshot(root)

    code, out, _ = _run(capsys=capsys)

    assert code == EXIT_OK
    assert out.strip().startswith("doctor:   clean")
    assert len(out.strip().splitlines()) == 1
    assert _snapshot(root) == before


def test_library_with_no_ledger_is_not_given_one(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root.mkdir()
    code, _, _ = _run(capsys=capsys)
    assert code == EXIT_OK
    assert not (root / "ledger.db").exists()
    assert not (root / "logs").exists()


def test_clean_library_reports_nothing(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    library = Library(root)
    library.ensure()
    with Ledger(library.ledger_path) as ledger:
        _store(library, ledger, A, hash_it=True)
        _note(library, ledger, A)
        ledger.start_run("run1", "topic", brief="b")
        ledger.finish_run(
            "run1",
            rounds=1,
            candidates_seen=0,
            videos_stored=1,
            requests_made=0,
            input_tokens=0,
            output_tokens=0,
            stopped_because="done",
        )
    code, out, _ = _run(capsys=capsys)
    assert code == EXIT_OK
    assert "clean" in out
    assert "1 video(s), 1 dir(s), 1 run(s)" in out


def test_a_decision_with_no_videos_row_is_not_reported(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Trap 1, and the most important test here. Stage 1 declines a candidate
    without fetching it: `decisions` gets a row, `videos` does not, and that is
    rule 11 working. 231 of these were once reported as a failure."""
    library = Library(root)
    library.ensure()
    with Ledger(library.ledger_path) as ledger:
        ledger.start_run("run1", "topic", brief="b")
        for video_id in (A, B, C):
            ledger.log_decision(
                video_id, "topic", stage="metadata", verdict=VERDICT_NO, run_id="run1"
            )
        ledger.finish_run(
            "run1",
            rounds=1,
            candidates_seen=3,
            videos_stored=0,
            requests_made=1,
            input_tokens=0,
            output_tokens=0,
            stopped_because="done",
        )
    code, out, _ = _run(capsys=capsys)
    assert code == EXIT_OK, out
    assert "clean" in out
    assert "0 video(s)" in out


def test_a_needs_asr_row_with_no_directory_is_not_reported(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Trap 2. Rule 6 writes the row before the fetch; no captions means a row
    and no files. `pending` and `failed` are the same shape."""
    library = Library(root)
    library.ensure()
    with Ledger(library.ledger_path) as ledger:
        ledger.mark_pending(A)
        ledger.mark_needs_asr(A, provider="ytdlp")
        ledger.mark_pending(B)  # still pending: a fetch in flight or crashed
        ledger.mark_pending(C)
        ledger.mark_failed(C, "transient", provider="ytdlp")
    code, out, _ = _run(capsys=capsys)
    assert code == EXIT_OK, out
    assert "clean" in out
    assert "3 video(s), 0 dir(s)" in out


# -- the seven checks -----------------------------------------------------------


def test_check_1_untracked_directory(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    library = Library(root)
    library.ensure()
    with Ledger(library.ledger_path):
        pass
    # Written without the CLI: no row anywhere.
    library.write_transcript(D, [TranscriptLine(0, 1, TRANSCRIPT_SECRET)])
    (library.videos_dir / "not-an-id").mkdir()  # ignored: cannot be a video

    code, out, _ = _run(capsys=capsys)
    assert code == EXIT_FAILED
    assert CHECK_UNTRACKED_DIR in out
    assert f"  {D}" in out
    assert "not-an-id" not in out


def test_check_2_status_claims_a_transcript_that_is_gone(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = Library(root)
    library.ensure()
    with Ledger(library.ledger_path) as ledger:
        _store(library, ledger, A)
    library.transcript_path(A).unlink()
    code, out, _ = _run(capsys=capsys)
    assert code == EXIT_FAILED
    assert CHECK_TRANSCRIPT_MISSING in out
    assert f"  {A}  ({STATUS_HAS_TRANSCRIPT})" in out


def test_check_3_noted_at_set_but_note_gone(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = Library(root)
    library.ensure()
    with Ledger(library.ledger_path) as ledger:
        _store(library, ledger, A)
        _note(library, ledger, A)
    library.note_path(A).unlink()
    code, out, _ = _run(capsys=capsys)
    assert code == EXIT_FAILED
    assert CHECK_NOTE_MISSING in out
    assert f"  {A}" in out


def test_check_4_note_present_but_never_recorded(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = Library(root)
    library.ensure()
    with Ledger(library.ledger_path) as ledger:
        _store(library, ledger, A)
    library.write_note(A, GOOD_NOTE)  # no mark_noted: written outside the CLI
    code, out, _ = _run(capsys=capsys)
    assert code == EXIT_FAILED
    assert CHECK_NOTE_UNRECORDED in out
    assert CHECK_NOTE_INVALID not in out  # a valid note, only unrecorded
    assert "bytes" in out


def test_check_5_stored_note_fails_validation(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = Library(root)
    library.ensure()
    with Ledger(library.ledger_path) as ledger:
        _store(library, ledger, A)
        _note(library, ledger, A, BAD_NOTE)
    code, out, _ = _run(capsys=capsys)
    assert code == EXIT_FAILED
    assert CHECK_NOTE_INVALID in out
    assert "Wrong or shaky" in out  # section names are allowed; content is not
    assert CHECK_NOTE_UNRECORDED not in out


def test_check_6_transcript_modified(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule 3. The one check that reads the transcript's bytes."""
    library = Library(root)
    library.ensure()
    with Ledger(library.ledger_path) as ledger:
        _store(library, ledger, A, hash_it=True)
        _store(library, ledger, B, hash_it=True)
    with library.transcript_path(A).open("a", encoding="utf-8") as handle:
        handle.write("[00:00:01.000 --> 00:00:02.000] appended\n")
    code, out, _ = _run(capsys=capsys)
    assert code == EXIT_FAILED
    assert CHECK_TRANSCRIPT_MODIFIED in out
    assert f"  {A}" in out
    assert f"  {B}" not in out
    # One of the two matched. The other is the finding above, not a verification.
    assert "hashes:   1 verified, 0 unrecorded" in out


def test_check_6_counts_an_unrecorded_hash_rather_than_judging_it(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing writes `content_hash` yet, so today every transcript is in this
    state. Rule 14: unverifiable is neither verified nor a mismatch."""
    library = Library(root)
    library.ensure()
    with Ledger(library.ledger_path) as ledger:
        _store(library, ledger, A)
    code, out, _ = _run("--json", capsys=capsys)
    assert code == EXIT_OK
    data = json.loads(out)
    assert data["clean"] is True
    assert data["hashes_verified"] == 0
    assert data["hashes_unrecorded"] == 1


def test_check_7_unfinished_run_is_reported_and_not_an_error(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = Library(root)
    library.ensure()
    with Ledger(library.ledger_path) as ledger:
        ledger.start_run("deadbeef", "topic", brief="b")
    code, out, _ = _run(capsys=capsys)
    assert code == EXIT_OK
    assert CHECK_RUN_UNFINISHED in out
    assert "  deadbeef" in out
    assert "0 problem(s)" in out
    assert "1 unfinished run(s)" in out


# -- integrity reads are not accesses --------------------------------------------


def test_doctor_never_touches_last_accessed(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Trap 3. The hash check reads bytes and the note check reads headings;
    neither is an access, and stamping every video on every run would erase
    the one signal the future deletion policy depends on."""
    library = Library(root)
    library.ensure()
    with Ledger(library.ledger_path) as ledger:
        _store(library, ledger, A, hash_it=True)
        _note(library, ledger, A)
        assert ledger.get(A).last_accessed is None
    _run(capsys=capsys)
    with Ledger(library.ledger_path) as ledger:
        assert ledger.get(A).last_accessed is None


def test_doctor_writes_nothing_even_when_it_finds_everything(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = Library(root)
    library.ensure()
    with Ledger(library.ledger_path) as ledger:
        _store(library, ledger, A, hash_it=True)
        _note(library, ledger, A, BAD_NOTE)
        ledger.start_run("deadbeef", "topic", brief="b")
    library.write_note(B, GOOD_NOTE)  # untracked dir with an unrecorded note
    library.transcript_path(A).write_text("changed", encoding="utf-8")
    before = {
        p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file() and p.suffix != ".db"
    }

    code, _, _ = _run(capsys=capsys)

    assert code == EXIT_FAILED
    after = {
        p: p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file() and p.suffix != ".db"
    }
    assert after == before
    assert not (root / "logs").exists()


# -- rule 1: nothing leaks --------------------------------------------------------


@pytest.mark.parametrize("mode", ["text", "json"])
def test_no_transcript_or_note_text_reaches_the_output(
    root: Path, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    """Trap 4. Every check that reads a file fires here, and the file contents
    carry phrases that must appear nowhere in what was printed."""
    library = Library(root)
    library.ensure()
    with Ledger(library.ledger_path) as ledger:
        _store(library, ledger, A, hash_it=True)
        _note(library, ledger, A, BAD_NOTE)
        _store(library, ledger, B)
    library.write_note(B, GOOD_NOTE)
    library.write_note(C, BAD_NOTE)  # untracked directory
    library.transcript_path(A).write_text(TRANSCRIPT_SECRET + " edited", encoding="utf-8")

    code, out, err = _run(*(["--json"] if mode == "json" else []), capsys=capsys)

    assert code == EXIT_FAILED
    printed = out + err
    assert TRANSCRIPT_SECRET not in printed
    assert NOTE_SECRET not in printed
    assert "marmalade" not in printed
    assert "porcupine" not in printed


# -- json ----------------------------------------------------------------------


def test_json_is_valid_and_carries_every_check(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    library = Library(root)
    library.ensure()
    with Ledger(library.ledger_path) as ledger:
        _store(library, ledger, A)
    library.transcript_path(A).unlink()

    code, out, _ = _run("--json", capsys=capsys)

    assert code == EXIT_FAILED
    data = json.loads(out)
    assert data["clean"] is False
    assert data["problems"] == 1
    assert set(data["checks"]) == set(DOCTOR_CHECKS)
    assert data["checks"][CHECK_TRANSCRIPT_MISSING] == {
        "count": 1,
        "findings": [{"subject": A, "detail": STATUS_HAS_TRANSCRIPT}],
    }
    others = [c for c in DOCTOR_CHECKS if c != CHECK_TRANSCRIPT_MISSING]
    assert all(data["checks"][c]["count"] == 0 for c in others)


def test_json_on_an_empty_library(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root.mkdir()
    code, out, _ = _run("--json", capsys=capsys)
    assert code == EXIT_OK
    data = json.loads(out)
    assert data["clean"] is True
    assert data["videos_checked"] == data["dirs_checked"] == data["runs_checked"] == 0


# -- the pure halves, and the exit-code split -----------------------------------------


def test_pure_checks_take_rows_and_names() -> None:
    row = VideoRow(
        video_id=A,
        status=STATUS_NEEDS_ASR,
        source=None,
        caption_kind=None,
        transcribed_at=None,
        content_hash=None,
        duplicate_of=None,
        first_seen="t",
        last_updated="t",
        last_accessed=None,
        attempts=1,
        fail_kind=None,
        provider=None,
    )
    found = check_directories([row], [A, B])
    assert [f.subject for f in found] == [B]
    assert found[0].check == CHECK_UNTRACKED_DIR

    run = RunRow(
        run_id="r",
        topic="t",
        brief="b",
        brief_version=None,
        model=None,
        started_at="2026-09-01T00:00:00Z",
        finished_at=None,
        rounds=0,
        candidates_seen=0,
        videos_stored=0,
        requests_made=0,
        input_tokens=0,
        output_tokens=0,
        stopped_because=None,
    )
    assert [f.detail for f in check_runs([run])] == ["2026-09-01"]


def test_every_check_is_its_own_counter() -> None:
    """Rule 14. Eight names, eight counters, and the only one that is not a
    problem is the one rule 6 leaves on purpose."""
    assert len(set(DOCTOR_CHECKS)) == 8
    assert DOCTOR_PROBLEMS == set(DOCTOR_CHECKS) - {CHECK_RUN_UNFINISHED}


def test_diagnose_reads_nothing_when_given_nothing(tmp_path: Path) -> None:
    library = Library(tmp_path / "nowhere")
    report = diagnose(library, [], [])
    assert report.clean
    assert report.findings == ()
    assert not (tmp_path / "nowhere").exists()


def test_doctor_does_not_migrate_an_older_ledger(root: Path, capsys) -> None:
    """`Ledger()` migrates an older schema in place before checking it, so a
    doctor that opened the ledger that way altered the file it diagnosed. It
    opens read-only now, and an older ledger is reported, not repaired."""
    import hashlib
    import sqlite3

    root.mkdir(parents=True)
    path = root / "ledger.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE videos (video_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
        "first_seen TEXT NOT NULL, last_updated TEXT NOT NULL);"
    )
    connection.commit()
    connection.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    code, _out, err = _run(capsys=capsys)

    assert code != 0
    assert "older version of marginalia" in err
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert sorted(p.name for p in root.iterdir()) == ["ledger.db"]


def test_doctor_leaves_a_current_ledger_byte_for_byte(root: Path, capsys) -> None:
    import hashlib

    library = Library(root)
    library.ensure()
    with Ledger(library.ledger_path) as ledger:
        _store(library, ledger, "aaaaaaaaaaa", hash_it=True)
    before = hashlib.sha256(library.ledger_path.read_bytes()).hexdigest()

    code, _out, _err = _run(capsys=capsys)

    assert code == 0
    assert hashlib.sha256(library.ledger_path.read_bytes()).hexdigest() == before
