"""The marginalia command line.

    marg ingest <url>       ingest one video
    marg search "<query>"   find candidate videos and say which are new
    marg discover "<topic>" turn a topic into a library
    marg note               write understanding notes for stored videos
    marg snap-timestamps    align cited timestamps in notes to the transcript
    marg ask "<question>"   answer a question from every note at once
    marg run export <id>    render one discovery run as a readable folder
    marg doctor             compare the library on disk against the ledger
    marg llm-check          one request to the configured model provider
    marg status <url|id>    show ledger state for one video
    marg list               list stored videos
    marg guard-write        internal: the PreToolUse hook

Output is terse and single-line, because a skill reads it rather than a human.
The one exception is `ask`, which prints the answer itself — that is the product,
and it is prose written *from* the notes rather than the notes.

Transcript contents are never printed. Not on success, not on error, not under
--verbose. That rule is the whole reason this is a CLI and not a tool the model
calls directly. Notes are held to the same rule: smaller than a transcript,
still large enough that thirty-six of them fill a context window.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .config import Config, ConfigError, library_root, load_config
from .contracts import (
    BASELINE_FRESH,
    BASELINE_REUSED,
    RECENCY_DECIDED,
    RECENCY_OWNER,
    RECENCY_SAMPLED,
    ITEM_STATES,
    ACQUIRE_FAILED_PERMANENT,
    ACQUIRE_FAILED_TRANSIENT,
    ACQUIRE_NO_CAPTIONS,
    ACQUIRE_TRANSLATED_ONLY,
    GONE,
    NO_CAPTIONS,
    TRANSLATED_ONLY,
    RECOVERED,
    STILL_FAILING,
    STOP_REFUSED,
    STOP_THROTTLED,
    FAILED,
    HARD_FILTERS,
    KNOWN,
    NEW,
    NOTED,
    ORIGIN_PLAN,
    ORIGIN_TERRITORY,
    REJECTED,
    SEARCH_SOURCES,
    SOURCE_PLAN,
    SKIPPED,
    STATUS_HAS_TRANSCRIPT,
    CHECK_NOTE_INVALID,
    CHECK_NOTE_MISSING,
    CHECK_NOTE_UNRECORDED,
    CHECK_RUN_UNFINISHED,
    CHECK_TRANSCRIPT_MISSING,
    CHECK_TRANSCRIPT_MODIFIED,
    CHECK_TRANSCRIPT_UNRECORDED,
    CHECK_UNTRACKED_DIR,
    DOCTOR_CHECKS,
    Answer,
    Candidate,
    DiscoverRun,
    DoctorReport,
    RoundReport,
    SearchRun,
    TranscriptResult,
    VideoRow,
)
from .core.ids import extract_video_id, is_video_id
from .flow.acquire import acquire, transcript_providers
from .flow.brief import (
    BriefNotApproved,
    BriefPendingExists,
    approve,
    brief_status,
    run_brief,
    set_owner_recency,
    write_pending,
)
from .flow.discover import STOP_MODEL_UNAVAILABLE, decide_recency, discover
from .flow.doctor import diagnose
from .flow.export import export_run
from .flow.refetch import refetch
from .flow.repair import MIN_SPAN_SHARE, replace_translated
from .flow.store import reconcile_stored, store_transcript, unreconciled
from .brain.judge import BriefUnusable, Judge
from .llm import LlmClient, LlmConfigError, LlmError
from .core.ledger import FAIL_PERMANENT, FAIL_TRANSIENT, Ledger, skip_reason
from .core.library import Library, format_duration
from .enrich.note import note_version, write_notes
from .ask import (
    AskTooLarge,
    SelectionFailed,
    answer_question,
    deep_link,
    has_grounding,
    mark_unverified,
    render_answer,
    render_links,
)
from .enrich.timestamps import snap_library
from .providers import PermanentError
from .providers.base import TranscriptProvider
from .flow.search import run_search

log = logging.getLogger("marginalia")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
# The PreToolUse contract: exit 2 blocks the tool call and shows stderr to the
# agent. Anything else lets it through.
EXIT_BLOCKED = 2

_VERB_WIDTH = 9
_TITLE_LIMIT = 80
# Narrower than _TITLE_LIMIT so duration and channel line up in a search list.
_SEARCH_TITLE_WIDTH = 46


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8_output()
    parser = _build_parser()
    args = parser.parse_args(_shield_leading_hyphen_ids(argv))

    _configure_console(verbose=args.verbose)

    try:
        return int(args.handler(args))
    except ConfigError as exc:
        return _error(str(exc), EXIT_USAGE)
    except PermanentError as exc:
        # A bad query, a rejected key, an unknown provider name. Deliberately
        # not softened into a fallback: all three are the user's to fix.
        return _error(str(exc), EXIT_USAGE)
    except SelectionFailed as exc:
        # Same shape as AskTooLarge: a sentence naming what happened and
        # what was not sent, rather than a class name and a traceback.
        return _error(str(exc), EXIT_FAILED)
    except BriefNotApproved as exc:
        # Opted in, and the topic has no approved brief: stopped before the run
        # row, with the three commands that make one.
        return _error(str(exc), EXIT_FAILED)
    except BriefUnusable as exc:
        # The run stopped before it started: no draft was usable, so nothing
        # was searched or recorded. The message names each attempt and what
        # to change.
        return _error(str(exc), EXIT_FAILED)
    except AskTooLarge as exc:
        # A sentence someone can act on, not a class name and a traceback. The
        # whole point of the guard is that the run ends with the two numbers and
        # what to do about them.
        return _error(str(exc), EXIT_FAILED)
    except LlmConfigError as exc:
        # No balance, a rejected key, an unknown model. Named here rather than
        # left to the catch-all below, which would prefix the class name onto a
        # message already written to be read.
        return _error(str(exc), EXIT_FAILED)
    except KeyboardInterrupt:  # pragma: no cover
        return _error("interrupted", EXIT_FAILED)
    except Exception as exc:  # noqa: BLE001 - the CLI is the last line of defence
        log.debug("unhandled error", exc_info=True)
        return _error(f"{type(exc).__name__}: {exc}", EXIT_FAILED)


def _shield_leading_hyphen_ids(argv: Sequence[str] | None) -> Sequence[str] | None:
    """Let a video ID that begins with ``-`` be typed without ``--``.

    **YouTube IDs may start with a hyphen** — `-Ueh5XBpcoY`, `-0czfBICp74` — and
    roughly one ID in thirty-two does. argparse reads such a token as an option
    and fails with "unrecognized arguments", which makes a documented command
    (`marg note --regenerate -Ueh5XBpcoY`) unusable for those videos.

    Same family as the citation repair: an ID is case-sensitive, may begin with
    a hyphen, and must never be normalised to suit a parser. So the parser is
    adjusted instead — a POSIX ``--`` is inserted before the first such token,
    which is exactly what a user would have to type by hand.

    Deliberately narrow. A token qualifies only if it starts with a single
    hyphen, is exactly eleven characters, and satisfies ``is_video_id``. Every
    real option here is either two characters (``-v``) or long-form (``--json``),
    so no flag can collide; and ``--`` is only inserted when nothing has already
    ended option parsing.
    """
    if argv is None:
        argv = sys.argv[1:]
    tokens = list(argv)
    if "--" in tokens:
        return tokens
    for index, token in enumerate(tokens):
        if (
            token.startswith("-")
            and not token.startswith("--")
            and len(token) == 11
            and is_video_id(token)
        ):
            log.debug("cli: shielding %r, a video id that begins with a hyphen", token)
            return [*tokens[:index], "--", *tokens[index:]]
    return tokens


# -- commands --------------------------------------------------------------


def _cmd_ingest(args: argparse.Namespace) -> int:
    video_id = extract_video_id(args.url)
    if video_id is None:
        return _error(f"not a YouTube URL or video ID: {args.url!r}", EXIT_USAGE)

    config = _load(args)
    library = Library(config.library_path)
    library.ensure()

    with Ledger(library.ledger_path) as ledger:
        existing = ledger.get(video_id)
        # An interrupted store left the bytes and not the row. Fetching again
        # can only fail — `transcript.txt` is immutable — so record what is
        # there instead. No network call, and nothing on disk changes.
        if reconcile_stored(library, ledger, video_id):
            _report("reconciled", video_id, "transcript was on disk; ledger row recorded")
            return EXIT_OK
        held = library.has_transcript(video_id)
        if existing is None and held:
            # Bytes on disk that no row records: written without the CLI (rule
            # 2). Fetching could only collide with the immutable file — it used
            # to make the network call anyway, then die on `FileExistsError` and
            # leave a `pending` row behind. Adopting the file silently would
            # launder the violation into the ledger, so it is reported instead.
            return _error(
                f"{video_id}: transcript.txt is on disk with no ledger row (written "
                "without the CLI); nothing was fetched — see `marg doctor`",
                EXIT_FAILED,
            )
        reason = skip_reason(existing, transcript_present=held)
        if reason is not None:
            _report("skipped", video_id, reason)
            return EXIT_OK
        if existing is not None:
            log.info("re-attempting %s (ledger status %s)", video_id, existing.status)

        # Resolved before the row is written: an unknown provider name is a
        # configuration error. It used to be recorded as the video's own
        # permanent failure, and fixing the typo then skipped it forever.
        providers = transcript_providers(config)
        # Ledger row before the network call, always (inside `acquire`). A
        # crash after that point leaves `pending`, which is exactly what the
        # next run needs to know.
        acquisition = acquire(ledger, video_id, providers)
        row = ledger.get(video_id)  # `acquire` wrote it before fetching
        attempts = row.attempts if row is not None else 1

        if acquisition.outcome in (ACQUIRE_FAILED_PERMANENT, ACQUIRE_FAILED_TRANSIENT):
            kind = FAIL_PERMANENT if acquisition.outcome == ACQUIRE_FAILED_PERMANENT else FAIL_TRANSIENT
            _report("failed", video_id, f"{kind}: {acquisition.detail} (attempt {attempts})")
            return EXIT_FAILED

        if acquisition.outcome == ACQUIRE_NO_CAPTIONS:
            answering = next(
                (p for p in providers if p.name == acquisition.provider), providers[0]
            )
            title = _title_of(_metadata(answering, video_id))
            _report("needs-asr", video_id, _quoted(title), "no captions available")
            return EXIT_OK

        if acquisition.outcome == ACQUIRE_TRANSLATED_ONLY:
            answering = next(
                (p for p in providers if p.name == acquisition.provider), providers[0]
            )
            title = _title_of(_metadata(answering, video_id))
            _report("needs-asr", video_id, _quoted(title), acquisition.detail)
            return EXIT_OK

        result = acquisition.result
        assert result is not None
        count, _digest = store_transcript(
            library, ledger, video_id, result,
            provider=acquisition.provider or "unknown", original_url=args.url,
        )
        _report(
            "has-transcript",
            video_id,
            _quoted(result.title),
            f"{count} lines",
            f"{result.source}/{result.language}",
            *([f"audio labelled {result.audio_label}"] if result.audio_label else []),
        )
        return EXIT_OK


def _cmd_search(args: argparse.Namespace) -> int:
    config = _load(args)
    library = Library(config.library_path)
    library.ensure()

    with Ledger(library.ledger_path) as ledger:
        # Gate 2's held exemption asks the disk, as discovery's does: a row
        # saying `has_transcript` over a missing file is not held, and bytes an
        # interrupted store left under a `pending` row are.
        run = run_search(
            config,
            ledger,
            args.query,
            limit=args.limit,
            topic=args.topic,
            holds=library.has_transcript,
        )

    if args.json:
        print(json.dumps(_as_json(run), indent=2, ensure_ascii=False))
        return EXIT_OK

    for candidate in run.candidates:
        if candidate.verdict == NEW:
            _report(
                NEW,
                candidate.video_id,
                _padded(
                    _quoted(candidate.result.title, _SEARCH_TITLE_WIDTH - 2),
                    _SEARCH_TITLE_WIDTH,
                ),
                _duration(candidate.result.duration_s),
                candidate.result.channel or None,
            )
        else:
            _report(candidate.verdict, candidate.video_id, candidate.reason)

    print(_summary(run))
    return EXIT_OK


def _summary(run: SearchRun) -> str:
    counts = run.counts
    line = (
        f"{counts['found']} found, {counts[KNOWN]} known, "
        f"{counts[REJECTED]} previously rejected, {counts[NEW]} new"
    )
    line += f" — {run.provider}" if run.provider else " — no provider"
    if run.fallbacks:
        # Never silent: a fallback that nobody sees looks like a quota that
        # never runs out, right up until the day the overflow breaks too.
        fell = ", ".join(f"{name}: {why}" for name, why in run.fallbacks)
        line += f" (fell back from {fell})"
    return line


def _as_json(run: SearchRun) -> dict[str, Any]:
    """The same data, structured. M2b consumes this instead of parsing lines."""
    return {
        "query": run.query,
        "topic": run.topic,
        "provider": run.provider,
        "fallbacks": [{"provider": name, "reason": why} for name, why in run.fallbacks],
        "counts": run.counts,
        "candidates": [_candidate_json(candidate) for candidate in run.candidates],
    }


def _candidate_json(candidate: Candidate) -> dict[str, Any]:
    result = candidate.result
    return {
        "video_id": result.video_id,
        "verdict": candidate.verdict,
        "reason": candidate.reason,
        "title": result.title,
        "channel": result.channel,
        "description": result.description,
        "duration_s": result.duration_s,
        "published_at": result.published_at,
        "view_count": result.view_count,
        "provider": result.provider,
    }


# Discovery stops that end the run before its work: printed in full, exit 1.
_DISCOVER_FAILED = frozenset({STOP_MODEL_UNAVAILABLE, STOP_THROTTLED})


def _cmd_discover(args: argparse.Namespace) -> int:
    config = _load(args)
    library = Library(config.library_path)
    library.ensure()

    client = LlmClient(config.llm)
    with Ledger(library.ledger_path) as ledger:
        run = discover(
            config,
            ledger,
            library,
            client,
            args.topic,
            max_rounds=args.max_rounds,
            max_videos=args.max_videos,
            max_requests=args.max_requests,
            dry_run=args.dry_run,
            search_source=args.search_source,
            web_context=None if args.web_context is None else args.web_context == "on",
        )

    # Recorded and reported in full, and still not a success: a run the model
    # provider ended is reported, then exits as failed.
    status = EXIT_FAILED if run.stopped_because in _DISCOVER_FAILED else EXIT_OK
    if args.json:
        print(json.dumps(_discover_json(run), indent=2, ensure_ascii=False))
        return status

    for line in _discover_report(run):
        print(line)
    return status


def _brief_line(run) -> str:
    """The brief's intent, and which brief it was: the topic's approved
    version, reused, or a fresh one — with the command that makes one."""
    brief = run.brief
    if brief.approved_version is not None:
        return f"brief:    {brief.intent} [approved brief v{brief.approved_version}, reused]"
    quoted = json.dumps(run.topic, ensure_ascii=False)
    return (
        f"brief:    {brief.intent} [fresh brief: no approved brief for this topic; "
        f"make one with marg brief {quoted}]"
    )


def _cmd_brief(args: argparse.Namespace) -> int:
    """Write a topic's brief to review, approve it, or show where it stands.

    Writing costs one write and one review call and never searches; approving
    and the status make no model call at all.
    """
    config = _load(args)
    library = Library(config.library_path)
    library.ensure()
    quoted = json.dumps(args.topic, ensure_ascii=False)

    if args.recency is not None and not args.approve:
        return _brief_recency(config, library, args.topic, args.recency)
    if getattr(args, "reopen_gaps", False):
        return _reopen_gaps(library, args.topic)

    if args.approve or args.status:
        with Ledger(library.ledger_path) as ledger:
            if args.status:
                status = brief_status(ledger, library, args.topic)
                print(f"topic:    {status.topic}")
                state = (
                    "none" if not status.pending
                    else "differs from the latest approval" if status.pending_differs
                    else "same as the latest approval"
                )
                print(f"pending:  {status.pending_path} ({state})")
                for version, sha256, approved_at in status.versions:
                    print(f"approved: v{version} {sha256[:12]} at {approved_at}")
                if not status.versions:
                    print("approved: none — every run writes a fresh brief")
                else:
                    print(_recency_status(ledger, status.topic, status.versions[-1][0]))
                    print(_gaps_status(ledger, status.topic, status.versions[-1][0]))
                return EXIT_OK
            result = approve(ledger, library, args.topic)
        if result.problem:
            return _error(
                f"brief not approved: {result.problem}. Nothing was stored; the pending "
                f"file is kept at {result.path}",
                EXIT_FAILED,
            )
        if result.unchanged:
            print(f"unchanged: the pending brief is approved brief v{result.version} already")
            return EXIT_OK
        print(f"approved: brief v{result.version} for {quoted} (sha256 {result.sha256[:12]})")
        print(f"copy:     {result.approved_path}")
        print("every marg discover on this topic now reuses it")
        if args.recency is not None:
            return _brief_recency(config, library, args.topic, args.recency)
        return EXIT_OK

    try:
        pending = write_pending(
            config, library, Judge(LlmClient(config.llm)), args.topic, force=args.force
        )
    except BriefPendingExists as exc:
        return _error(str(exc), EXIT_FAILED)
    print(f"pending:  brief written for {quoted} (prompts v{pending.version})")
    for number, problem in enumerate(pending.draft_problems, start=1):
        print(f"          draft {number} unusable ({problem}); drawn again")
    if pending.review_problem:
        print(f"          review unusable ({pending.review_problem}); this is the unreviewed draft")
    print(f"file:     {pending.path}")
    print(f"parts:    {len(pending.parts)}")
    for name, things in pending.parts:
        print(f"  - {name}")
        for thing in things:
            print(f"      . {thing}")
    print(f"next:     edit the file, then run: marg brief {quoted} --approve")
    return EXIT_OK


def _recency_status(ledger: Ledger, topic: str, version: int) -> str:
    """The latest approved version's recency, and whether it was decided or owner-set."""
    row = ledger.brief_recency(topic, version)
    if row is None:
        return (f"recency:  v{version} not decided yet — the next run on it decides it "
                "(the widest of its samples), or set it with --recency")
    label = f"last {row.months} months" if row.months else "any time"
    if row.source == RECENCY_OWNER:
        return f"recency:  v{version} {label} — owner-set at {row.set_at}"
    samples = "/".join("any time" if m is None else str(m) for m in json.loads(row.samples_json))
    by = f"run {row.run_id}" if row.run_id else "marg brief --recency decide"
    return f"recency:  v{version} {label} — decided by {by} from samples {samples}, at {row.set_at}"


def _gaps_status(ledger: Ledger, topic: str, version: int) -> str:
    """The gaps exhausted on the latest approved version, each with its queries."""
    rows = ledger.exhausted_gaps(topic, version)
    if not rows:
        return f"gaps:     v{version} none exhausted"
    named = "; ".join(f"{row.line} ({row.queries} queries, run {row.run_id})" for row in rows)
    return (f"gaps:     v{version} {len(rows)} exhausted, one probe query each per run — "
            f"{named}; re-open with --reopen-gaps")


def _reopen_gaps(library: Library, topic: str) -> int:
    """Re-open every exhausted gap on the approved brief: a full share again."""
    with Ledger(library.ledger_path) as ledger:
        approved = ledger.approved_brief(topic)
        if approved is None:
            return _error("no gaps re-opened: no approved brief for this topic", EXIT_FAILED)
        rows = ledger.reopen_gaps(topic, approved.version)
    print(f"gaps:     v{approved.version} {len(rows)} re-opened"
          + (" — " + "; ".join(row.line for row in rows) if rows else ""))
    return EXIT_OK


def _brief_recency(config, library: Library, topic: str, value: str) -> int:
    """Set the approved brief's recency (`ANY`, `N months`), or decide it now."""
    with Ledger(library.ledger_path) as ledger:
        if value.strip().lower() == "decide":
            approved = ledger.approved_brief(topic)
            if approved is None:
                return _error("recency not decided: no approved brief for this topic", EXIT_FAILED)
            if ledger.brief_recency(approved.topic, approved.version) is not None:
                print(_recency_status(ledger, approved.topic, approved.version))
                print("          already set; give --recency ANY or N months to change it")
                return EXIT_OK
            brief = run_brief(config, ledger, Judge(LlmClient(config.llm)), topic)
            client = LlmClient(config.llm)
            recency, territory = decide_recency(config, ledger, library, client, brief)
            if not recency.samples:
                return _error(
                    f"recency not decided: no sample voted ({territory.problem or 'no reason'}); "
                    "nothing stored", EXIT_FAILED,
                )
            print(_recency_status(ledger, approved.topic, approved.version))
            print(f"          {client.usage.requests} call(s), no search")
            return EXIT_OK
        row, problem = set_owner_recency(ledger, topic, value)
        if problem:
            return _error(f"recency not set: {problem}", EXIT_FAILED)
        print(_recency_status(ledger, row.topic, row.brief_version))
        return EXIT_OK


def _brief_outcome_lines(brief) -> list[str]:
    """Only when the brief was not made the ordinary way: one draft, reviewed.

    Rule 14. A retried draft and an unreviewed brief each change what every
    later verdict was measured against, and both used to be one log line at
    most — a draft cut off with nothing in it was not even that.
    """
    lines = []
    for number, problem in enumerate(brief.draft_problems, start=1):
        lines.append(
            f"          draft {number} unusable ({problem}); drawn again"
        )
    if brief.review_problem:
        lines.append(
            f"          review unusable ({brief.review_problem}); the unreviewed draft is the brief"
        )
    return lines


def _count_by_query(unread) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in unread:
        counts[entry.query] = counts.get(entry.query, 0) + 1
    return counts


def _count_by_item(unread) -> dict[str, int]:
    """What is waiting, by the coverage item it would serve, as `state: item`."""
    counts: dict[str, int] = {}
    for entry in unread:
        key = f"{entry.item_state}: {entry.item}" if entry.item else entry.item_state
        counts[key] = counts.get(key, 0) + 1
    return counts


def _gap_first_line(report) -> str | None:
    """What gap-first fetching did this round, on its own line, zeros included
    once the round had coverage to tag against (rule 14)."""
    if not report.held_cap and not any(report.tagged.get(state) for state in ITEM_STATES[:3]):
        return None
    tagged = ", ".join(f"{report.tagged.get(state, 0)} {state}" for state in ITEM_STATES)
    fetched = ", ".join(f"{report.fetched_by_state.get(state, 0)} {state}" for state in ITEM_STATES)
    return (
        f"          items: stage 1 tagged {tagged} ({report.tags_unmatched} unmatched); "
        f"fetched {fetched}; held cap {report.held_cap}, "
        f"{report.held_capped} left by it, {report.released} released past it, "
        f"{report.held_back_last} held back in the last round (release off)"
    )


def _by_source(counts: dict[str, int]) -> str:
    return ", ".join(f"{n} {origin}" for origin, n in sorted(counts.items())) or "0"


def _gap_follow_lines(report) -> list[str]:
    """What following the gaps took out of the round, zeros included once the
    round had coverage to follow (rule 14), and the gaps that became exhausted
    at its end, each with its query count. Nothing on a fresh topic."""
    counted = (report.held_while_gaps or report.exhausted_skipped or report.threads_held_back
               or report.exhausted or report.exhaustion_excluded)
    if not report.held_cap and not counted:
        return []
    lines = [
        f"          gaps: {sum(report.held_while_gaps.values())} quer(ies) on held parts "
        f"while a gap was open, not run ({_by_source(report.held_while_gaps)}); "
        f"{sum(report.exhausted_skipped.values())} on exhausted gaps, not run "
        f"({_by_source(report.exhausted_skipped)}); "
        f"{sum(report.threads_held_back.values())} thread quer(ies) on a concept only "
        f"keepers on no gap gave, held back ({_by_source(report.threads_held_back)}); "
        f"{report.exhaustion_excluded} quer(ies) not counted toward exhaustion, their "
        f"candidates blocked"
    ]
    for gap in report.exhausted:
        lines.append(
            f"          exhausted: {gap['line']} — {gap['queries']} quer(ies) over "
            f"{len(gap['rounds'])} round(s), no keeper"
        )
    return lines


def _discover_report(run: DiscoverRun) -> list[str]:
    lines = [_brief_line(run)]
    lines.extend(_brief_outcome_lines(run.brief))
    lines.extend(_territory_lines(run))
    lines.extend(_web_context_lines(run))
    lines.extend(_baseline_lines(run))
    for report in run.rounds:
        source = "queries from threads" if report.from_threads else "queries"
        line = (
            f"round {report.number}:  {len(report.queries)} {source}, "
            f"{report.candidates} candidates, {report.known} known, "
            f"{report.verdict_no} no, {report.verdict_maybe} maybe "
            f"→ {report.stored} stored"
            # The round's share of the video budget, when it had one.
            + (f" of {report.allowance} allowed" if report.allowance else "")
        )
        # Never silent, and never conflated. A round where every fetch failed
        # otherwise reads exactly like a round where the judge rejected
        # everything — observed in a real run, where 13 transient fetch
        # failures left no trace in the report at all.
        #
        # Each of these is a different thing that happened to a candidate, and
        # until B3 three of them shared two counters. B5's planner reads these
        # numbers; a sum of three causes cannot be planned on.
        notes = [
            (report.failed, "fetch failed"),
            (report.no_captions, "no captions, sent to ASR"),
            (report.translated_only, "only translated captions, sent to ASR"),
            (report.audio_label_disagreed, "stored from an original-speech track the audio label disagreed with"),
            (report.unavailable, "already recorded as unavailable, skipped"),
            (report.not_noteable, "kept, but the note worker will refuse it"),
            (report.stored_unreadable, "never noteable, stored off the video budget"),
            (report.already_held, "already on disk"),
            # Of those, the ones read from disk because nothing had judged them
            # for this topic. A verdict happened; a download did not.
            (report.judged_from_disk, "of them judged from disk"),
            (report.reconciled, "of them repaired from an interrupted store"),
            (report.unjudged, "kept without a stage-2 verdict"),
            (report.uncertain, "kept on a stage-2 MAYBE, not coverage"),
            (report.known_this_run, "stored in an earlier round"),
            (report.flipped_to_no, "kept on disk, declined here"),
            # Its own line, and the one worth reading. This is stage 1 having
            # been confidently wrong about a video the transcript then
            # contradicted — the number that says how far a metadata verdict
            # can be trusted on its own.
            (report.overturned, "stage-1 YES overturned by the transcript"),
            (report.degraded, "degraded evidence, not noted"),
            # Its own line, not folded into `degraded`: the transcript was fine
            # and the length was not, and one number for both would report a
            # ceiling as a caption failure.
            (report.too_long_to_note, "too long to note, not answerable"),
            (report.unparsed, "broke the reply format"),
            # Not part of the line above: those replies were well formed. This
            # is era knowledge carrying a verdict without being marked as
            # coming from outside the transcript.
            (report.unmarked_currency, "dated an API unmarked"),
            # Stage 1 this time: a NO the judge could state only as a guess,
            # kept as MAYBE (rule 8). Already part of the MAYBE count.
            (report.guessed_no, "stage-1 NO on a guess, kept as MAYBE"),
            # Stage 1 again, a different ground: a NO resting on what the
            # metadata did not show rather than on anything it did.
            (report.unshown_no, "stage-1 NO on missing evidence, kept as MAYBE"),
            # The expansion's own: a query whose reply named no aim, and one
            # dropped as the third or later on one aim.
            (report.expansion_unaimed, "expansion queries with no aim named"),
            (report.expansion_capped, "expansion queries over two on one aim, dropped"),
            (report.expansion_unmatched, "expansion aims matching no coverage line"),
            # The expansion's reply itself: refused (the round ran without it),
            # short, and the placeholder lines it held, never run.
            (report.expansion_refused, "expansion reply refused, round ran without it"),
            (report.expansion_short, "expansion reply short of the queries asked for"),
            (report.expansion_placeholders, "expansion placeholder line(s), not run"),
            # Never silent: judged worth reading, not fetched because this
            # round's share of the video budget ran out, carried forward.
            (report.unread_yes, "stage-1 YES not yet read, carried forward"),
            (report.unread_maybe, "stage-1 MAYBE not yet read, carried forward"),
            (report.read_from_earlier, "read from an earlier round's queue"),
            (report.already_waiting, "found again while queued, not re-screened"),
            (report.filters_rejected, "filters rejected"),
            # Named apart from the line above, because they are opposite
            # directions: a rejected filter widened a search that ran, a
            # skipped duplicate removed a search entirely.
            (report.duplicates_skipped, "duplicate queries skipped"),
            # Third direction again: not a refusal and not a skip, but the one
            # place a reply can still widen past the derived date window.
            (report.recency_waived, "queries waived the date window"),
        ]
        detail = ", ".join(f"{count} {label}" for count, label in notes if count)
        if detail:
            line += f" ({detail})"
        if report.rejected_before:
            line += f", {report.rejected_before} previously rejected"
        if report.rejected_this_run:
            line += f", {report.rejected_this_run} declined earlier in this run"
        lines.append(line)
        gap_first = _gap_first_line(report)
        if gap_first:
            lines.append(gap_first)
        lines.extend(_gap_follow_lines(report))
        lines.extend(_plan_lines(report))
    verb = "would store" if run.dry_run else "stored"
    lines.append(
        f"run:      {len(run.stored_ids)} {verb}"
        # Kept, stored, never noteable, and off the video budget: said beside
        # the stored count, never folded into it (rule 14).
        + (f" (+{len(run.kept_unreadable)} never noteable, off the budget)"
           if run.kept_unreadable else "")
        + f", {run.judged} judged, "
        f"{len(run.rounds)} rounds, {run.requests} requests, "
        f"stopped: {run.stopped_because}"
        + (f" — {run.stop_detail}" if run.stop_detail else "")
    )
    # The same shape citation parsing reports, for the same reason: a parser
    # that silently drops what it cannot read is how `/ask` came to report 2
    # citations out of 104. Printed even at zero — "0 unparsed" is the line
    # that makes the count trustworthy on the day it is not zero.
    lines.extend(_coverage_lines(run))
    read = sum(report.judged_here for report in run.rounds)
    unparsed = sum(report.unparsed for report in run.rounds)
    unmarked = sum(report.unmarked_currency for report in run.rounds)
    if read:
        lines.append(
            f"judge-full: {read} judged, {read - unparsed} parsed, {unparsed} unparsed"
        )
        # Printed even at zero, like the unparsed count beside it: the rate is
        # the whole point — era knowledge that only ever excludes is a judge
        # with a bias, and that cannot be seen from a number that appears only
        # when it is non-zero.
        lines.append(
            f"            {unmarked} dated an API without the outside-knowledge marker"
        )
    return lines


def _territory_lines(run: DiscoverRun) -> list[str]:
    """The map, the recency call and its reason, and what the map surfaced alone.

    The recency reason is printed rather than only stored, because it is the one
    judgement in this phase that silently *excludes* — a twelve-month cutoff
    removes older material with no trace anywhere a reader would look. A call
    you cannot inspect is one you cannot correct.
    """
    territory = run.territory
    if not territory and not run.rejected_filters:
        return []

    lines = []
    if territory.fallback:
        # A plan was asked for and the map ran instead. Its own line, before
        # the map's: a run that reads as an ordinary map run would hide that
        # the source it was configured to compare never searched.
        lines.append(f"plan:     not used — {territory.fallback}; the map wrote round one")
    if territory.source == SOURCE_PLAN:
        lines.extend(_search_plan_lines(territory))
    elif territory:
        lines.append(
            f"map:      {len(territory.concepts)} concepts, "
            f"{len(territory.vocabulary)} vocabulary terms, "
            f"{len(territory.queries)} queries"
        )
        reason = territory.recency.reason or "no reason given"
        lines.append(
            f"recency:  {territory.recency.label} — {reason}{recency_source(territory.recency)}"
        )
    if territory.problem:
        # Printed whenever there was a problem, not only when the map was lost:
        # a usable map can still have dropped a query or a recency vote.
        state = "problems" if territory else "unusable"
        label = "plan" if territory.source == SOURCE_PLAN else "map"
        lines.append(f"{label + ':':<10}{state} ({territory.problem})")

    # Printed even at zero, like the unparsed count, so the number is
    # trustworthy on the day it is not zero.
    #
    # Split the same way the round line is. This renderer had the same bug and
    # kept it one commit longer: a run whose only refusal was a duplicate query
    # announced "1 filter rejected before executing", which is the opposite of
    # what happened — no filter was refused, and one search did not run.
    refused = [
        item
        for item in run.rejected_filters
        if not item.is_duplicate_query and not item.is_recency_waiver
    ]
    duplicates = [item for item in run.rejected_filters if item.is_duplicate_query]
    waived = [item for item in run.rejected_filters if item.is_recency_waiver]
    lines.append(f"filters:  {len(refused)} rejected before executing")
    for rejection in refused[:5]:
        # Whether the refusal prevented an *exclusion* is what a reader wants
        # first. A rejected `order` cost that query some ranking; a rejected
        # `publishedAfter` or `videoDuration` stopped content disappearing
        # before anything in this system had seen it.
        excludes = " (would have excluded)" if rejection.field in HARD_FILTERS else ""
        lines.append(
            f"          {rejection.field}={rejection.value} on {rejection.query!r}"
            f"{excludes} — {rejection.reason}"
        )

    if waived:
        # Only when it happened, and never summed with the line above. This is
        # the reply asking for a *wider* search than the derived window would
        # have run — the opposite direction from a refusal, and the only
        # remaining way the date filter can be kept off a main query.
        lines.append(
            f"waived:   {len(waived)} quer(ies) asked for any age despite the window"
        )
        for waiver in waived[:5]:
            lines.append(f"          {waiver.query!r} — {waiver.reason}")

    if duplicates:
        # Its own line, and only when it happened. A duplicate skipped is a
        # search that did not run because an identical one already had — worth
        # seeing, and not a filter refusal.
        lines.append(
            f"dupes:    {len(duplicates)} quer(ies) skipped as already run this round"
        )

    origin, label = (
        (ORIGIN_PLAN, "plan-only:") if territory.source == SOURCE_PLAN
        else (ORIGIN_TERRITORY, "map-only:")
    )
    exclusive = [
        video_id
        for report in run.rounds
        for video_id in report.only_from(origin)
    ]
    if run.rounds:
        # The phase's value in one number: candidates the map's own queries
        # surfaced that no brief-derived query returned. A video the search
        # never returns is invisible to every stage after it, so this is the
        # whole difference the map makes.
        lines.append(
            f"{label:<10}{len(exclusive)} candidate(s) no brief query surfaced"
        )
    return lines


def _web_context_lines(run: DiscoverRun) -> list[str]:
    """The start-of-run web lookup's line (rule 14): lookups, results, lines
    kept and dropped with reasons, domains, cost — or why there was none."""
    context = run.web_context
    if context.status == "off":
        # Switched off, nothing ran: no line, as a run with no map prints no map
        # line (`--json` still says `off`). Asked for on a map run, it says why.
        return [f"context:  off ({context.reason})"] if context.reason and \
            context.reason != "web_context is off" else []
    dropped = ", ".join(f"{n} {why}" for why, n in sorted(context.dropped.items()))
    refused = ", ".join(f"{n} {why}" for why, n in sorted(context.queries_refused.items()))
    errors = sum(1 for lookup in context.lookups if lookup.error)
    lines = [
        f"context:  {context.status} — {len(context.lookups)} lookup(s) via {context.provider or '?'}, "
        f"{context.results} result(s), {len(context.lines)} line(s) kept, "
        f"{sum(context.dropped.values())} dropped{' (' + dropped + ')' if dropped else ''}; "
        f"{len(context.domains)} domain(s); {context.searches_billed} billed search(es) "
        f"${context.search_cost_usd:.4f} + {context.model_requests} model call(s)",
    ]
    if refused:
        lines.append(f"          queries refused: {refused}")
    if errors:
        lines.append(f"          {errors} lookup(s) failed")
    if context.reason:
        lines.append(f"          {context.reason}")
    return lines


def _search_plan_lines(territory) -> list[str]:
    """The search plan's line: items, queries per item, languages, and the
    brief's PARTS no query reaches — each counter printed at zero too, so it
    can be trusted on the day it is not zero (rule 14)."""
    plan = territory.plan
    if plan is None:
        return []
    per_item = "/".join(str(len(item.queries)) for item in plan.items) or "none"
    lines = [
        f"plan:     {len(plan.items)} items, {len(territory.queries)} queries "
        f"(per item {per_item}), prompt {territory.version}",
        f"recency:  {territory.recency.label} — {territory.recency.reason or 'no reason given'}"
        f"{recency_source(territory.recency)}",
    ]
    languages = "; ".join(
        f"{language.name} ({language.code or 'no code'}, {language.script or 'script not given'})"
        for language in plan.languages
    )
    lines.append(f"          languages: {languages or 'none named'}")
    # What the plan was told the library holds, and where the queries went.
    lines.append(f"          coverage: {plan.coverage} — {plan.coverage_note or 'no note'}")
    by_state = ", ".join(f"{state} {n}" for state, n in plan.queries_by_state.items()) or "none"
    lines.append(f"          by state: {by_state}")
    by_tier = ", ".join(f"{tier} {n}" for tier, n in plan.queries_by_tier.items()) or "none"
    lines.append(
        f"          by tier: {by_tier} ({plan.distinct_gaps} distinct gap(s), "
        f"{plan.duplicates_avoided} duplicate(s) avoided, "
        f"{plan.second_gap_queries} second gap quer(ies), "
        f"{plan.unfilled} budget slot(s) unfilled)"
    )
    named = plan.named_queries
    kinds = ", ".join(f"{sum(1 for q in named if q['kind'] == k)} {k}" for k in ("case", "term"))
    refused_named = ", ".join(f"{n} {why}" for why, n in sorted(plan.named_refused.items()))
    lines.append(
        f"          named from context: {len(named)} ({kinds}; cap {plan.named_cap})"
        + (f"; refused: {refused_named}" if refused_named else "")
    )
    for q in named:
        lines.append(f"            {q['line']} [{q['gap']}] {q['kind']}: {q['query']!r} <- ({q['domain']})")
    if plan.context == "supplied":
        lines.append(
            f"          recency: the brief's {territory.recency.label}; the context sample said "
            f"{plan.context_recency or '?'}{' (overridden)' if plan.recency_overridden else ''}"
        )
    lines.append(f"          skipped as excluded: {len(plan.excluded_gaps)}")
    for skipped in plan.excluded_gaps[:5]:
        line, _, reason = skipped.partition(" | ")
        lines.append(f"            {line} — {reason}")
    by_language = ", ".join(f"{code} {n}" for code, n in plan.queries_by_language.items()) or "none"
    aimed = ", ".join(f"P{n}" for n in plan.language_items) or (
        f"none, so the broad item P{plan.broad_item}" if plan.broad_item else "none named"
    )
    lines.append(
        f"          by language: {by_language} (main {plan.main_language or 'not named'}; "
        f"other languages aim at {aimed})"
    )
    if plan.unserved_gaps:
        lines.append(
            f"          gaps with no query: {len(plan.unserved_gaps)} — "
            + "; ".join(plan.unserved_gaps[:5])
        )
    missing = ", ".join(plan.parts_without_query)
    lines.append(
        f"          no query: {len(plan.parts_without_query)} part(s)"
        + (f" — {missing}" if missing else "")
    )
    for count, what in (
        (len(plan.unknown_items), "item(s) naming no PART of the brief, not planned"),
        (plan.queries_dropped, "quer(ies) under those items, not run"),
        (plan.things_unmatched, "listed thing(s) the brief does not list, tagged by PART"),
        (plan.script_mismatches, "quer(ies) not in the script their language's titles use"),
        (len(plan.languages_without_query), "language(s) with no query"),
        (plan.held_while_gaps, "quer(ies) on held items while a gap had none"),
        (plan.held_while_thin, "quer(ies) on held items while a gap had only one"),
        (plan.language_misaimed, "quer(ies) in another language aimed at a narrow part"),
        (plan.over_budget, "quer(ies) over the budget, not run, held first"),
        (plan.coverage_unmatched, "item(s) matching no coverage line"),
    ):
        if count:
            lines.append(f"          {count} {what}")
    context = (
        f"{plan.context_lines} untrusted line(s)" if plan.context == "supplied" else plan.context
    )
    lines.append(f"          context: {context}")
    return lines


def _baseline_lines(run: DiscoverRun) -> list[str]:
    """What the library already held, on its own line, degraded or not (rule
    14): the planner treats what this says is held as held, and a cut evidence
    mode holds less. Nothing for a run whose library held no notes."""
    lines: list[str] = []
    baseline = run.baseline
    if baseline.notes_read:
        partly = sum(1 for item in baseline.items if item.partly)
        line = (
            f"baseline: {len(baseline.satisfied)} of {len(baseline.items)} brief items held, "
            f"{partly} partly, from {baseline.notes_read} note(s); evidence "
            f"{baseline.evidence_mode or 'unknown'}, ~{baseline.evidence_tokens:,} tokens"
        )
        if baseline.degraded:
            line += f" — DEGRADED: {baseline.notes_cut} notes' claims cut to {baseline.claims_chars} chars"
        if not baseline.parsed and baseline.problem:
            line += f"; reply had problems ({baseline.problem})"
        lines.append(line)
        lines.append(f"          {baseline_reuse_line(baseline)}")
        lines.append(f"          {_left_out_line(baseline)}")
    elif not run.dry_run:
        lines.append("baseline: none — the library holds no note this topic's stage 2 kept, so "
                     "this is planned as a fresh topic, with no call")
        lines.append(f"          {_left_out_line(baseline)}")
    return lines


def _left_out_line(baseline) -> str:
    """The notes the evidence left out, by kind, zeros included (rule 14)."""
    left = baseline.notes_left_out
    return (f"notes left out (not this topic's stage-2 YES): {left.get('no', 0)} NO, "
            f"{left.get('maybe', 0)} MAYBE, {left.get('unjudged', 0)} not judged for this topic")


def recency_source(recency) -> str:
    """Where a run's window came from, after its reason (empty before it was recorded)."""
    samples = f" from samples {recency.samples_label}" if recency.samples else ""
    run = f" by run {recency.stored_from}" if recency.stored_from else ""
    if recency.source == RECENCY_OWNER:
        return " [owner-set for this brief version; reused]"
    if recency.source == RECENCY_DECIDED:
        return f" [the brief version's, decided{run}{samples}; stored]"
    if recency.source == RECENCY_SAMPLED:
        return " [this run's own samples: no approved brief, or no sample voted]"
    return ""


def baseline_reuse_line(baseline) -> str:
    """Whether the baseline was made by this run or reused, and what it cost."""
    if baseline.reuse == BASELINE_REUSED:
        return f"reused (from run {baseline.reused_from or 'unknown'}): unchanged state, 0 calls"
    if baseline.reuse == BASELINE_FRESH:
        why = f" ({baseline.fresh_reason})" if baseline.fresh_reason else ""
        return (f"fresh{why}: {baseline.calls} call(s), "
                f"{baseline.flips_resolved} flip(s) resolved toward missing")
    return "fresh: 1 call (made before baselines were stored)"


def _plan_lines(report: RoundReport) -> list[str]:
    """What the planner decided at the end of one round, in two lines at most.

    Printed per round rather than only at the end because the interesting thing
    is the *change*: round two searching for something round one did not is the
    whole claim of this phase, and a reader should be able to see the gap that
    produced it on the line above.
    """
    plan = report.plan
    if plan is None:
        return []

    lines = []
    gaps = plan.gaps
    summary = f"{len(plan.satisfied)} of {len(plan.coverage)} brief items covered"
    partly = sum(1 for item in plan.coverage if item.partly)
    if partly:
        summary += f", {partly} partly"
    if gaps:
        summary += f"; gap: {gaps[0].part}"
        if len(gaps) > 1:
            summary += f" (+{len(gaps) - 1} more)"
    lines.append(f"  plan:   {summary} → {plan.recommendation}: {plan.reason}")

    if plan.queries:
        lines.append("  next:   " + ", ".join(query.text for query in plan.queries[:6]))
    for revision in plan.revisions:
        lines.append(f"  map:    {revision.line()}")
    # Refusals are never silent, for the same reason a rejected filter is not:
    # a proposal the code declined and nobody counted is indistinguishable from
    # a proposal that was never made.
    for revision, why in plan.refused_revisions:
        lines.append(f"  map:    refused {revision.line()} — {why}")
    if not plan.parsed and plan.problem:
        lines.append(f"  plan:   reply had problems ({plan.problem})")
    return lines


def _coverage_lines(run: DiscoverRun) -> list[str]:
    """The final coverage assessment: what the brief still has nothing against.

    The last plan wins — it is the one that saw the most. Printed as its own
    block because "which part of the brief did this run fail to satisfy?" is
    the question a library is judged on, and it should not need the database.
    """
    plans = [report.plan for report in run.rounds if report.plan is not None]
    if not plans:
        return []
    final = plans[-1]
    if not final.coverage:
        return []

    lines = [f"coverage: assessed at the end of round {final.round_number}"]
    # What is still queued for each part, so a gap with candidates waiting
    # reads differently from one with nothing found.
    waiting: dict[str, int] = {}
    for entry in run.backlog:
        part = entry.item.split(" — ", 1)[0] if entry.item else ""
        waiting[part] = waiting.get(part, 0) + 1
    for item in final.coverage:
        mark = {"yes": "yes", "partly": "part", "no": "NO "}[item.mark]
        detail = f" — {item.detail}" if item.detail else ""
        queued = f" [{waiting[item.part]} waiting]" if waiting.get(item.part) else ""
        lines.append(f"  {mark}  {item.part}{detail}{queued}")
    if waiting.get(""):
        lines.append(f"  ({waiting['']} waiting on no item)")
    # Tagged against an earlier round's wording of a part: still counted.
    parts = {item.part for item in final.coverage}
    elsewhere = sum(count for part, count in waiting.items() if part and part not in parts)
    if elsewhere:
        lines.append(f"  ({elsewhere} waiting on parts an earlier round worded differently)")
    return lines


def _discover_json(run: DiscoverRun) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "topic": run.topic,
        "model": run.model,
        "dry_run": run.dry_run,
        "brief": run.brief.text,
        "brief_version": run.brief.version,
        "brief_intent": run.brief.intent,
        # How the brief was made: draft attempts and why any failed, and
        # whether the review was used. Also stored on the run.
        "brief_outcome": run.brief.outcome,
        # The map in full, and what it cost. Same argument as printing the
        # brief: when results look wrong the first question is what the run
        # thought this topic covered, and a list of queries is the conclusion
        # rather than the reasoning.
        # Which source wrote round one's planned queries, and why the map ran
        # when a plan was asked for. The plan itself is under `territory.plan`.
        "search_source": run.territory.source,
        # Step 4: what the start-of-run web lookup searched and what the plan
        # was shown. Stored on the ledger too (`web_contexts`).
        "web_context": run.web_context.as_dict(),
        # What the library held before round one, and whether this run made it
        # (`reuse: fresh`, with its calls and flips resolved) or reused a
        # stored one on the same state (`reuse: reused`, `reused_from`).
        "baseline": run.baseline.as_dict(),
        "search_fallback": run.territory.fallback,
        "territory_version": run.territory.label,
        "territory_revision": run.territory.revision,
        "territory": json.loads(run.territory.as_json()),
        "territory_call": {
            "input_tokens": run.territory.input_tokens,
            "output_tokens": run.territory.output_tokens,
            "seconds": round(run.territory.latency_s, 2),
        },
        "rejected_filters": [
            {
                "query": rejection.query,
                "field": rejection.field,
                "value": rejection.value,
                "reason": rejection.reason,
            }
            for rejection in run.rejected_filters
        ],
        "stopped_because": run.stopped_because,
        "stop_detail": run.stop_detail,
        "fetch_block": dict(run.fetch_block),
        # Every gap's account: queries aimed at it, keepers on it, by round, and
        # the round it became exhausted in (0: it did not).
        "gaps": [tally.as_dict() for tally in run.gaps],
        "fetched": run.fetched,
        "requests": run.requests,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "candidates_seen": run.candidates_seen,
        "judged": run.judged,
        "stored": run.stored_ids,
        "kept_unreadable": run.kept_unreadable,
        "rounds": [
            {
                "number": report.number,
                "from_threads": report.from_threads,
                "expansion_unaimed": report.expansion_unaimed,
                "expansion_capped": report.expansion_capped,
                "expansion_unmatched": report.expansion_unmatched,
                "expansion_refused": report.expansion_refused,
                "expansion_short": report.expansion_short,
                "expansion_placeholders": report.expansion_placeholders,
                "stored_unreadable": report.stored_unreadable,
                "queries": report.queries,
                # The same queries with their provenance. `queries` above stays
                # a bare list of strings: it is what every earlier baseline
                # carries, and a comparison against one should not need a
                # reader to learn a new shape.
                "planned": [
                    {
                        "text": query.text,
                        "origin": query.origin,
                        "origin_detail": query.origin_detail,
                        "filters": query.filters.as_dict(),
                        "variant_of": query.variant_of,
                    }
                    for query in report.planned
                ],
                "filters_rejected": report.filters_rejected,
                "duplicates_skipped": report.duplicates_skipped,
                "recency_waived": report.recency_waived,
                "unmarked_currency": report.unmarked_currency,
                "guessed_no": report.guessed_no,
                "unshown_no": report.unshown_no,
                # The round's share of the video budget, and what it left unread,
                # by the query that found each — so a reader can see which parts
                # of the search were read and which were carried.
                "allowance": report.allowance,
                "unread_yes": report.unread_yes,
                "unread_maybe": report.unread_maybe,
                "read_from_earlier": report.read_from_earlier,
                "already_waiting": report.already_waiting,
                "unread_by_query": _count_by_query(report.unread),
                # Gap-first fetching: stage 1's tags by item state, the tags that
                # matched no coverage line, fetches by state, the held cap and
                # what it left and released, and the queue by item.
                "tagged": {state: report.tagged.get(state, 0) for state in ITEM_STATES},
                "tags_unmatched": report.tags_unmatched,
                "fetched_by_state": {
                    state: report.fetched_by_state.get(state, 0) for state in ITEM_STATES
                },
                "held_cap": report.held_cap,
                "held_capped": report.held_capped,
                "released": report.released,
                "fetched": report.fetched,
                # Following the gaps, per source, and what became exhausted.
                "held_while_gaps": dict(report.held_while_gaps),
                "exhausted_skipped": dict(report.exhausted_skipped),
                "threads_held_back": dict(report.threads_held_back),
                "held_concepts": list(report.held_concepts),
                "held_back_last": report.held_back_last,
                "exhaustion_excluded": report.exhaustion_excluded,
                "exhausted": list(report.exhausted),
                "unread_by_item": _count_by_item(report.unread),
                # What the planner concluded at the end of this round, in full:
                # its coverage assessment, the queries it wrote for the next
                # round, the map revisions it proposed and the ones the code
                # refused. `None` on the last round and on a dry run, where no
                # plan was made rather than an empty one.
                "plan": report.plan.as_dict() if report.plan else None,
                # Which origins' queries returned each candidate, and the
                # subset only the map returned — the phase's value, per round.
                "surfaced_by": {
                    video_id: sorted(origins)
                    for video_id, origins in report.surfaced_by.items()
                },
                "territory_only": sorted(report.only_from(ORIGIN_TERRITORY)),
                "plan_only": sorted(report.only_from(ORIGIN_PLAN)),
                # What stage 2 found unexplained in this round's videos. These
                # are what the *next* round is supposed to have searched for,
                # so they are the only way to check the loop-back really fed
                # forward rather than the brief being re-expanded.
                "concepts": report.concepts,
                "candidates": report.candidates,
                "known": report.known,
                "known_this_run": report.known_this_run,
                "rejected_before": report.rejected_before,
                "rejected_this_run": report.rejected_this_run,
                "yes": report.verdict_yes,
                "no": report.verdict_no,
                "maybe": report.verdict_maybe,
                "flipped_to_no": report.flipped_to_no,
                "overturned": report.overturned,
                "stored": report.stored,
                "failed": report.failed,
                "already_held": report.already_held,
                "judged_from_disk": report.judged_from_disk,
                "reconciled": report.reconciled,
                "unjudged": report.unjudged,
                "uncertain": report.uncertain,
                "no_captions": report.no_captions,
                "translated_only": report.translated_only,
                "audio_label_disagreed": report.audio_label_disagreed,
                "unavailable": report.unavailable,
                "not_noteable": report.not_noteable,
                "degraded": report.degraded,
                "too_long_to_note": report.too_long_to_note,
                "unparsed": report.unparsed,
            }
            for report in run.rounds
        ],
    }


def _replace_translated(args: argparse.Namespace) -> int:
    """Rule 3's one exception: replace transcripts rule 40 shows to be
    translations, and clean stored track ids. yt-dlp only — no search quota,
    no model calls. ``--dry-run`` judges every video and writes nothing."""
    from .contracts import (
        REPAIR_ACCEPTED,
        REPAIR_FAILED,
        REPAIR_LANGUAGE_CLEANED,
        REPAIR_REPLACED,
        REPAIR_SKIPPED,
    )

    config = _load(args)
    library = Library(config.library_path)
    library.ensure()
    ids = [extract_video_id(video) or video for video in args.video]
    with Ledger(library.ledger_path) as ledger:
        run = replace_translated(config, ledger, library, video_ids=ids,
                                 dry_run=args.dry_run, limit=args.limit)
    verb = {REPAIR_REPLACED: "would replace" if run.dry_run else "replaced",
            REPAIR_LANGUAGE_CLEANED: "would clean" if run.dry_run else "cleaned",
            REPAIR_SKIPPED: "skipped", REPAIR_FAILED: "failed"}
    for result in run.results:
        if result.action == REPAIR_ACCEPTED:
            continue
        change = f"{result.old_language or '?'} -> {result.new_language or '?'}"
        track = f" ({result.new_track})" if result.new_track else ""
        _report(verb[result.action], result.video_id, change + track, result.detail or None)
    skipped: dict[str, int] = {}
    for result in run.results:
        if result.action == REPAIR_SKIPPED:
            skipped[result.detail] = skipped.get(result.detail, 0) + 1
    reasons = "; ".join(f"{n} {why}" for why, n in sorted(skipped.items()))
    print(
        f"repair:   {run.considered} considered{' (dry run, nothing written)' if run.dry_run else ''} — "
        f"{run.count(REPAIR_REPLACED)} replaced, {run.count(REPAIR_LANGUAGE_CLEANED)} language cleaned, "
        f"{run.count(REPAIR_ACCEPTED)} accepted, {run.count(REPAIR_SKIPPED)} skipped"
        f"{' (' + reasons + ')' if reasons else ''}, {run.count(REPAIR_FAILED)} failed; "
        f"span guard {MIN_SPAN_SHARE:.0%}; {_elapsed(run.elapsed_s)}, stopped: {run.stopped_because}"
    )
    return EXIT_FAILED if run.count(REPAIR_FAILED) else EXIT_OK


def _cmd_refetch(args: argparse.Namespace) -> int:
    """Retry the transcript fetches that failed transiently.

    Uses no search quota — this is transcript fetching, not the search API — and
    makes no model calls at all.
    """
    if args.replace_translated:
        return _replace_translated(args)
    if args.video:
        return _error("video ids are taken only with --replace-translated", EXIT_FAILED)
    config = _load(args)
    library = Library(config.library_path)
    library.ensure()
    with Ledger(library.ledger_path) as ledger:
        run = refetch(config, ledger, library, limit=args.limit, dry_run=args.dry_run)

    if not run.considered:
        print("refetch:  nothing to retry — no transient failures on the ledger")
        return EXIT_OK

    if run.dry_run:
        wanted = sum(1 for r in run.results if r.wanted)
        print(
            f"refetch:  {run.considered} transient failure(s) would be retried, "
            f"{wanted} of them judged YES by some run (those go first)"
        )
        for result in run.results[: args.limit or 20]:
            mark = "wanted" if result.wanted else "      "
            print(f"  {mark}  {result.video_id}  {result.attempts} attempt(s) so far")
        if len(run.results) > (args.limit or 20):
            print(f"  … and {len(run.results) - (args.limit or 20)} more")
        return EXIT_OK

    for result in run.results:
        if result.outcome == RECOVERED:
            print(f"recovered {result.video_id}")
    parts = [
        (run.count(RECOVERED), "recovered"),
        (run.count(STILL_FAILING), "failed again"),
        (run.count(NO_CAPTIONS), "no captions, routed to ASR"),
        (run.count(TRANSLATED_ONLY), "only translated captions, routed to ASR"),
        (run.count(GONE), "gone (the provider says permanently)"),
    ]
    detail = ", ".join(f"{n} {label}" for n, label in parts if n)
    print(
        f"run:      {len(run.results)} of {run.considered} tried"
        f"{' — ' + detail if detail else ''}, {_elapsed(run.elapsed_s)}, "
        f"stopped: {run.stopped_because}"
    )

    # Reported, never acted on. No threshold turns `transient` into permanent:
    # a video written off is rule 8's invisible error with extra steps, and an
    # attempt count cannot tell a deleted video from an unlucky one.
    stubborn = sorted(
        (r for r in run.results if r.attempts >= 3 and r.outcome == STILL_FAILING),
        key=lambda r: -r.attempts,
    )
    if stubborn:
        print(
            f"          {len(stubborn)} video(s) have now failed 3+ times and are "
            f"worth a look — still retryable, nothing was written off:"
        )
        for result in stubborn[:5]:
            print(f"            {result.video_id}  {result.attempts} attempts")
    if run.count(RECOVERED):
        print("          recovered videos have no note yet — run `marg note`")
    return EXIT_FAILED if run.stopped_because == STOP_THROTTLED else EXIT_OK


def _cmd_note(args: argparse.Namespace) -> int:
    """Generate notes for stored videos.

    Note contents never reach stdout, for exactly the reason transcripts do
    not. A note is ten times smaller than a transcript and thirty-six of them
    still fill a context window, which is the cost curve this whole design
    exists to avoid.
    """
    config = _load(args)
    library = Library(config.library_path)
    library.ensure()

    if args.status:
        return _note_status(config, library)

    video_ids: list[str] = []
    for value in args.video or []:
        video_id = extract_video_id(value)
        if video_id is None:
            return _error(f"not a YouTube URL or video ID: {value!r}", EXIT_USAGE)
        video_ids.append(video_id)

    client = LlmClient(config.llm)
    with Ledger(library.ledger_path) as ledger:
        run = write_notes(
            config,
            ledger,
            library,
            client,
            video_ids=video_ids,
            limit=args.limit,
            regenerate=args.regenerate,
            missing_only=args.missing_only,
            every=args.all,
        )

    for result in run.results:
        _report(
            result.outcome,
            result.video_id,
            result.detail,
            f"v{result.version}" if result.version and result.outcome == NOTED else None,
        )
    print(
        f"run:      {run.count(NOTED)} noted, {run.count(SKIPPED)} skipped, "
        f"{run.count(FAILED)} failed, {run.requests} requests, "
        f"${run.cost_usd:.2f}, {_elapsed(run.elapsed_s)}"
        + (f", stopped: {run.stopped_because}" if run.stopped_because != "done" else "")
    )
    checked = sum(result.verbatim_checked for result in run.results)
    if checked:
        # Printed whenever anything was checked, zero included: "0 not found"
        # is the line that makes the count trustworthy on the day it is not.
        print(
            f"          {sum(r.snapped for r in run.results)} timestamp(s) snapped; "
            f"{sum(r.verbatim_unfound for r in run.results)} of {checked} verbatim "
            f"quote(s) not found in the transcript, marked in the note; "
            f"{sum(r.verbatim_untranslated for r in run.results)} Verbatim line(s) in "
            f"another script than the transcript with no translation marker"
        )
    timed = [result for result in run.results if result.timestamps.total or result.redrawn]
    if timed:
        # Rule 14: timestamps that name no moment in the video, on their own
        # line, printed whenever any were counted, zero included — a note whose
        # citations all pointed past its end used to be reported exactly like a
        # good one.
        print(
            f"          timestamps: {sum(r.timestamps.unplaceable for r in timed)} of "
            f"{sum(r.timestamps.total for r in timed)} name no moment in the video, in "
            f"{sum(1 for r in timed if r.timestamps.unplaceable)} note(s); "
            f"{sum(r.timestamps.in_tolerance for r in timed)} within the caption tolerance "
            f"past the listed end; {sum(1 for r in timed if r.redrawn)} note(s) drawn twice, "
            f"{sum(1 for r in timed if r.redrawn and r.outcome == FAILED)} of them not written"
        )
    outside = [result for result in run.results if result.quotes_outside]
    if outside:
        # Rule 14: quotes outside Verbatim are counted, not marked — the same
        # question the Verbatim check asks, asked of every other section.
        total = sum(f + m for r in outside for f, m in r.quotes_outside.values())
        missing = sum(m for r in outside for _f, m in r.quotes_outside.values())
        by_script = {}
        for r in outside:
            for script, (f, m) in r.quotes_outside_script.items():
                found, missed = by_script.get(script, (0, 0))
                by_script[script] = (found + f, missed + m)
        split = "; ".join(
            f"{script}: {m} of {f + m} not found"
            for script, (f, m) in sorted(by_script.items(), key=lambda item: item[0] != "same script")
        )
        print(
            f"          quotes outside Verbatim: {missing} of {total} not found in the "
            f"transcript, in {sum(1 for r in outside if any(m for _f, m in r.quotes_outside.values()))} "
            f"note(s); counted, not marked" + (f" ({split})" if split else "")
        )
    settled = [result for result in run.results if result.outcome == NOTED]
    if any(result.marker_times[0] for result in settled):
        # Rule 33: a time difference a marker states, checked against its own
        # line's timestamps. Counted, never rewritten.
        print(
            f"          marker times: {sum(r.marker_times[1] for r in settled)} of "
            f"{sum(r.marker_times[0] for r in settled)} stated time difference(s) "
            f"contradicted by their line's timestamps, in "
            f"{sum(1 for r in settled if r.marker_times[1])} note(s); counted, not changed"
        )
    if any(result.markers.lines_checked for result in settled):
        # Rule 14: marker boundaries that cut a quotation, and which way each
        # was resolved — printed whenever a line held both, zero included.
        print(
            f"          quotations: {sum(r.markers.straddles for r in settled)} marker "
            f"boundary(ies) cut one: {sum(r.markers.kept_inside for r in settled)} kept inside, "
            f"{sum(r.markers.put_outside for r in settled)} put outside, "
            f"{sum(r.markers.refused for r in settled)} refused; "
            f"{sum(r.markers.single_quoted_unhandled for r in settled)} single-quoted unit(s) "
            f"not handled; {sum(r.markers.lines_checked for r in settled)} line(s) held a "
            f"marker and a quotation"
        )
    ran = [result for result in run.results if result.mark_ran]
    if ran:
        # Rule 14: what the marking pass did, on its own line, zero included.
        failed_passes = [result for result in ran if result.mark_problem]
        capped_passes = [result for result in ran if result.mark_capped]
        print(
            f"          marking pass: {sum(r.marked for r in ran)} statement(s) marked in "
            f"{len(ran)} note(s); {sum(r.mark_rejected for r in ran)} listed span(s) not "
            f"applied; {len(failed_passes)} pass(es) failed, those notes kept as written; "
            f"{len(capped_passes)} pass(es) hit the span cap, "
            f"{sum(r.mark_capped for r in ran)} span(s) past it left unmarked"
        )
    # A refusal stopped the drain before it reached the rest of the queue, and
    # it records no result for the video it was on (rule 24) — so `failed`
    # stays 0 and this used to exit 0, which a script reads as "every note is
    # written". The refusal is the user's to fix, and the exit code says so.
    if run.stopped_because == STOP_REFUSED:
        return EXIT_FAILED
    return EXIT_FAILED if run.count(FAILED) else EXIT_OK


def _note_status(config: Config, library: Library) -> int:
    version = note_version()
    with Ledger(library.ledger_path) as ledger:
        counts = ledger.note_counts(version=version)
    print(
        f"notes:    {counts['has_transcript']} with transcript, {counts['current']} current, "
        f"{counts['stale']} stale, {counts['missing']} missing, "
        f"{counts['unreadable']} unreadable, {counts['unmarked']} unmarked  (prompt v{version})"
    )
    return EXIT_OK


def _elapsed(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def _cmd_llm_check(args: argparse.Namespace) -> int:
    """One request against the configured provider. The first thing to run
    after swapping providers, and it must cost exactly one request."""
    config = _load(args)
    client = LlmClient(config.llm)

    started = time.monotonic()
    try:
        reply = client.complete(
            "Answer in one word.",
            "Reply with exactly the word: ready",
        )
    except LlmError as exc:
        return _error(f"{config.llm.provider}: {exc}", EXIT_USAGE)

    elapsed = time.monotonic() - started
    parsed = "ready" in reply.text.lower()
    _report("llm", config.llm.provider, reply.model)
    print(
        f"          {elapsed:.1f}s  "
        f"in={reply.usage.input_tokens} out={reply.usage.output_tokens}  "
        f"parse={'ok' if parsed else 'FAILED'}  base_url={config.llm.base_url}"
    )
    if not parsed:
        print(f"          reply was: {_quoted(reply.text, 60)}", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK


def _cmd_status(args: argparse.Namespace) -> int:
    video_id = extract_video_id(args.video)
    if video_id is None:
        return _error(f"not a YouTube URL or video ID: {args.video!r}", EXIT_USAGE)

    config = _load(args)
    library = Library(config.library_path)
    library.ensure()

    with Ledger(library.ledger_path) as ledger:
        row = ledger.get(video_id)

    if row is None:
        _report("unknown", video_id, "not in ledger")
        return EXIT_OK

    _report(row.status.replace("_", "-"), video_id, *_describe(library, row))
    return EXIT_OK


def _cmd_list(args: argparse.Namespace) -> int:
    config = _load(args)
    library = Library(config.library_path)
    library.ensure()

    with Ledger(library.ledger_path) as ledger:
        rows = ledger.list_by_status(args.status)

    # One line per video and nothing else: a skill parses this, so an empty
    # library must produce zero lines rather than a sentence it has to
    # distinguish from a result. Exit 0 already says the command succeeded.
    for row in rows:
        meta = library.read_meta(row.video_id) or {}
        _report(row.status.replace("_", "-"), row.video_id, _quoted(meta.get("title")))
    if not rows:
        log.info("no videos with status %s", args.status)
    return EXIT_OK


def _cmd_ask(args: argparse.Namespace) -> int:
    """Answer a question from the library and print the finished answer.

    The one command that deliberately prints a lot. Everything else here
    returns a line, because everything else is about material the conversation
    must not hold; this returns the *product* — prose written from the notes,
    with a link per claim. Notes and transcripts stay inside Python either way.
    """
    config = _load(args)
    library = Library(config.library_path)
    library.ensure()

    deep: list[str] = []
    for value in args.deep or []:
        video_id = extract_video_id(value)
        if video_id is None:
            return _error(f"not a YouTube URL or video ID: {value!r}", EXIT_USAGE)
        deep.append(video_id)

    question = " ".join(args.question.split())
    if not question:
        return _error("ask what?", EXIT_USAGE)

    client = LlmClient(config.llm)
    with Ledger(library.ledger_path) as ledger:
        answer = answer_question(
            config, library, client, question, ledger=ledger, deep=deep
        )

    # Saved before anything is printed, and saved even when the answer is empty
    # or the model call failed. A failed answer is the one you most want a
    # record of: it carries the cost, the prompt version and the notes that were
    # loaded, which is the whole of what you would otherwise be guessing at.
    saved = _save_answer(library, answer, no_save=args.no_save)

    if args.json:
        print(
            json.dumps(
                {
                    "question": answer.question,
                    "saved_to": str(saved) if saved else None,
                    "answer": answer.text,
                    "prompt_version": answer.version,
                    "model": answer.model,
                    "notes_loaded": answer.notes_loaded,
                    "notes_considered": answer.notes_considered,
                    "selection": {
                        "mode": answer.selection.mode,
                        "reason": answer.selection.reason,
                        "video_ids": answer.selection.video_ids,
                        "requested_drops": answer.selection.requested_drops,
                        "allowed_drops": answer.selection.allowed_drops,
                        "refused_drops": answer.selection.refused_drops,
                        "overflowed": answer.selection.overflowed,
                        "parsed": answer.selection.parsed,
                        "problem": answer.selection.problem,
                    },
                    "estimated_input_tokens": answer.estimated_input_tokens,
                    "headroom": answer.headroom,
                    "opened": [
                        {"video_id": video_id, "why": why} for video_id, why in answer.opened
                    ],
                    "deep_unselected": answer.deep_unselected,
                    "deep_skipped": [
                        {"video_id": video_id, "why": why}
                        for video_id, why in answer.deep_skipped
                    ],
                    "escalations_refused": [
                        {"video_id": video_id, "why": why}
                        for video_id, why in answer.escalations_refused
                    ],
                    "transcripts_cut": [
                        {"video_id": video_id, "given_chars": given, "total_chars": total}
                        for video_id, given, total in answer.transcripts_cut
                    ],
                    "transcript_tokens": answer.transcript_tokens,
                    "citation_shaped": len(answer.citations) + len(answer.unparsed),
                    "unparsed": answer.unparsed,
                    "citations": [
                        {
                            "video_id": citation.video_id,
                            "seconds": citation.seconds,
                            "url": deep_link(citation.video_id, citation.seconds),
                        }
                        for citation in answer.citations
                    ],
                    # Counts and the answer's own failing strings, never the
                    # sources — the same line `render_answer` holds.
                    "grounding": {
                        "quotes_checked": answer.quotes_checked,
                        "quotes_verified": answer.quotes_verified,
                        "quotes_unchecked": answer.quotes_unchecked,
                        "unverified_speech": [q.text for q in answer.unverified_speech],
                        "unverified_phrases": [q.text for q in answer.unverified_phrases],
                        "unspoken": [q.text for q in answer.unspoken_quotes],
                        "translated": [q.text for q in answer.translated_quotes],
                        "uncited": list(answer.uncited_quotes),
                        "unresolved_ids": list(answer.unresolved_ids),
                    },
                    "failed": answer.failed,
                    "truncated": answer.truncated,
                    "requests": answer.requests,
                    "cost_usd": round(answer.cost_usd, 6),
                    "elapsed_s": round(answer.elapsed_s, 1),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return EXIT_FAILED if answer.failed or not answer.text else EXIT_OK

    if not answer.text:
        if saved is not None:
            log.warning("ask: the empty answer was recorded at %s", saved)
        return _error("the model returned an empty answer", EXIT_FAILED)

    # The same markers the stored file carries, so the record matches what
    # was read. `mark_unverified` adds only marginalia's own annotation.
    print(mark_unverified(answer))

    links = render_links(answer.citations, answer.titles)
    if links:
        print("\nlinks:")
        for line in links:
            print(f"  {line}")

    cut = {video_id: (given, total) for video_id, given, total in answer.transcripts_cut}
    if answer.opened:
        print("\nopened transcripts:")
        for video_id, why in answer.opened:
            extent = (
                f"  (cut: first {cut[video_id][0]:,} of {cut[video_id][1]:,} chars)"
                if video_id in cut
                else ""
            )
            print(f"  {video_id}  {why}{extent}")
    if answer.escalations_refused:
        print("\nasked for and not opened:")
        for video_id, why in answer.escalations_refused:
            print(f"  {video_id}  {why}")
    if answer.deep_skipped:
        print("\n--deep not honoured:")
        for video_id, why in answer.deep_skipped:
            print(f"  {video_id}  {why}")

    print()
    print(_ask_summary(answer))
    if saved is not None:
        print(f"saved:    {saved}")
    if answer.unparsed:
        # Named, not merely counted. A number tells you something was lost; the
        # fragment tells you which shape to teach the parser next.
        log.info("ask: %d citation-shaped strings were unreadable", len(answer.unparsed))
        for fragment in answer.unparsed:
            log.debug("ask: unreadable citation: %s", fragment)
    if answer.failed:
        # Printed above so the record and the terminal agree, and still a
        # failure: an exit 0 here is what lets a wrapper relay an error message
        # as though it were the library's answer.
        return _error("the model call did not produce an answer", EXIT_FAILED)
    if answer.truncated:
        print("warning:  the answer hit the token ceiling; it is a fragment", file=sys.stderr)
    return EXIT_OK


def _ask_summary(answer: Answer) -> str:
    """The one accounting line after an answer. Pure, so it can be asserted on.

    `unparsed` is printed even at zero — that is the number which makes the
    count trustworthy on the day it is not zero. `repaired` is printed only when
    it happened, because zero is the normal state there and a permanent
    "0 id(s) repaired" would be noise rather than a signal.
    """
    line = (
        f"ask:      {answer.notes_loaded} notes, "
        f"{len(answer.citations) + len(answer.unparsed)} citation-shaped, "
        f"{len(answer.citations)} parsed, {len(answer.unparsed)} unparsed, "
        f"{len(answer.past_end)} past the end of their video, "
    )
    if answer.repaired:
        # The model got a video ID's casing wrong and the parser covered for it.
        # Worth seeing rather than inferring from a debug log.
        line += f"{len(answer.repaired)} id(s) repaired, "
    plain = sum(1 for item in answer.edited if not item.case_folded)
    folded = len(answer.edited) - plain
    if plain:
        # Its own count: a different rule from case and prefix, with a margin.
        line += f"{plain} mistyped id(s) repaired by edit distance, "
    if folded:
        # And the folded pass apart from it: its own bounds, its own count.
        line += f"{folded} mistyped id(s) repaired by case-folded edit distance, "
    line += (
        f"{answer.requests} requests, ${answer.cost_usd:.4f}, "
        f"{_elapsed(answer.elapsed_s)}, v{answer.version}"
    )
    if has_grounding(answer):
        # Its own line, never summed with the citation counters: those read
        # syntax and this reads content, so a run where both are non-zero has
        # two different problems. The denominator comes first because a count
        # of failures cannot be read without it, and the two unverified counts
        # stay apart — a quoted sentence nobody said and a quoted phrase are
        # different claims about the answer.
        against = (
            f" ({answer.quotes_unchecked} against a note only)"
            if answer.quotes_unchecked
            else ""
        )
        line += "\n          " + (
            f"grounding: {answer.quotes_verified} of {answer.quotes_checked} quote(s) "
            f"found in the cited transcript{against}; "
            f"{len(answer.unverified_speech)} sentence(s) and "
            f"{len(answer.unverified_phrases)} phrase(s) not found, "
            f"{len(answer.unspoken_quotes)} the note's words not the speaker's, "
            f"{len(answer.translated_quotes)} a note's translation, "
            f"{len(answer.unresolved_ids)} id(s) name no loaded video"
        )
    # Printed every time, not only when it is close. The point of the number is
    # that the whole-library approach stays visible while it is still working —
    # a guard nobody sees until it fires is a cliff with a sign at the bottom.
    headroom = (
        f", {answer.headroom:.0%} of the input ceiling"
        if answer.headroom is not None
        else ""
    )
    line += "\n          " + f"~{answer.estimated_input_tokens:,} input tokens{headroom}"
    if answer.notes_considered > answer.notes_loaded:
        # The number that says an answer was written from part of the library.
        # Printed whenever it is true, because a selection nobody sees is one
        # nobody can correct.
        dropped = answer.notes_considered - answer.notes_loaded
        over = (
            f", {answer.selection.overflowed} over budget"
            if answer.selection.overflowed
            else ""
        )
        line += (
            "\n          "
            + f"selection: {answer.selection.mode}, {answer.notes_loaded} of "
            + f"{answer.notes_considered} notes ({dropped} not loaded{over})"
        )
    return line


def _save_answer(library: Library, answer: Answer, *, no_save: bool) -> Path | None:
    """Write the answer under ``answers/``, and never fail a question over it.

    Saving is the default and `--no-save` is the exception, because an answer
    that exists only until the terminal scrolls is the thing this was added to
    stop. But the answer has already been produced and paid for by the time this
    runs: a full disk or a permission error must cost the record, not the
    answer. Logged rather than raised, and loudly enough to notice.
    """
    if no_save:
        return None
    try:
        return library.write_answer(
            answer.question,
            render_answer(answer, asked_at=_now()),
            day=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )
    except (OSError, ValueError) as exc:
        log.warning("ask: the answer could not be saved (%s)", exc)
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cmd_run_export(args: argparse.Namespace) -> int:
    """Render one discovery run as a folder of readable files.

    On demand only. Nothing is exported automatically and nothing is written
    back: the ledger stays the single source of truth and this is a view of it,
    which is what makes it safe to delete or regenerate at any time.
    """
    config = _load(args)
    library = Library(config.library_path)
    library.ensure()

    with Ledger(library.ledger_path) as ledger:
        try:
            exported = export_run(config, ledger, library, args.run_id)
        except LookupError:
            # The error names what does exist. Finding a run ID was the other
            # half of "diagnosing a run means writing SQL", and an error that
            # said only "no such run" would leave that half unsolved.
            return _error(
                f"no run {args.run_id!r}. Recent runs:\n"
                + "\n".join(_recent_runs(ledger)),
                EXIT_USAGE,
            )
        except ValueError as exc:
            return _error(str(exc), EXIT_USAGE)

    for path in exported.files:
        print(f"wrote     {path}")
    print(
        f"export:   run {exported.run_id}, {exported.rounds} rounds, "
        f"{exported.queries} queries, {exported.decisions} decisions "
        f"-> {exported.directory}"
    )
    return EXIT_OK


# What each check means, in the words the report prints. Kept beside the
# renderer rather than in contracts.py because it is prose for a reader, not a
# value anything else consumes.
_CHECK_MEANING = {
    CHECK_UNTRACKED_DIR: "videos/<id>/ exists with no ledger row: written without the CLI",
    CHECK_TRANSCRIPT_MISSING: "status claims a transcript that is not on disk",
    CHECK_NOTE_MISSING: "noted_at is set and note.md is gone",
    CHECK_NOTE_UNRECORDED: "note.md exists and noted_at is NULL: written without the CLI",
    CHECK_NOTE_INVALID: "stored note fails validate_note",
    CHECK_TRANSCRIPT_MODIFIED: "content_hash no longer matches transcript.txt (rule 3)",
    CHECK_TRANSCRIPT_UNRECORDED: (
        "transcript.txt is on disk and the row does not record it: an interrupted "
        "store; `marg reconcile` records it"
    ),
    CHECK_RUN_UNFINISHED: "run has no finished_at (rule 6's signal; reported, not an error)",
}


def _cmd_reconcile(args: argparse.Namespace) -> int:
    """Record transcripts already on disk that their ledger rows do not.

    The repair for an interrupted store, and the only write `marg doctor`'s
    eighth check points at. Reads the bytes to hash them and changes none; a
    row that already agrees is left alone, so running it twice is harmless.
    No network call and no model call.
    """
    config = _load(args)
    library = Library(config.library_path)
    library.ensure()

    with Ledger(library.ledger_path) as ledger:
        if args.video:
            video_ids = []
            for value in args.video:
                video_id = extract_video_id(value)
                if video_id is None:
                    return _error(f"not a YouTube URL or video ID: {value!r}", EXIT_USAGE)
                video_ids.append(video_id)
        else:
            video_ids = unreconciled(library, ledger.list_by_status())
        repaired = 0
        for video_id in video_ids:
            if reconcile_stored(library, ledger, video_id):
                repaired += 1
                _report("reconciled", video_id, "ledger row now records the transcript")
            elif args.video:
                _report("skipped", video_id, "nothing to reconcile")
    print(f"reconcile: {repaired} row(s) recorded")
    return EXIT_OK


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Compare the library on disk against the ledger. Read-only.

    Nothing is created on the way in, either: no ``logs/`` (so ``_load`` is
    asked not to attach the file log), no ``videos/`` (no ``ensure()``), and no
    ``ledger.db`` — a library with no ledger is diagnosed against zero rows
    rather than given one. A doctor that left footprints would be the first
    thing it reported next time.

    Exit 0 when clean, 1 when any of checks 1–6 found something. Check 7 — a
    run with no ``finished_at`` — is reported and does not affect the exit code:
    it is the trace rule 6 deliberately leaves, and a run in progress looks the
    same.
    """
    config = _load(args, file_log=False)
    library = Library(config.library_path)

    rows: list[VideoRow] = []
    runs: list[Any] = []
    if library.ledger_path.is_file():
        with Ledger(library.ledger_path, read_only=True) as ledger:
            rows = ledger.list_by_status()
            runs = ledger.list_runs()
    else:
        log.info("no ledger at %s; diagnosing against zero rows", library.ledger_path)

    report = diagnose(library, rows, runs)

    if args.json:
        print(json.dumps(_doctor_json(report), indent=2))
    else:
        for line in _doctor_lines(report):
            print(line)
    return EXIT_OK if report.clean else EXIT_FAILED


def _doctor_lines(report: DoctorReport) -> list[str]:
    """Counts per check, then ids under each. Never content — rule 1."""
    scope = (
        f"{report.videos_checked} video(s), {report.dirs_checked} dir(s), "
        f"{report.runs_checked} run(s)"
    )
    if not report.findings:
        return [f"doctor:   clean — {scope}"]
    unfinished = report.count(CHECK_RUN_UNFINISHED)
    head = f"doctor:   {report.problems} problem(s) in {scope}"
    if unfinished:
        head += f"; {unfinished} unfinished run(s)"
    lines = [head]
    for check in DOCTOR_CHECKS:
        count = report.count(check)
        if not count:
            continue
        lines.append(f"{check:<20} {count:>4}   {_CHECK_MEANING[check]}")
        for finding in report.findings:
            if finding.check != check:
                continue
            suffix = f"  ({finding.detail})" if finding.detail else ""
            lines.append(f"  {finding.subject}{suffix}")
    lines.append(
        f"hashes:   {report.hashes_verified} verified, {report.hashes_unrecorded} unrecorded"
    )
    return lines


def _doctor_json(report: DoctorReport) -> dict[str, Any]:
    return {
        "clean": report.clean,
        "problems": report.problems,
        "videos_checked": report.videos_checked,
        "dirs_checked": report.dirs_checked,
        "runs_checked": report.runs_checked,
        "hashes_verified": report.hashes_verified,
        "hashes_unrecorded": report.hashes_unrecorded,
        "checks": {
            check: {
                "count": report.count(check),
                "findings": [
                    {"subject": finding.subject, "detail": finding.detail}
                    for finding in report.findings
                    if finding.check == check
                ],
            }
            for check in DOCTOR_CHECKS
        },
    }


def _recent_runs(ledger: Ledger, limit: int = 10) -> list[str]:
    runs = ledger.list_runs()[-limit:]
    if not runs:
        return ["  (none — no discovery run has been recorded)"]
    return [
        f"  {run.run_id}  {run.started_at[:10]}  {run.topic}  "
        f"({run.videos_stored} stored, {run.stopped_because or '?'})"
        for run in reversed(runs)
    ]


def _cmd_snap_timestamps(args: argparse.Namespace) -> int:
    """Align cited timestamps onto the transcript line they refer to.

    Note text never reaches stdout here. What is printed is the citations
    themselves — ``00:09:33 -> 00:09:31`` — and the section each sits in.

    It used to print the first seventy characters of the note's line beside
    each move, on the reasoning that a note's words about a video are not the
    video's words. That is false exactly where snapping matters most: a
    ``## Verbatim`` line *is* the video's words, and a library-wide run printed
    hundreds of them — the note-text cost curve rule 1 exists to prevent.
    """
    config = _load(args)
    library = Library(config.library_path)
    library.ensure()

    video_ids: list[str] = []
    for value in args.video or []:
        video_id = extract_video_id(value)
        if video_id is None:
            return _error(f"not a YouTube URL or video ID: {value!r}", EXIT_USAGE)
        video_ids.append(video_id)

    results = snap_library(library, video_ids, dry_run=args.dry_run)
    if not results:
        print("no notes to snap")
        return EXIT_OK

    snapped = left = failed = 0
    for result in results:
        if result.error:
            failed += 1
            _report("skipped", result.video_id, result.error)
            continue
        snapped += len(result.snapped)
        left += len(result.left)
        if result.snapped:
            _report(
                "snapped" if not args.dry_run else "would",
                result.video_id,
                f"{len(result.snapped)} of {result.total}",
            )
            for change in result.snapped:
                shift = change.delta_ms / 1000.0
                print(
                    f"             {change.before} -> {change.after} "
                    f"({shift:+.1f}s, {change.section})"
                )
        elif result.total:
            _report("unchanged", result.video_id, f"0 of {result.total}")

    reasons: dict[str, int] = {}
    for result in results:
        for _section, _raw, reason in result.left:
            reasons[reason] = reasons.get(reason, 0) + 1
    for reason, count in sorted(reasons.items(), key=lambda item: -item[1]):
        print(f"left:     {count:3d}  {reason}")

    # Rule 14: "left alone" mixes citations the snapper could not improve with
    # readings that name no moment in the video. The second kind gets its own
    # line, per note and in total, zero included.
    checked = [result for result in results if not result.error]
    for result in checked:
        if result.timestamps.unplaceable:
            _report(
                "unplaced",
                result.video_id,
                f"{result.timestamps.unplaceable} of {result.timestamps.total} timestamp(s) "
                f"name no moment in the video",
            )
    print(
        f"placing:  {sum(r.timestamps.unplaceable for r in checked)} of "
        f"{sum(r.timestamps.total for r in checked)} timestamp(s) cannot be placed in their "
        f"video, in {sum(1 for r in checked if r.timestamps.unplaceable)} note(s); "
        f"{sum(r.timestamps.in_tolerance for r in checked)} within the caption tolerance "
        f"past the listed end"
    )

    print(
        f"run:      {snapped} snapped, {left} left alone, {failed} skipped, "
        f"{len(results)} notes" + (" (dry run, nothing written)" if args.dry_run else "")
    )
    return EXIT_OK


def _cmd_guard_write(args: argparse.Namespace) -> int:
    """PreToolUse hook: refuse Write/Edit aimed inside the library.

    Rule 2 — never write into the library directly, always go
    through the CLI — is the rule most likely to be broken *helpfully*, by an
    agent that decides editing a ``meta.json`` by hand is simpler than adding a
    flag. Instruction-following is not enforcement; this is.

    Fails **open**. A guard that cannot read its own input must not block every
    edit in the repository: that failure is loud, constant, and unrelated to
    the library. The narrow rule it enforces is worth less than the session.
    """
    raw = args.stdin.read() if args.stdin is not None else sys.stdin.read()
    try:
        event = json.loads(raw or "{}")
        target = _hook_target(event)
        if target is None:
            return EXIT_OK
        root = library_root()
    except Exception as exc:  # noqa: BLE001 - see the fail-open note above
        print(f"marg guard-write: could not evaluate the hook input: {exc}", file=sys.stderr)
        return EXIT_OK

    if not _inside(target, root):
        return EXIT_OK

    print(
        f"marginalia: blocked a direct write to {target}.\n"
        f"The library at {root} is CLI-owned: the ledger and the files on disk must "
        f"stay in agreement, and transcript.txt is immutable. Use `marg` — add a "
        f"command or a flag if none fits.",
        file=sys.stderr,
    )
    return EXIT_BLOCKED


def _hook_target(event: dict[str, Any]) -> Path | None:
    """The path a Write/Edit-shaped tool call is aimed at, if any."""
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    for key in ("file_path", "notebook_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            path = Path(value)
            if not path.is_absolute():
                path = Path(str(event.get("cwd") or ".")) / path
            # Not resolve(): the target usually does not exist yet, and on
            # Windows resolve() of a missing path still normalises fine, but
            # normpath keeps this honest about symlinks it cannot follow.
            return Path(os.path.normpath(path))
    return None


def _inside(target: Path, root: Path) -> bool:
    """Both sides through normcase: on Windows ``C:\\Lib`` and ``c:/lib`` are
    the same directory, and a guard that misses that is no guard."""
    inner = _comparable(target)
    outer = _comparable(root)
    return inner == outer or inner.startswith(outer.rstrip(os.sep) + os.sep)


def _comparable(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


# -- ingest helpers --------------------------------------------------------


def _metadata(provider: TranscriptProvider | None, video_id: str) -> dict[str, Any] | None:
    fetch_metadata = getattr(provider, "fetch_metadata", None)
    if fetch_metadata is None:
        return None
    try:
        return fetch_metadata(video_id)
    except Exception:  # noqa: BLE001 - a missing title must not fail the run
        log.info("could not fetch metadata for %s", video_id, exc_info=True)
        return None


def _title_of(meta: dict[str, Any] | None) -> str | None:
    return str(meta.get("title")) if meta and meta.get("title") else None


def _describe(library: Library, row: VideoRow) -> list[str]:
    fields: list[str] = []
    meta = library.read_meta(row.video_id) or {}
    if meta.get("title"):
        fields.append(_quoted(meta["title"]))
    if meta.get("line_count"):
        fields.append(f"{meta['line_count']} lines")
    if row.source:
        # Both source and caption_kind come from the ledger now, not meta.json.
        kind = f"/{row.caption_kind}" if row.caption_kind else ""
        fields.append(f"{row.source}{kind}/{meta.get('language', '?')}")
    if row.fail_kind:
        fields.append(row.fail_kind)
    fields.append(f"attempts={row.attempts}")
    fields.append(f"updated={row.last_updated}")
    return fields


# -- output ----------------------------------------------------------------


def _report(verb: str, video_id: str, *fields: str | None) -> None:
    parts = [f"{verb:<{_VERB_WIDTH}} {video_id}"]
    parts.extend(field for field in fields if field)
    print("  ".join(parts))


def _quoted(title: str | None, limit: int = _TITLE_LIMIT) -> str | None:
    if not title:
        return None
    flat = " ".join(str(title).split()).replace('"', "'")
    if len(flat) > limit:
        flat = flat[: limit - 1].rstrip() + "…"
    return f'"{flat}"'


def _padded(field: str | None, width: int) -> str:
    """Left-align in a fixed column so the fields after it line up."""
    return (field or '""').ljust(width)


def _duration(seconds: int | None) -> str:
    return format_duration(seconds)


def _error(message: str, code: int) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def _force_utf8_output() -> None:
    """Titles contain non-ASCII; the Windows console default does not."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover
                pass


# -- plumbing --------------------------------------------------------------


def _load(args: argparse.Namespace, *, file_log: bool = True) -> Config:
    """``file_log=False`` is for the one command that must leave no footprint:
    attaching the file log creates ``logs/`` under the library, and ``doctor``
    would then be reporting on a directory it made."""
    config = load_config()
    if config.created:
        print(f"created config {config.path} (edit it to change the library path)", file=sys.stderr)
    if file_log:
        _configure_file_log(Library(config.library_path))
    log.info("library: %s", config.library_path)
    return config


def _configure_console(*, verbose: bool) -> None:
    """stderr gets INFO or WARNING. The file gets DEBUG.

    The handler's own level is set, not just the root logger's, and that is the
    load-bearing detail. `marginalia` is put at DEBUG so debug records reach the
    file at all, and a record that passes its originating logger's level is
    handed to every handler up the chain — so a root handler left at level 0
    would print every DEBUG line to the terminal. Setting the level on the
    handler is what keeps the two destinations independent.
    """
    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(logging.INFO if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.WARNING, handlers=[console], force=True)
    log.setLevel(logging.DEBUG)


def _configure_file_log(library: Library) -> None:
    """Send `marginalia`'s own DEBUG records to ``logs/marg-<date>.log``.

    Attached to the `marginalia` logger rather than the root, which is a
    deliberate narrowing rather than tidiness. At DEBUG the root logger collects
    the HTTP client's records too — and those carry request bodies, which on
    this project means every note and every transcript the model was ever shown.
    A log file is a surface like any other, and hard rule 1 does not stop
    applying because the destination is a file nobody reads. Only this package's
    own loggers reach here, and none of them is given content to log.

    **Never fatal.** An unwritable library must not stop a command that was
    about to work; the failure goes to stderr once and the command continues.
    """
    path = library.log_path(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    for existing in log.handlers:
        if getattr(existing, "baseFilename", None) == str(path):
            return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8", delay=True)
    except OSError as exc:
        print(f"warning: could not open the log file at {path} ({exc})", file=sys.stderr)
        return
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    log.addHandler(handler)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marg", description="marginalia — YouTube library")
    parser.add_argument("--version", action="version", version=f"marginalia {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="log progress to stderr")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="ingest one video")
    ingest.add_argument("url", help="YouTube URL or video ID")
    ingest.set_defaults(handler=_cmd_ingest)

    search = subparsers.add_parser("search", help="find candidate videos")
    search.add_argument("query", help="search query")
    search.add_argument("--limit", type=int, default=25, help="candidates to request (default 25)")
    search.add_argument(
        "--topic",
        default=None,
        help="topic these candidates are judged against (default: the query)",
    )
    search.add_argument("--json", action="store_true", help="emit structured output")
    search.set_defaults(handler=_cmd_search)

    disco = subparsers.add_parser("discover", help="turn a topic into a library")
    disco.add_argument("topic", help="what to build a library about")
    disco.add_argument("--max-rounds", type=int, default=None, help="search rounds (config default)")
    disco.add_argument("--max-videos", type=int, default=None, help="videos to store this run")
    disco.add_argument("--max-requests", type=int, default=None, help="model requests this run")
    disco.add_argument(
        "--dry-run",
        action="store_true",
        help="brief, queries, search and stage-1 judging only; writes nothing",
    )
    disco.add_argument(
        "--search-source",
        choices=SEARCH_SOURCES,
        default=None,
        help="what writes round one's planned queries: the search plan or the map "
        "(default: [discover] search_source)",
    )
    disco.add_argument(
        "--web-context",
        choices=("on", "off"),
        default=None,
        help="a few web searches before the plan, digested into its CURRENT CONTEXT "
        "(default: [discover] web_context)",
    )
    disco.add_argument("--json", action="store_true", help="emit structured output")
    disco.set_defaults(handler=_cmd_discover)

    brief = subparsers.add_parser(
        "brief", help="write a topic's brief to review, approve it, or show its status"
    )
    brief.add_argument("topic", help="the topic, as marg discover is given it")
    action = brief.add_mutually_exclusive_group()
    action.add_argument(
        "--approve", action="store_true",
        help="check the edited pending brief and store it as the next approved version",
    )
    action.add_argument("--status", action="store_true", help="show the pending and approved briefs")
    brief.add_argument(
        "--force", action="store_true", help="write a new pending brief over an existing one"
    )
    brief.add_argument(
        "--recency", metavar="WINDOW",
        help="the approved brief's recency: ANY, '24 months' or '2 years' (owner-set), or "
             "'decide' to decide it now from the samples a run would make",
    )
    brief.add_argument(
        "--reopen-gaps", action="store_true",
        help="give the gaps runs found exhausted on the approved brief a full share of "
             "queries again (a run otherwise gives each one probe query)",
    )
    brief.set_defaults(handler=_cmd_brief)

    again = subparsers.add_parser(
        "refetch", help="retry transcript fetches that failed transiently"
    )
    again.add_argument(
        "--limit", type=int, default=None, help="stop after N videos"
    )
    again.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be retried, in judged-YES-first order, and touch nothing; "
        "with --replace-translated, judge every video and write nothing",
    )
    again.add_argument(
        "--replace-translated",
        action="store_true",
        help="rule 3's one exception: replace stored transcripts rule 40 shows to be machine "
        "translations (the old file kept as transcript.translated.txt), and clean stored track ids",
    )
    again.add_argument(
        "video", nargs="*",
        help="with --replace-translated: only these videos (default: every stored transcript)",
    )
    again.set_defaults(handler=_cmd_refetch)

    note = subparsers.add_parser("note", help="generate understanding notes")
    note.add_argument("video", nargs="*", help="YouTube URL or video ID (default: everything due)")
    note.add_argument(
        "--limit",
        type=int,
        default=None,
        help="stop after N attempts (notes written or failed; skips are free) — "
        "use this first, and read them before running the batch",
    )
    note.add_argument(
        "--regenerate",
        action="store_true",
        help="rewrite notes with the current prompt, including videos named directly",
    )
    note.add_argument(
        "--all",
        action="store_true",
        help="with --regenerate: every stored video, not only the stale ones",
    )
    note.add_argument(
        "--missing-only",
        action="store_true",
        help="only videos with no note at all, ignoring the prompt version — "
        "what to use after a prompt bump that does not warrant regenerating",
    )
    note.add_argument("--status", action="store_true", help="how many notes are current or stale")
    note.set_defaults(handler=_cmd_note)

    check = subparsers.add_parser(
        "llm-check", help="one request to the configured model provider"
    )
    check.set_defaults(handler=_cmd_llm_check)

    status = subparsers.add_parser("status", help="show ledger state for one video")
    status.add_argument("video", help="YouTube URL or video ID")
    status.set_defaults(handler=_cmd_status)

    listing = subparsers.add_parser("list", help="list stored videos")
    listing.add_argument(
        "--status",
        default=STATUS_HAS_TRANSCRIPT,
        help="ledger status to list (default: has_transcript)",
    )
    listing.set_defaults(handler=_cmd_list)

    ask = subparsers.add_parser("ask", help="answer a question from the library")
    ask.add_argument("question", help="the question, in quotes")
    ask.add_argument(
        "--deep",
        action="append",
        default=[],
        metavar="VIDEO",
        help="force a transcript open (repeatable); the model may still open more",
    )
    ask.add_argument(
        "--no-save",
        action="store_true",
        help="do not write the answer to the library's answers/ folder",
    )
    ask.add_argument("--json", action="store_true", help="machine-readable output")
    ask.set_defaults(handler=_cmd_ask)

    runs = subparsers.add_parser("run", help="inspect a past discovery run")
    run_actions = runs.add_subparsers(dest="run_command", required=True)
    run_export = run_actions.add_parser(
        "export", help="write a readable folder for one run under exports/"
    )
    run_export.add_argument("run_id", help="the run ID, as `marg discover` reported it")
    run_export.set_defaults(handler=_cmd_run_export)

    doctor = subparsers.add_parser(
        "doctor", help="compare the library on disk against the ledger (read-only)"
    )
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    doctor.set_defaults(handler=_cmd_doctor)

    reconcile = subparsers.add_parser(
        "reconcile",
        help="record transcripts left on disk by an interrupted store (no network)",
    )
    reconcile.add_argument("video", nargs="*", help="video IDs or URLs (default: all)")
    reconcile.set_defaults(handler=_cmd_reconcile)

    snap = subparsers.add_parser(
        "snap-timestamps", help="align cited timestamps in notes to the transcript"
    )
    snap.add_argument("video", nargs="*", help="video IDs or URLs (default: every note)")
    snap.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    snap.set_defaults(handler=_cmd_snap_timestamps)

    guard = subparsers.add_parser(
        "guard-write",
        help="internal: PreToolUse hook, blocks direct writes into the library",
    )
    guard.set_defaults(handler=_cmd_guard_write, stdin=None)

    return parser


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
