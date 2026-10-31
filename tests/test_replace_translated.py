"""`marg refetch --replace-translated`: rule 3's one exception, and nothing broader.

A stored transcript is replaced only when rule 40's selector, on a fresh
listing, picks a track in a different language — a translation. The old file is
kept, the replacement recorded, the note marked stale. An accepted track is
never touched, a short or empty replacement is refused, a stored track id is
cleaned in `meta.json` alone, and a dry run writes nothing. Caption text is
invented.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from conftest import make_config
from marginalia.contracts import (
    REPAIR_ACCEPTED,
    REPAIR_LANGUAGE_CLEANED,
    REPAIR_REPLACED,
    REPAIR_SKIPPED,
    TranscriptLine,
    TranscriptResult,
)
from marginalia.core.ledger import STALE_TRANSCRIPT_SUFFIX, Ledger
from marginalia.core.library import Library
from marginalia.flow.acquire import acquire
from marginalia.flow.repair import MIN_SPAN_SHARE, replace_translated
from marginalia.flow.store import store_transcript
from marginalia.providers.base import TranslatedOnly

VIDEO = "aaaaaaaaaaa"


def _lines(seconds: int, word: str) -> list[TranscriptLine]:
    return [TranscriptLine(s * 1000, (s + 5) * 1000, f"{word} {s}") for s in range(0, seconds, 5)]


def _result(language: str, seconds: int = 60, track: str = "", word: str = "new") -> TranscriptResult:
    return TranscriptResult(
        lines=_lines(seconds, word) if seconds else [], language=language, source="captions",
        title="t", channel="c", duration_s=60, upload_date=None, caption_kind="auto",
        caption_track=track or f"{language}-orig",
    )


class Provider:
    name = "ytdlp"

    def __init__(self, answer) -> None:
        self.answer = answer
        self.calls = 0

    def fetch(self, video_id):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture()
def stored(tmp_path):
    """A library holding one stored video, its language as given, with a note."""
    library = Library(tmp_path / "library")
    library.ensure()

    def make(language: str, *, noted: bool = True):
        with Ledger(library.ledger_path) as ledger:
            ledger.mark_pending(VIDEO)
            store_transcript(library, ledger, VIDEO, _result(language, word="old", track=language),
                             provider="ytdlp")
            meta = library.read_meta(VIDEO)
            library.write_meta(VIDEO, {**meta, "language": language})
            if noted:
                library.write_note(VIDEO, "# a note\n")
                ledger.mark_noted(VIDEO, version="9", model="m")
        return library

    return make


def _run(library, answer, *, dry_run=False, tmp_path=None):
    config = make_config(library.root.parent, library_path=library.root)
    provider = Provider(answer)
    with Ledger(library.ledger_path) as ledger:
        run = replace_translated(config, ledger, library, dry_run=dry_run, providers=[provider],
                                 sleep=lambda s: None)
        rows = ledger.repairs_for(VIDEO)
        video = ledger.get(VIDEO)
    return run, rows, video


def _snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and p.suffix != ".db" and "logs" not in p.parts}


def test_a_replacement_keeps_the_old_file_records_it_and_marks_the_note_stale(stored) -> None:
    library = stored("ar")
    old = library.transcript_path(VIDEO).read_bytes()
    old_hash = library.transcript_digest(VIDEO)

    run, rows, video = _run(library, _result("en"))

    assert [r.action for r in run.results] == [REPAIR_REPLACED]
    assert library.translated_path(VIDEO).read_bytes() == old, "the old file is kept, byte for byte"
    assert library.transcript_path(VIDEO).read_bytes() != old
    meta = library.read_meta(VIDEO)
    assert (meta["language"], meta["caption_track"], meta["replaced_language"]) == ("en", "en-orig", "ar")
    (row,) = rows
    assert (row.action, row.old_language, row.new_language, row.new_track) == ("replaced", "ar", "en", "en-orig")
    assert row.old_hash == old_hash and row.new_hash == video.content_hash != old_hash
    assert video.status == "has_transcript"
    assert video.note_version == "9" + STALE_TRANSCRIPT_SUFFIX


def test_an_accepted_track_is_never_touched(stored) -> None:
    library = stored("hi")
    before = _snapshot(library.root)

    run, rows, video = _run(library, _result("hi"))

    assert [r.action for r in run.results] == [REPAIR_ACCEPTED]
    assert _snapshot(library.root) == before
    assert rows == [] and not library.translated_path(VIDEO).exists()
    assert video.note_version == "9"


@pytest.mark.parametrize("seconds", [0, 30])
def test_a_short_or_empty_replacement_is_refused(stored, seconds) -> None:
    library = stored("ar")
    before = _snapshot(library.root)

    run, rows, _ = _run(library, _result("en", seconds=seconds))

    (result,) = run.results
    assert result.action == REPAIR_SKIPPED and f"under {MIN_SPAN_SHARE:.0%}" in result.detail
    assert _snapshot(library.root) == before and rows == []


def test_an_odd_code_is_cleaned_and_the_transcript_untouched(stored) -> None:
    library = stored("en-eEY6OEpapPo")
    transcript = library.transcript_path(VIDEO).read_bytes()

    run, rows, video = _run(library, _result("en", track="en-eEY6OEpapPo"))

    assert [r.action for r in run.results] == [REPAIR_LANGUAGE_CLEANED]
    meta = library.read_meta(VIDEO)
    assert (meta["language"], meta["caption_track"]) == ("en", "en-eEY6OEpapPo")
    assert library.transcript_path(VIDEO).read_bytes() == transcript
    assert not library.translated_path(VIDEO).exists()
    assert [(r.action, r.old_language, r.new_language) for r in rows] == [
        ("language_cleaned", "en-eEY6OEpapPo", "en")
    ]
    assert video.note_version == "9", "the transcript did not change, so neither did the note's standing"


def test_the_dry_run_writes_nothing(stored) -> None:
    library = stored("ar")
    before = _snapshot(library.root)

    run, rows, video = _run(library, _result("en"), dry_run=True)

    assert run.dry_run and [r.action for r in run.results] == [REPAIR_REPLACED]
    assert _snapshot(library.root) == before
    assert rows == [] and video.note_version == "9"


def test_no_original_track_leaves_the_stored_transcript(stored) -> None:
    library = stored("ar")
    before = _snapshot(library.root)
    run, rows, video = _run(library, TranslatedOnly("en", ["fr"]))
    assert [(r.action, r.detail) for r in run.results] == [(REPAIR_SKIPPED, "only translated captions")]
    assert _snapshot(library.root) == before and video.status == "has_transcript"


def test_a_kept_translation_is_never_overwritten(stored) -> None:
    library = stored("ar")
    library.replace_translated_transcript(VIDEO, _lines(60, "first"))
    with pytest.raises(FileExistsError):
        library.replace_translated_transcript(VIDEO, _lines(60, "second"))


def test_acquire_without_recording_leaves_a_stored_row_alone(stored) -> None:
    library = stored("ar")
    with Ledger(library.ledger_path) as ledger:
        acquire(ledger, VIDEO, [Provider(None)], record=False)
        assert ledger.get(VIDEO).status == "has_transcript"
        acquire(ledger, VIDEO, [Provider(TranslatedOnly("en"))], record=False)
        assert ledger.get(VIDEO).status == "has_transcript"


# -- rule 2: the hook still blocks a direct write -----------------------------


def test_the_guard_still_blocks_writing_the_kept_translation(tmp_path, monkeypatch) -> None:
    from marginalia.cli import EXIT_BLOCKED, main

    root = tmp_path / "library"
    config = tmp_path / "config.toml"
    config.write_text(f'[library]\npath = "{root.as_posix()}"\n', encoding="utf-8")
    monkeypatch.setenv("MARGINALIA_CONFIG", str(config))
    for target in ("transcript.translated.txt", "transcript.txt"):
        event = {"hook_event_name": "PreToolUse", "tool_name": "Write",
                 "tool_input": {"file_path": str(root / "videos" / VIDEO / target)},
                 "cwd": str(tmp_path)}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
        assert main(["guard-write"]) == EXIT_BLOCKED, target


def test_the_command_line_reports_each_count(stored, monkeypatch, capsys) -> None:
    import marginalia.flow.repair as repair
    from marginalia.cli import EXIT_OK, main

    library = stored("ar")
    config = library.root.parent / "config.toml"
    config.write_text(f'[library]\npath = "{library.root.as_posix()}"\n\n'
                      '[llm]\napi_key = "k"\nbase_url = "https://example.invalid/v1"\nmodel = "m"\n',
                      encoding="utf-8")
    monkeypatch.setenv("MARGINALIA_CONFIG", str(config))
    monkeypatch.setattr(repair, "transcript_providers", lambda config: [Provider(_result("en"))])
    monkeypatch.setattr(repair.time, "sleep", lambda s: None)

    assert main(["refetch", "--replace-translated", "--dry-run"]) == EXIT_OK
    out = capsys.readouterr().out
    assert f"would replace {VIDEO}" in out and "ar -> en (en-orig)" in out
    assert "1 considered (dry run, nothing written) — 1 replaced, 0 language cleaned, 0 accepted, " \
           "0 skipped, 0 failed" in out
    assert not library.translated_path(VIDEO).exists()

    assert main(["refetch", VIDEO]) != EXIT_OK, "ids are taken only with --replace-translated"
