"""The marginalia command line.

    marg ingest <url>       ingest one video
    marg status <url|id>    show ledger state for one video
    marg list               list stored videos

Output is terse and single-line, because a skill reads it rather than a human.

Transcript contents are never printed. Not on success, not on error, not under
--verbose. That rule is the whole reason this is a CLI and not a tool the model
calls directly.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Sequence

from . import __version__
from .config import Config, ConfigError, load_config
from .ids import extract_video_id
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

log = logging.getLogger("marginalia")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

_VERB_WIDTH = 9
_TITLE_LIMIT = 80


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


def _quoted(title: str | None) -> str | None:
    if not title:
        return None
    flat = " ".join(str(title).split()).replace('"', "'")
    if len(flat) > _TITLE_LIMIT:
        flat = flat[: _TITLE_LIMIT - 1].rstrip() + "…"
    return f'"{flat}"'


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

    status = subparsers.add_parser("status", help="show ledger state for one video")
    status.add_argument("video", help="YouTube URL or video ID")
    status.set_defaults(handler=_cmd_status)

    listing = subparsers.add_parser("list", help="list stored videos")
    listing.add_argument(
        "--status", default=STATUS_STORED, help="ledger status to list (default: stored)"
    )
    listing.set_defaults(handler=_cmd_list)

    return parser


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
