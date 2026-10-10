"""`content_hash`: rule 3 made checkable.

The column sat unwritten from M1 until `marg doctor` arrived, which meant check
6 compared nothing and *never modify a stored transcript* was unverifiable on
real data. Now every store path records `Library.transcript_digest` of the file
as it landed, and this file pins the three things that make that worth having:

- the hash is of the **file**, read back, not of the lines that were passed in;
- `marg ingest` — the third store path after `discover.py` and `refetch.py`,
  which have their own tests beside their own store tests — writes it too
  (rule 25: a guard traced to every caller that shares the failure);
- doctor treats a freshly stored transcript as *verified*, a modified one as
  `transcript_modified`, and one with no recorded hash as *unrecorded* — never
  as a fault, because a transcript restored from the archive arrives that way.

No test here prints transcript text; the fixture line is a sentinel that the
doctor tests assert never reaches the output.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import marginalia.cli as cli
from marginalia.cli import EXIT_FAILED, EXIT_OK, main
from marginalia.contracts import (
    CHECK_TRANSCRIPT_MODIFIED,
    STATUS_HAS_TRANSCRIPT,
    TranscriptLine,
    TranscriptResult,
)
from marginalia.core.ledger import Ledger
from marginalia.core.library import Library

A = "aaaaaaaaaaa"
B = "bbbbbbbbbbb"
SENTINEL = "the transcript said porcupine ledger seventeen"


class FakeProvider:
    name = "fake"

    def __init__(self, result: TranscriptResult) -> None:
        self.result = result

    def fetch(self, video_id: str) -> TranscriptResult:
        return self.result


def _result() -> TranscriptResult:
    return TranscriptResult(
        lines=[TranscriptLine(start_ms=0, end_ms=2000, text=SENTINEL)],
        language="en",
        source="captions",
        title="a title",
        channel="a channel",
        duration_s=60,
        upload_date="20260101",
        caption_kind="auto",
    )


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


@pytest.fixture()
def fake_provider(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider(_result())
    # Ingest resolves its providers through the shared acquisition path now.
    monkeypatch.setattr("marginalia.flow.acquire.get_provider", lambda _name: provider)
    return provider


# -- the ledger contract ----------------------------------------------------------


def test_a_stored_transcript_carries_a_hash_equal_to_its_digest(
    root: Path, fake_provider: FakeProvider, capsys: pytest.CaptureFixture[str]
) -> None:
    """`marg ingest`, the single-video path. The third caller of
    `mark_has_transcript`, and the one with no test until now."""
    code = main(["ingest", f"https://www.youtube.com/watch?v={A}"])
    out = capsys.readouterr().out

    assert code == EXIT_OK, out
    assert out.startswith("has-transcript")
    library = Library(root)
    with Ledger(library.ledger_path) as ledger:
        row = ledger.get(A)
    assert row.status == STATUS_HAS_TRANSCRIPT
    assert row.content_hash is not None
    assert row.content_hash == library.transcript_digest(A)
    assert len(row.content_hash) == 64


def test_the_hash_is_of_the_file_not_of_the_lines(
    root: Path, fake_provider: FakeProvider, capsys: pytest.CaptureFixture[str]
) -> None:
    """Hashing the in-memory lines would record what was meant to be written.
    Hashing the file records what landed — the only thing doctor can compare
    against, and the only version that sees a truncated write."""
    import hashlib

    main(["ingest", A])
    library = Library(root)
    with Ledger(library.ledger_path) as ledger:
        recorded = ledger.get(A).content_hash
    on_disk = hashlib.sha256(library.transcript_path(A).read_bytes()).hexdigest()
    of_lines = hashlib.sha256(SENTINEL.encode("utf-8")).hexdigest()
    assert recorded == on_disk
    assert recorded != of_lines


# -- doctor, end to end -------------------------------------------------------------


def test_doctor_counts_a_fresh_transcript_as_verified(
    root: Path, fake_provider: FakeProvider, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["ingest", A])
    capsys.readouterr()

    code = main(["doctor", "--json"])
    data = json.loads(capsys.readouterr().out)

    assert code == EXIT_OK
    assert data["clean"] is True
    assert data["hashes_verified"] == 1
    assert data["hashes_unrecorded"] == 0


def test_modifying_a_stored_transcript_is_detected(
    root: Path, fake_provider: FakeProvider, capsys: pytest.CaptureFixture[str]
) -> None:
    """The end-to-end case: rule 3 broken on a transcript the system itself
    stored, caught by doctor with no fixture hash planted by the test."""
    main(["ingest", A])
    capsys.readouterr()
    library = Library(root)
    with library.transcript_path(A).open("a", encoding="utf-8") as handle:
        handle.write("[00:00:02.000 --> 00:00:03.000] appended after storage\n")

    code = main(["doctor"])
    out = capsys.readouterr().out

    assert code == EXIT_FAILED
    assert CHECK_TRANSCRIPT_MODIFIED in out
    assert f"  {A}" in out
    # The only video changed: a finding, so nothing is verified.
    assert "hashes:   0 verified, 0 unrecorded" in out
    assert SENTINEL not in out
    assert "porcupine" not in out


def test_a_transcript_with_no_recorded_hash_is_unrecorded_not_a_fault(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """What a restored-from-archive transcript looks like: bytes on disk, a
    row that claims them, and no hash to check. Rule 14 — its own counter,
    and not a problem, because a hash computed today would prove only what
    the file looks like today."""
    library = Library(root)
    library.ensure()
    with Ledger(library.ledger_path) as ledger:
        ledger.mark_pending(B)
        library.write_transcript(B, [TranscriptLine(0, 1000, SENTINEL)])
        ledger.mark_has_transcript(B, source="captions", provider="ytdlp")
        assert ledger.get(B).content_hash is None

    code = main(["doctor", "--json"])
    data = json.loads(capsys.readouterr().out)

    assert code == EXIT_OK
    assert data["clean"] is True
    assert data["hashes_verified"] == 0
    assert data["hashes_unrecorded"] == 1
    assert data["checks"][CHECK_TRANSCRIPT_MODIFIED]["count"] == 0


# -- an interrupted store, and the way back from it ------------------------------
#
# `store_transcript` writes the file, then `meta.json`, then the row. A crash
# between the file and the row left `pending` beside immutable bytes: the note
# queue skips `pending`, `marg note <id>` refused it, `marg ingest` failed every
# time on the existing file, `refetch` never asked, and doctor called it clean.


def _interrupt_ingest(monkeypatch: pytest.MonkeyPatch, video_id: str = A) -> None:
    def crash(*_args, **_kwargs):
        raise KeyboardInterrupt

    # `main` turns a Ctrl-C into an exit code, as it does for a user.
    with monkeypatch.context() as patched:
        patched.setattr(Ledger, "mark_has_transcript", crash)
        assert main(["ingest", video_id]) != EXIT_OK


def test_doctor_reports_a_transcript_its_row_does_not_record(
    root: Path, fake_provider: FakeProvider, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _interrupt_ingest(monkeypatch)
    capsys.readouterr()

    code = main(["doctor"])

    out = capsys.readouterr().out
    assert code == EXIT_FAILED
    assert "transcript_unrecorded" in out and A in out
    assert SENTINEL not in out


def test_ingest_after_an_interrupted_store_records_the_file_without_fetching(
    root: Path, fake_provider: FakeProvider, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _interrupt_ingest(monkeypatch)
    library = Library(root)
    before = library.transcript_path(A).read_bytes()
    fetched: list[str] = []
    monkeypatch.setattr(fake_provider, "fetch", lambda video_id: fetched.append(video_id))

    first = main(["ingest", A])
    second = main(["ingest", A])

    assert (first, second) == (EXIT_OK, EXIT_OK)
    assert fetched == [], "the bytes were already there"
    with Ledger(library.ledger_path) as ledger:
        row = ledger.get(A)
        assert row.status == STATUS_HAS_TRANSCRIPT
        assert row.content_hash == library.transcript_digest(A)
        assert [r.video_id for r in ledger.videos_needing_note(version="x")] == [A]
    assert library.transcript_path(A).read_bytes() == before
    out = capsys.readouterr().out
    assert "reconciled" in out and "already have the transcript" in out
    assert SENTINEL not in out


def test_marg_reconcile_records_every_stranded_transcript_once(
    root: Path, fake_provider: FakeProvider, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _interrupt_ingest(monkeypatch, A)
    _interrupt_ingest(monkeypatch, B)
    capsys.readouterr()

    assert main(["reconcile"]) == EXIT_OK
    assert "2 row(s) recorded" in capsys.readouterr().out
    assert main(["reconcile"]) == EXIT_OK
    assert "0 row(s) recorded" in capsys.readouterr().out
    assert main(["doctor"]) == EXIT_OK


def test_reconcile_writes_what_the_file_can_tell_when_meta_never_landed(
    root: Path, fake_provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(*_args, **_kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as patched:
        patched.setattr(Library, "write_meta", crash)
        assert main(["ingest", A]) != EXIT_OK
    library = Library(root)
    assert library.read_meta(A) is None

    assert main(["reconcile", A]) == EXIT_OK

    meta = library.read_meta(A)
    assert meta["video_id"] == A and meta["line_count"] == 1
    assert meta["source"] == "unknown", "provenance nobody recorded is not guessed"
    with Ledger(library.ledger_path) as ledger:
        assert ledger.get(A).status == STATUS_HAS_TRANSCRIPT
