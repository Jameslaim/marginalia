"""The library on disk, held against the ledger.

Rule 2 — never write into the library directly — cannot be enforced from inside
this program. A Python process cannot stop another process writing to a folder;
the operating system could and does not, and the editor hook that backs the rule
matches four tool calls and no shell command. So the rule is kept by being
followed, and what this module adds is the other half: a violation cannot be
prevented, but it can be made impossible to miss.

Seven disagreements, seven counters, and **nothing is fixed, written, fetched or
asked of a model**. A doctor that repaired what it found would be a second writer
into the library with its own idea of which side is right, which is the state
rule 2 exists to prevent.

The two traps, because a doctor that reports correct state as drift trains its
reader to ignore it:

- A ``decisions`` row with no ``videos`` row is **correct** (rule 11). Only
  ``mark_pending`` inserts into ``videos``, immediately before a fetch, so every
  candidate stage 1 declined exists in ``decisions`` and nowhere else. The same
  goes for ``queries`` and ``plans``, which belong to ``runs``. None of the three
  is checked against ``videos``, and 231 "orphan decisions" once reported as a
  failure were every one of them the system working.
- A ``needs_asr``, ``pending`` or ``failed`` row with no directory is **correct**
  (rule 6). The row is written before the fetch, and a video with no captions
  gets a row and no files. Check 2 keys on what the *status claims*, never on
  "a row exists but files do not".

Rule 1 in full: no finding carries transcript or note text. Ids, statuses,
section names and byte counts only. Reading a file to hash it or to check its
headings is an integrity read, not an access, and ``touch_accessed`` is
deliberately not called — see ``Library.transcript_digest``.
"""

from __future__ import annotations

import logging
from typing import Sequence

from ..contracts import (
    CHECK_NOTE_INVALID,
    CHECK_NOTE_MISSING,
    CHECK_NOTE_UNRECORDED,
    CHECK_RUN_UNFINISHED,
    CHECK_TRANSCRIPT_MISSING,
    CHECK_TRANSCRIPT_MODIFIED,
    CHECK_UNTRACKED_DIR,
    STATUS_HAS_TRANSCRIPT,
    DoctorFinding,
    DoctorReport,
    VideoRow,
)
from ..core.ledger import RunRow
from ..core.library import Library
from ..enrich.note import NoteInvalid, validate_note

log = logging.getLogger(__name__)

__all__ = ["diagnose", "check_directories", "check_runs"]


def diagnose(library: Library, rows: Sequence[VideoRow], runs: Sequence[RunRow]) -> DoctorReport:
    """Every disagreement between ``library`` and the ledger rows it was given.

    Takes rows rather than a ``Ledger`` so the ledger is opened — or, when the
    file does not exist, deliberately *not* created — by the caller. Read-only
    all the way down.
    """
    on_disk = library.video_ids_on_disk()
    findings: list[DoctorFinding] = list(check_directories(rows, on_disk))
    verified = unrecorded = 0
    for row in rows:
        row_findings, hashed = _check_row(library, row)
        findings.extend(row_findings)
        if hashed is True:
            verified += 1
        elif hashed is False:
            unrecorded += 1
    findings.extend(check_runs(runs))
    report = DoctorReport(
        findings=tuple(findings),
        videos_checked=len(rows),
        dirs_checked=len(on_disk),
        runs_checked=len(runs),
        hashes_verified=verified,
        hashes_unrecorded=unrecorded,
    )
    log.info(
        "doctor: %d video(s), %d dir(s), %d run(s), %d finding(s)",
        report.videos_checked,
        report.dirs_checked,
        report.runs_checked,
        len(report.findings),
    )
    return report


# -- pure checks ------------------------------------------------------------


def check_directories(rows: Sequence[VideoRow], on_disk: Sequence[str]) -> list[DoctorFinding]:
    """Check 1: a ``videos/<id>/`` directory with no ``videos`` row.

    The one direction that means *something wrote without the CLI*. The other
    direction — a row with no directory — is not checked here and must not be:
    rule 6 writes the row before the fetch, so it is the normal shape of a
    ``needs_asr``, ``pending`` or ``failed`` video.
    """
    known = {row.video_id for row in rows}
    return [
        DoctorFinding(CHECK_UNTRACKED_DIR, video_id)
        for video_id in on_disk
        if video_id not in known
    ]


def check_runs(runs: Sequence[RunRow]) -> list[DoctorFinding]:
    """Check 7: a run with no ``finished_at``.

    Reported and never an error. This is the signal rule 6's ordering exists to
    leave behind — ``start_run`` precedes the first judgement, so a crash leaves
    exactly this — and a run in progress right now looks identical.
    """
    return [
        DoctorFinding(CHECK_RUN_UNFINISHED, run.run_id, run.started_at[:10])
        for run in runs
        if run.finished_at is None
    ]


# -- per-row checks (read the disk, never the ledger) ---------------------------


def _check_row(library: Library, row: VideoRow) -> tuple[list[DoctorFinding], bool | None]:
    """Checks 2 through 6 for one row.

    The second value says what happened to the hash: ``True`` compared, ``False``
    a transcript is on disk with no ``content_hash`` to compare against, ``None``
    nothing to compare.
    """
    findings: list[DoctorFinding] = []
    video_id = row.video_id
    has_transcript = library.has_transcript(video_id)
    has_note = library.has_note(video_id)

    # 2. Keyed on the claim, not on the absence of files.
    if row.status == STATUS_HAS_TRANSCRIPT and not has_transcript:
        findings.append(DoctorFinding(CHECK_TRANSCRIPT_MISSING, video_id, row.status))

    # 3 and 4. The two directions are two findings: one says the ledger
    # recorded a note that is gone, the other that a note arrived the ledger
    # never saw. They point at different writers.
    if row.noted_at is not None and not has_note:
        findings.append(DoctorFinding(CHECK_NOTE_MISSING, video_id, row.note_version))
    if has_note and row.noted_at is None:
        findings.append(DoctorFinding(CHECK_NOTE_UNRECORDED, video_id, _size_label(library, video_id)))

    # 5. The note is read to check its headings and discarded. Not an access —
    # nothing here is answered from it — so `touch_accessed` is not called, on
    # the same reasoning as `Library.transcript_digest`. The exception message
    # names sections, never content, and that is all that reaches the finding.
    if has_note:
        text = library.read_note(video_id)
        try:
            validate_note(text or "")
        except NoteInvalid as exc:
            findings.append(DoctorFinding(CHECK_NOTE_INVALID, video_id, str(exc)))
        del text

    # 6. Rule 3, checked. Only a row that carries a hash can disagree with one;
    # a transcript with no recorded hash is counted, not judged.
    hashed: bool | None = None
    if has_transcript:
        if row.content_hash is None:
            hashed = False
        elif library.transcript_digest(video_id) == row.content_hash:
            hashed = True
        else:
            # A finding, and not "verified": counted as verified before it was
            # compared, a changed transcript read as one more verified hash in
            # the line under the finding that said it had changed (rule 14).
            findings.append(DoctorFinding(CHECK_TRANSCRIPT_MODIFIED, video_id))

    return findings, hashed


def _size_label(library: Library, video_id: str) -> str:
    try:
        return f"{library.note_path(video_id).stat().st_size} bytes"
    except OSError:  # pragma: no cover - the file was there a moment ago
        return "unreadable"
