"""The note worker.

Every model call is mocked; the suite never touches a network. What is tested
is the part that decides what gets read, what happens when a reading fails, and
whether the two things the design promises actually hold:

* a transcript is never modified, and a failure leaves the video retryable
* notes regenerate from stored transcripts, so a better prompt rewrites the
  library without re-acquiring anything

Both are invisible when broken. A note worker that silently marked a video
noted after a failed call would leave a hole nothing ever revisits, and the
library would look complete.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_config, make_note_config

from marginalia.contracts import (
    FAILED,
    NOTED,
    SKIPPED,
    STATUS_HAS_TRANSCRIPT,
    STATUS_NEEDS_ASR,
    STOP_MAX_COST,
    STOP_REPEATED_FAILURES,
)
from marginalia.core.ledger import Ledger
from marginalia.core.library import Library
from marginalia.llm import LlmClient, LlmError
from marginalia.enrich.note import (
    REQUIRED_SECTIONS,
    NoteInvalid,
    missing_sections,
    note_header,
    validate_note,
    write_notes,
)
from marginalia.providers.base import TranscriptLine

PROMPT = '---\nname: write_note\nversion: "1"\n---\nRead {{ transcript }} about\n{{ title }} {{ channel }} {{ duration }} {{ upload_date }} {{ caption_kind }}\n{{ chapters }}\n'

# A reply the validator must accept: all seven sections, each with something
# under it. The distinctive phrases are here so the "nothing leaks to stdout"
# test has something specific to look for.
GOOD_NOTE = "\n".join(
    f"## {section}\nprivate detail about {section.lower()} — quadratic not exponential\n"
    for section in REQUIRED_SECTIONS
)


# -- scaffolding -------------------------------------------------------------


class FakeUsage:
    def __init__(self, prompt_tokens: int = 4000, completion_tokens: int = 1432) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class FakeChoice:
    def __init__(self, text: str) -> None:
        self.message = type("Message", (), {"content": text})()


class FakeResponse:
    def __init__(self, text: str, usage: FakeUsage) -> None:
        self.choices = [FakeChoice(text)]
        self.usage = usage
        self.model = "test-model"


def client(replies, *, usage: FakeUsage | None = None) -> LlmClient:
    """An LlmClient whose transport is scripted. Records every prompt sent."""
    sent: list[dict] = []

    def transport(**kwargs):
        sent.append(kwargs)
        reply = replies(len(sent) - 1) if callable(replies) else replies
        if isinstance(reply, Exception):
            raise reply
        return FakeResponse(reply, usage or FakeUsage())

    made = LlmClient(make_config(Path(".")).llm, transport=transport, sleep=lambda _: None)
    made.sent = sent  # type: ignore[attr-defined]
    return made


def library_with(tmp_path: Path, *video_ids: str, text: str = "hello world") -> tuple:
    """A library and ledger holding stored transcripts for each id."""
    config = make_config(tmp_path)
    library = Library(config.library_path)
    library.ensure()
    ledger = Ledger(library.ledger_path)
    for video_id in video_ids:
        library.write_transcript(video_id, [TranscriptLine(0, 2000, text)])
        library.write_meta(
            video_id,
            {
                "video_id": video_id,
                "title": "A video",
                "channel": "Chan",
                "duration_s": 3720,
                "upload_date": "20260101",
                "source": "captions",
                "caption_kind": "manual",
                "chapters": [{"start_ms": 0, "title": "Intro"}],
            },
        )
        ledger.mark_pending(video_id)
        ledger.mark_has_transcript(video_id, source="captions", provider="ytdlp")
    return config, library, ledger


def prompts(tmp_path: Path, version: str = "1") -> Path:
    directory = tmp_path / f"prompts-{version}"
    directory.mkdir(exist_ok=True)
    (directory / "write_note.md").write_text(
        PROMPT.replace('version: "1"', f'version: "{version}"'), encoding="utf-8"
    )
    return directory


# -- validation --------------------------------------------------------------


def test_a_complete_note_validates() -> None:
    validate_note(GOOD_NOTE)
    assert missing_sections(GOOD_NOTE) == []


def test_a_missing_section_is_an_error_not_silence() -> None:
    """A note without "Leaves out" reads exactly like a video that leaves
    nothing out, and the query layer would believe it."""
    without = GOOD_NOTE.replace("## Leaves out\n", "")
    assert "Leaves out" in missing_sections(without)
    with pytest.raises(NoteInvalid) as caught:
        validate_note(without)
    assert "Leaves out" in str(caught.value)


def test_an_empty_section_counts_as_missing() -> None:
    """The prompt requires a section to *say* it is empty. A bare heading means
    the model ran out, not that the video was flawless."""
    emptied = GOOD_NOTE.replace(
        "## Wrong or shaky\nprivate detail about wrong or shaky — quadratic not exponential\n",
        "## Wrong or shaky\n\n",
    )
    assert missing_sections(emptied) == ["Wrong or shaky"]


def test_a_section_saying_it_is_empty_is_accepted() -> None:
    stated = GOOD_NOTE.replace(
        "private detail about wrong or shaky — quadratic not exponential",
        "No errors found; the technical claims hold up.",
    )
    validate_note(stated)


def test_a_typographic_apostrophe_still_matches() -> None:
    """Models substitute ’ for ' about as often as not, and rejecting a good
    note over punctuation would look exactly like a bad prompt."""
    fancy = GOOD_NOTE.replace("Who's talking", "Who’s talking")
    assert missing_sections(fancy) == []


def test_the_header_is_built_not_generated() -> None:
    """The video ID is the one field the model must never write: the query
    layer escalates from a note to a transcript by it."""
    header = note_header(
        "dQw4w9WgXcQ",
        {
            "title": "A  video",
            "channel": "Chan",
            "duration_s": 3720,
            "upload_date": "20260101",
            "source": "captions",
            "caption_kind": "manual",
        },
    )
    assert header.splitlines()[0] == "# dQw4w9WgXcQ — A video"
    assert header.splitlines()[1] == "Chan · 1h02m · 20260101 · captions/manual"


# -- selection ---------------------------------------------------------------


def test_a_video_needing_a_note_is_selected_and_a_current_one_is_not(tmp_path: Path) -> None:
    config, library, ledger = library_with(tmp_path, "aaaaaaaaaaa", "bbbbbbbbbbb")
    ledger.mark_noted("aaaaaaaaaaa", version="1", model="test-model")

    run = write_notes(
        config, ledger, library, client(GOOD_NOTE), prompts_directory=prompts(tmp_path)
    )

    assert [r.video_id for r in run.results if r.outcome == NOTED] == ["bbbbbbbbbbb"]
    assert library.has_note("bbbbbbbbbbb")
    assert not library.has_note("aaaaaaaaaaa")


def test_a_version_bump_makes_existing_notes_stale(tmp_path: Path) -> None:
    """This is what makes regeneration a query rather than a list somebody
    maintains by hand."""
    config, library, ledger = library_with(tmp_path, "aaaaaaaaaaa")
    ledger.mark_noted("aaaaaaaaaaa", version="1", model="test-model")

    assert ledger.videos_needing_note(version="1") == []
    assert [row.video_id for row in ledger.videos_needing_note(version="2")] == ["aaaaaaaaaaa"]

    run = write_notes(
        config, ledger, library, client(GOOD_NOTE), prompts_directory=prompts(tmp_path, "2")
    )
    assert run.count(NOTED) == 1
    assert ledger.get("aaaaaaaaaaa").note_version == "2"


def test_regenerate_all_rewrites_current_notes_too(tmp_path: Path) -> None:
    config, library, ledger = library_with(tmp_path, "aaaaaaaaaaa", "bbbbbbbbbbb")
    ledger.mark_noted("aaaaaaaaaaa", version="1", model="test-model")
    ledger.mark_noted("bbbbbbbbbbb", version="1", model="test-model")

    run = write_notes(
        config,
        ledger,
        library,
        client(GOOD_NOTE),
        regenerate=True,
        every=True,
        prompts_directory=prompts(tmp_path),
    )
    assert run.count(NOTED) == 2


def test_a_named_video_with_a_current_note_is_skipped(tmp_path: Path) -> None:
    config, library, ledger = library_with(tmp_path, "aaaaaaaaaaa")
    ledger.mark_noted("aaaaaaaaaaa", version="1", model="test-model")

    run = write_notes(
        config,
        ledger,
        library,
        client(GOOD_NOTE),
        video_ids=["aaaaaaaaaaa"],
        prompts_directory=prompts(tmp_path),
    )
    assert run.count(NOTED) == 0
    assert [r.detail for r in run.results] == ["current note is v1"]


def test_a_video_that_is_not_stored_is_skipped(tmp_path: Path) -> None:
    config, library, ledger = library_with(tmp_path, "aaaaaaaaaaa")
    ledger.mark_needs_asr("aaaaaaaaaaa")

    run = write_notes(
        config,
        ledger,
        library,
        client(GOOD_NOTE),
        video_ids=["aaaaaaaaaaa"],
        prompts_directory=prompts(tmp_path),
    )
    assert run.results[0].outcome == SKIPPED
    assert STATUS_NEEDS_ASR in run.results[0].detail


def test_limit_stops_at_n(tmp_path: Path) -> None:
    """Not optional: thirty-six notes from an untuned prompt costs real money
    and produces thirty-six mediocre notes."""
    ids = ["aaaaaaaaaa1", "aaaaaaaaaa2", "aaaaaaaaaa3", "aaaaaaaaaa4", "aaaaaaaaaa5"]
    config, library, ledger = library_with(tmp_path, *ids)
    made = client(GOOD_NOTE)

    run = write_notes(
        config, ledger, library, made, limit=3, prompts_directory=prompts(tmp_path)
    )

    assert run.count(NOTED) == 3
    assert len(made.sent) == 3  # type: ignore[attr-defined]


# -- failure -----------------------------------------------------------------


def test_a_failed_call_leaves_the_video_retryable_and_the_transcript_untouched(
    tmp_path: Path,
) -> None:
    config, library, ledger = library_with(tmp_path, "aaaaaaaaaaa")
    before = library.transcript_path("aaaaaaaaaaa").read_bytes()

    run = write_notes(
        config,
        ledger,
        library,
        client(LlmError("connection refused")),
        prompts_directory=prompts(tmp_path),
    )

    assert run.results[0].outcome == FAILED
    assert not library.has_note("aaaaaaaaaaa")
    assert library.transcript_path("aaaaaaaaaaa").read_bytes() == before
    row = ledger.get("aaaaaaaaaaa")
    assert row.noted_at is None and row.status == STATUS_HAS_TRANSCRIPT
    # Retryable means the next run picks it up again, with no intervention.
    assert [r.video_id for r in ledger.videos_needing_note(version="1")] == ["aaaaaaaaaaa"]


def test_an_invalid_note_is_not_written(tmp_path: Path) -> None:
    config, library, ledger = library_with(tmp_path, "aaaaaaaaaaa")

    run = write_notes(
        config,
        ledger,
        library,
        client("## Claims\nit says things\n"),
        prompts_directory=prompts(tmp_path),
    )

    assert run.results[0].outcome == FAILED
    assert "invalid" in run.results[0].detail
    assert not library.has_note("aaaaaaaaaaa")
    assert ledger.get("aaaaaaaaaaa").noted_at is None


def test_a_note_is_written_atomically(tmp_path: Path, monkeypatch) -> None:
    """A crash mid-write must leave the previous note or none, never half of
    one: half a note passes no section check and would look like a bad model."""
    config, library, ledger = library_with(tmp_path, "aaaaaaaaaaa")
    import marginalia.core.library as library_module

    real_replace = library_module.os.replace

    def die(source, target):  # the write has happened; the rename has not
        raise OSError("crash between write and rename")

    monkeypatch.setattr(library_module.os, "replace", die)
    with pytest.raises(OSError):
        library.write_note("aaaaaaaaaaa", "partial")

    assert not library.has_note("aaaaaaaaaaa")
    monkeypatch.setattr(library_module.os, "replace", real_replace)


def test_an_over_long_transcript_is_skipped_never_split(tmp_path: Path) -> None:
    """The whole premise is one model reading the whole thing at once. Half a
    reading is not half a note; it is a different and worse artifact."""
    config, library, ledger = library_with(tmp_path, "aaaaaaaaaaa", text="word " * 500)
    config = make_config(
        tmp_path,
        library_path=config.library_path,
        note=make_note_config(max_transcript_chars=100),
    )
    made = client(GOOD_NOTE)

    run = write_notes(config, ledger, library, made, prompts_directory=prompts(tmp_path))

    assert run.results[0].outcome == SKIPPED
    assert "over" in run.results[0].detail
    assert made.sent == []  # type: ignore[attr-defined]


def test_the_cost_ceiling_stops_the_drain(tmp_path: Path) -> None:
    ids = ["aaaaaaaaaa1", "aaaaaaaaaa2", "aaaaaaaaaa3"]
    config, library, ledger = library_with(tmp_path, *ids)
    config = make_config(
        tmp_path,
        library_path=config.library_path,
        note=make_note_config(max_cost_usd=0.03),
    )
    # 4000 in + 1432 out at the test rates is ~$0.033 per note: the second
    # check sees the ceiling already breached.
    made = client(GOOD_NOTE)

    run = write_notes(config, ledger, library, made, prompts_directory=prompts(tmp_path))

    assert run.stopped_because == STOP_MAX_COST
    assert run.count(NOTED) == 1


def test_a_run_of_failures_gives_up(tmp_path: Path) -> None:
    """Observed: a run against an unreachable endpoint failed all thirty-six
    videos, four attempts each, and took six minutes to say the same thing
    thirty-six times. Nothing was wrong with the videos."""
    ids = [f"aaaaaaaaaa{n}" for n in range(1, 7)]
    config, library, ledger = library_with(tmp_path, *ids)

    run = write_notes(
        config,
        ledger,
        library,
        client(LlmError("connection refused")),
        prompts_directory=prompts(tmp_path),
    )

    assert run.stopped_because == STOP_REPEATED_FAILURES
    assert run.count(FAILED) == 3
    # Giving up early is only safe because nothing was consumed: all six are
    # still waiting for the next run.
    assert len(ledger.videos_needing_note(version="1")) == 6


def test_a_success_resets_the_failure_streak(tmp_path: Path) -> None:
    ids = [f"aaaaaaaaaa{n}" for n in range(1, 7)]
    config, library, ledger = library_with(tmp_path, *ids)
    # fail, fail, succeed, fail, fail, fail
    script = [LlmError("x"), LlmError("x"), GOOD_NOTE, LlmError("x"), LlmError("x"), LlmError("x")]

    run = write_notes(
        config,
        ledger,
        library,
        client(lambda index: script[index]),
        prompts_directory=prompts(tmp_path),
    )

    assert run.count(NOTED) == 1
    assert run.stopped_because == STOP_REPEATED_FAILURES


# -- the promises ------------------------------------------------------------


def test_regeneration_makes_no_provider_call_at_all(tmp_path: Path, monkeypatch) -> None:
    """The design claims notes are disposable and transcripts are permanent.
    If the note worker could reach a provider, that claim would be untestable
    the day acquisition breaks — which is the day it matters."""
    config, library, ledger = library_with(tmp_path, "aaaaaaaaaaa")
    ledger.mark_noted("aaaaaaaaaaa", version="0", model="old")

    import marginalia.providers as providers

    def refuse(name):
        raise AssertionError(f"the note worker reached for provider {name!r}")

    monkeypatch.setattr(providers, "get_provider", refuse)

    run = write_notes(
        config,
        ledger,
        library,
        client(GOOD_NOTE),
        regenerate=True,
        every=True,
        prompts_directory=prompts(tmp_path),
    )
    assert run.count(NOTED) == 1


def test_reading_a_transcript_records_the_access(tmp_path: Path) -> None:
    """The deletion policy that arrives later depends on it, and it cannot be
    reconstructed retroactively."""
    config, library, ledger = library_with(tmp_path, "aaaaaaaaaaa")
    assert ledger.get("aaaaaaaaaaa").last_accessed is None

    write_notes(config, ledger, library, client(GOOD_NOTE), prompts_directory=prompts(tmp_path))

    assert ledger.get("aaaaaaaaaaa").last_accessed is not None


def test_the_whole_transcript_is_sent_in_one_call(tmp_path: Path) -> None:
    config, library, ledger = library_with(tmp_path, "aaaaaaaaaaa", text="a distinctive phrase")
    made = client(GOOD_NOTE)

    write_notes(config, ledger, library, made, prompts_directory=prompts(tmp_path))

    sent = made.sent  # type: ignore[attr-defined]
    assert len(sent) == 1
    assert "a distinctive phrase" in sent[0]["messages"][1]["content"]


def test_the_stored_note_carries_the_header_and_the_body(tmp_path: Path) -> None:
    config, library, ledger = library_with(tmp_path, "aaaaaaaaaaa")

    write_notes(config, ledger, library, client(GOOD_NOTE), prompts_directory=prompts(tmp_path))

    note = library.read_note("aaaaaaaaaaa")
    assert note.startswith("# aaaaaaaaaaa — A video\n")
    assert missing_sections(note) == []
    row = ledger.get("aaaaaaaaaaa")
    assert (row.note_version, row.note_model) == ("1", "test-model")
