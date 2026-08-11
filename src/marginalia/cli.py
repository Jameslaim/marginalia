"""The marginalia command line.

    marg ingest <url>       ingest one video
    marg search "<query>"   find candidate videos and say which are new
    marg discover "<topic>" turn a topic into a library
    marg llm-check          one request to the configured model provider
    marg status <url|id>    show ledger state for one video
    marg list               list stored videos
    marg guard-write        internal: the PreToolUse hook

Output is terse and single-line, because a skill reads it rather than a human.

Transcript contents are never printed. Not on success, not on error, not under
--verbose. That rule is the whole reason this is a CLI and not a tool the model
calls directly.
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
from .config import Config, ConfigError, load_config
from .discover import DiscoverRun, discover
from .ids import extract_video_id
from .llm import LlmClient, LlmError
from .ledger import (
    FAIL_PERMANENT,
    FAIL_TRANSIENT,
    STATUS_STORED,
    Ledger,
    VideoRow,
    skip_reason,
)
from .library import Library
from .providers import PermanentError, TransientError, get_provider
from .providers.base import TranscriptProvider, TranscriptResult
from .search import KNOWN, NEW, REJECTED, Candidate, SearchRun, run_search

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
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        return int(args.handler(args))
    except ConfigError as exc:
        return _error(str(exc), EXIT_USAGE)
    except PermanentError as exc:
        # A bad query, a rejected key, an unknown provider name. Deliberately
        # not softened into a fallback: all three are the user's to fix.
        return _error(str(exc), EXIT_USAGE)
    except KeyboardInterrupt:  # pragma: no cover
        return _error("interrupted", EXIT_FAILED)
    except Exception as exc:  # noqa: BLE001 - the CLI is the last line of defence
        log.debug("unhandled error", exc_info=True)
        return _error(f"{type(exc).__name__}: {exc}", EXIT_FAILED)


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
        reason = skip_reason(
            existing, transcript_present=library.has_transcript(video_id)
        )
        if reason is not None:
            _report("skipped", video_id, reason)
            return EXIT_OK
        if existing is not None:
            log.info("re-attempting %s (ledger status %s)", video_id, existing.status)

        # Ledger row before the network call, always. A crash after this point
        # leaves `pending`, which is exactly what the next run needs to know.
        row = ledger.mark_pending(video_id)

        provider, result, failure = _fetch(config, video_id)

        if failure is not None:
            kind, message = failure
            ledger.mark_failed(video_id, kind, provider=provider.name if provider else None)
            _report("failed", video_id, f"{kind}: {message} (attempt {row.attempts})")
            return EXIT_FAILED

        if result is None:
            ledger.mark_needs_asr(video_id, provider=provider.name if provider else None)
            title = _title_of(_metadata(provider, video_id))
            _report("needs-asr", video_id, _quoted(title), "no captions available")
            return EXIT_OK

        assert provider is not None
        count = _store(library, video_id, result, provider, args.url)
        ledger.mark_stored(
            video_id,
            source=result.source,
            caption_kind=result.caption_kind,
            provider=provider.name,
        )
        _report(
            "stored",
            video_id,
            _quoted(result.title),
            f"{count} lines",
            f"{result.source}/{result.language}",
        )
        return EXIT_OK


def _cmd_search(args: argparse.Namespace) -> int:
    config = _load(args)
    library = Library(config.library_path)
    library.ensure()

    with Ledger(library.ledger_path) as ledger:
        run = run_search(config, ledger, args.query, limit=args.limit, topic=args.topic)

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
        )

    if args.json:
        print(json.dumps(_discover_json(run), indent=2, ensure_ascii=False))
        return EXIT_OK

    for line in _discover_report(run):
        print(line)
    return EXIT_OK


def _discover_report(run: DiscoverRun) -> list[str]:
    lines = [f"brief:    {run.brief.intent}"]
    for report in run.rounds:
        source = "queries from threads" if report.from_threads else "queries"
        line = (
            f"round {report.number}:  {len(report.queries)} {source}, "
            f"{report.candidates} candidates, {report.known} known, "
            f"{report.verdict_no} no, {report.verdict_maybe} maybe "
            f"→ {report.stored} stored"
        )
        # Never silent. A round where every fetch failed otherwise reads
        # exactly like a round where the judge rejected everything — observed
        # in a real run, where 13 transient fetch failures left no trace in
        # the report at all.
        if report.failed:
            line += f" ({report.failed} fetch failed)"
        if report.rejected_before:
            line += f", {report.rejected_before} previously rejected"
        lines.append(line)
    verb = "would store" if run.dry_run else "stored"
    lines.append(
        f"run:      {len(run.stored_ids)} {verb}, {run.judged} judged, "
        f"{len(run.rounds)} rounds, {run.requests} requests, "
        f"stopped: {run.stopped_because}"
    )
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
        "stopped_because": run.stopped_because,
        "requests": run.requests,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "candidates_seen": run.candidates_seen,
        "judged": run.judged,
        "stored": run.stored_ids,
        "rounds": [
            {
                "number": report.number,
                "from_threads": report.from_threads,
                "queries": report.queries,
                # What stage 2 found unexplained in this round's videos. These
                # are what the *next* round is supposed to have searched for,
                # so they are the only way to check the loop-back really fed
                # forward rather than the brief being re-expanded.
                "concepts": report.concepts,
                "candidates": report.candidates,
                "known": report.known,
                "rejected_before": report.rejected_before,
                "yes": report.verdict_yes,
                "no": report.verdict_no,
                "maybe": report.verdict_maybe,
                "flipped_to_no": report.flipped_to_no,
                "stored": report.stored,
                "failed": report.failed,
            }
            for report in run.rounds
        ],
    }


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


def _cmd_guard_write(args: argparse.Namespace) -> int:
    """PreToolUse hook: refuse Write/Edit aimed inside the library.

    ``CLAUDE.md`` rule 2 — never write into the library directly, always go
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
        root = load_config().library_path
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


def _fetch(
    config: Config, video_id: str
) -> tuple[TranscriptProvider | None, TranscriptResult | None, tuple[str, str] | None]:
    """Try each configured provider in order.

    Returns ``(provider, result, failure)``. ``result is None`` with no failure
    means no captions exist — the needs_asr route, not an error. A transient
    failure outranks "no captions": if any provider errored we do not yet know
    that captions are absent, so the video stays retryable.
    """
    provider: TranscriptProvider | None = None
    last_failure: tuple[str, str] | None = None

    for name in config.transcript_order:
        try:
            provider = get_provider(name)
        except KeyError:
            return None, None, (FAIL_PERMANENT, f"unknown provider {name!r}")

        try:
            result = provider.fetch(video_id)
        except PermanentError as exc:
            return provider, None, (FAIL_PERMANENT, str(exc))
        except TransientError as exc:
            log.info("provider %s failed transiently: %s", name, exc)
            last_failure = (FAIL_TRANSIENT, str(exc))
            continue

        if result is not None:
            return provider, result, None
        log.info("provider %s reports no captions for %s", name, video_id)

    return provider, None, last_failure


def _store(
    library: Library,
    video_id: str,
    result: TranscriptResult,
    provider: TranscriptProvider,
    original_url: str,
) -> int:
    count = library.write_transcript(video_id, result.lines)
    library.write_meta(
        video_id,
        {
            "video_id": video_id,
            "title": result.title,
            "channel": result.channel,
            "duration_s": result.duration_s,
            "language": result.language,
            "upload_date": result.upload_date,
            "source": result.source,
            "caption_kind": result.caption_kind,
            "provider": provider.name,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "chapters": result.chapters,
            "original_url": original_url,
            "line_count": count,
        },
    )
    return count


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
    """Compact and fixed-vocabulary: ``45s``, ``18m``, ``1h02m``, or ``?``."""
    if not seconds or seconds < 0:
        return "?"
    if seconds < 60:
        return f"{seconds}s"
    minutes, _ = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, remainder = divmod(minutes, 60)
    return f"{hours}h{remainder:02d}m"


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


def _load(args: argparse.Namespace) -> Config:
    config = load_config()
    if config.created:
        print(f"created config {config.path} (edit it to change the library path)", file=sys.stderr)
    log.info("library: %s", config.library_path)
    return config


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
    disco.add_argument("--json", action="store_true", help="emit structured output")
    disco.set_defaults(handler=_cmd_discover)

    check = subparsers.add_parser(
        "llm-check", help="one request to the configured model provider"
    )
    check.set_defaults(handler=_cmd_llm_check)

    status = subparsers.add_parser("status", help="show ledger state for one video")
    status.add_argument("video", help="YouTube URL or video ID")
    status.set_defaults(handler=_cmd_status)

    listing = subparsers.add_parser("list", help="list stored videos")
    listing.add_argument(
        "--status", default=STATUS_STORED, help="ledger status to list (default: stored)"
    )
    listing.set_defaults(handler=_cmd_list)

    guard = subparsers.add_parser(
        "guard-write",
        help="internal: PreToolUse hook, blocks direct writes into the library",
    )
    guard.set_defaults(handler=_cmd_guard_write, stdin=None)

    return parser


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
