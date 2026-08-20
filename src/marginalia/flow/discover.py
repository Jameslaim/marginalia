"""The discovery loop: one topic in, a library out.

::

    round:
      expand brief -> queries
      search each query -> candidates          (M2a, unchanged)
      gates 1 and 2                            (M2a, unchanged)
      stage 1, batched -> yes / no / maybe
      maybe -> fetch -> stage 2 -> yes / no + threads
      yes -> store
      decide whether to run again

The loop-back is the whole point. Everything else is plumbing.

**Threads come free.** Stage 2 already reads the opening, so it returns the
concepts a video mentions without explaining in the same response — no second
request. A video on prompt caching that says "KV cache" and moves on has
written round two's query for us.

Cost discipline runs through the whole file. Model requests are the scarce
resource on a free tier, so: gates run before judging, judging runs before
fetching, and every stopping condition is checked before spending rather than
after.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Sequence

from ..brain.judge import Judge, opening_words
from ..config import Config
from ..contracts import (
    NEW,
    VERDICT_MAYBE,
    VERDICT_NO,
    VERDICT_YES,
    DiscoverRun,
    RoundReport,
    SearchResult,
    TranscriptResult,
)
from ..core.ids import extract_video_id
from ..core.ledger import (
    FAIL_PERMANENT,
    FAIL_TRANSIENT,
    STAGE_METADATA,
    STAGE_OPENING,
    Ledger,
    normalise_topic,
)
from ..core.library import Library
from ..llm import LlmClient
from ..providers import PermanentError, TransientError, get_provider
from ..providers.base import TranscriptProvider
from .search import run_search

log = logging.getLogger(__name__)

__all__ = ["DiscoverRun", "RoundReport", "discover", "STOP_REASONS"]

# Why a loop stopped. A loop that stops for an unknown reason cannot be tuned.
STOP_MAX_ROUNDS = "max_rounds"
STOP_NO_NEW_VIDEOS = "no_new_videos"
STOP_MAX_VIDEOS = "max_videos"
STOP_MAX_REQUESTS = "max_requests"
STOP_MAX_COST = "max_cost"
STOP_NO_SEARCH = "search_unavailable"
STOP_DRY_RUN = "dry_run"
STOP_REASONS = (
    STOP_MAX_ROUNDS,
    STOP_NO_NEW_VIDEOS,
    STOP_MAX_VIDEOS,
    STOP_MAX_REQUESTS,
    STOP_MAX_COST,
    STOP_NO_SEARCH,
    STOP_DRY_RUN,
)

# Requests a round needs beyond judging: query expansion. Checked before the
# round starts so the loop stops cleanly instead of failing mid-round.
_ROUND_OVERHEAD_REQUESTS = 1


def discover(
    config: Config,
    ledger: Ledger,
    library: Library,
    client: LlmClient,
    topic: str,
    *,
    max_rounds: int | None = None,
    max_videos: int | None = None,
    max_requests: int | None = None,
    dry_run: bool = False,
    judge: Judge | None = None,
    provider: TranscriptProvider | None = None,
) -> DiscoverRun:
    """Run the loop. Writes nothing to the library when ``dry_run`` is set."""
    limits = config.discover
    max_rounds = max_rounds if max_rounds is not None else limits.max_rounds
    max_videos = max_videos if max_videos is not None else limits.max_videos
    max_requests = max_requests if max_requests is not None else limits.max_requests

    normalised = normalise_topic(topic)
    judge = judge or Judge(client)
    run_id = _new_run_id()

    # The brief costs two requests and must exist before the run row can be
    # written — but the row must exist before any *judging* happens, which is
    # what the ordering rule is protecting. A crash during brief-writing leaves
    # no run, and also no decisions to explain.
    brief = judge.write_brief(topic)

    ledger.start_run(
        run_id,
        normalised,
        brief=brief.text,
        brief_version=brief.version,
        model=client.model,
    )

    run = DiscoverRun(
        run_id=run_id,
        topic=normalised,
        brief=brief,
        model=client.model,
        dry_run=dry_run,
    )

    try:
        _loop(
            config, ledger, library, client, judge, run, provider,
            max_rounds=max_rounds, max_videos=max_videos, max_requests=max_requests,
        )
    finally:
        run.requests = client.usage.requests
        run.input_tokens = client.usage.input_tokens
        run.output_tokens = client.usage.output_tokens
        run.cost_usd = config.llm.cost_usd(run.input_tokens, run.output_tokens)
        ledger.finish_run(
            run_id,
            rounds=len(run.rounds),
            candidates_seen=run.candidates_seen,
            videos_stored=len(run.stored_ids),
            requests_made=run.requests,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            stopped_because=run.stopped_because or "incomplete",
        )
    return run


def _loop(
    config: Config,
    ledger: Ledger,
    library: Library,
    client: LlmClient,
    judge: Judge,
    run: DiscoverRun,
    provider: TranscriptProvider | None,
    *,
    max_rounds: int,
    max_videos: int,
    max_requests: int,
) -> None:
    threads: list[str] = []
    seen_queries: set[str] = set()
    quiet_rounds = 0

    for number in range(1, max_rounds + 1):
        if client.usage.requests + _ROUND_OVERHEAD_REQUESTS > max_requests:
            run.stopped_because = STOP_MAX_REQUESTS
            return
        if _overspent(config, client):
            run.stopped_because = STOP_MAX_COST
            return

        queries = [
            query
            for query in judge.expand_queries(
                run.brief, count=config.discover.queries_per_round, concepts=threads[:8]
            )
            if query.lower() not in seen_queries
        ]
        seen_queries.update(query.lower() for query in queries)
        report = RoundReport(number=number, queries=queries, from_threads=bool(threads))
        run.rounds.append(report)

        if not queries:
            run.stopped_because = STOP_NO_NEW_VIDEOS
            return

        candidates = _gather(config, ledger, run, queries, report)
        if candidates is None:
            run.stopped_because = STOP_NO_SEARCH
            return

        threads = _judge_round(
            config, ledger, library, client, judge, run, report, candidates, provider,
            max_videos=max_videos, max_requests=max_requests,
        )
        # Recorded on the round that *produced* them, so a later reader can ask
        # whether round N+1's queries actually came from round N's findings.
        # Without this, `from_threads` only says concepts were passed in — not
        # that they changed anything, which is the claim worth checking.
        report.concepts = list(threads)

        if run.stopped_because:
            return
        if len(run.stored_ids) >= max_videos:
            run.stopped_because = STOP_MAX_VIDEOS
            return

        quiet_rounds = 0 if report.stored else quiet_rounds + 1
        if quiet_rounds >= config.discover.quiet_rounds:
            run.stopped_because = STOP_NO_NEW_VIDEOS
            return

    run.stopped_because = STOP_MAX_ROUNDS


def _gather(
    config: Config,
    ledger: Ledger,
    run: DiscoverRun,
    queries: Sequence[str],
    report: RoundReport,
) -> list[SearchResult] | None:
    """Search every query and apply gates 1 and 2. Returns survivors, in rank order."""
    survivors: dict[str, SearchResult] = {}
    answered = False

    for query in queries:
        try:
            result = run_search(
                config,
                ledger,
                query,
                limit=config.discover.results_per_query,
                topic=run.topic,
                log_decisions=not run.dry_run,
                run_id=run.run_id,
            )
        except (PermanentError, TransientError) as exc:
            # One dead query must not end a round that has nine good ones.
            log.warning("discover: search failed for %r: %s", query, exc)
            continue

        answered = True
        report.candidates += len(result.candidates)
        report.known += len(result.of("known"))
        report.rejected_before += len(result.of("rejected"))
        for candidate in result.of(NEW):
            survivors.setdefault(candidate.video_id, candidate.result)

    if not answered:
        return None
    return list(survivors.values())


def _judge_round(
    config: Config,
    ledger: Ledger,
    library: Library,
    client: LlmClient,
    judge: Judge,
    run: DiscoverRun,
    report: RoundReport,
    candidates: Sequence[SearchResult],
    provider: TranscriptProvider | None,
    *,
    max_videos: int,
    max_requests: int,
) -> list[str]:
    if not candidates:
        return []

    verdicts = judge.judge_metadata(
        run.brief, candidates, batch_size=config.llm.judge_batch_size
    )
    by_id = {candidate.video_id: candidate for candidate in candidates}
    concepts: list[str] = []
    fetched_any = False

    for verdict in verdicts:
        _log(ledger, run, verdict.video_id, STAGE_METADATA, verdict.verdict, verdict.reason)
        if verdict.verdict == VERDICT_YES:
            report.verdict_yes += 1
        elif verdict.verdict == VERDICT_NO:
            report.verdict_no += 1
        else:
            report.verdict_maybe += 1

    if run.dry_run:
        run.stopped_because = STOP_DRY_RUN
        return []

    # YES first: it needs no second model call, so it is the cheaper keeper.
    ordered = [v for v in verdicts if v.verdict == VERDICT_YES]
    ordered += [v for v in verdicts if v.verdict == VERDICT_MAYBE]

    for verdict in ordered:
        if len(run.stored_ids) >= max_videos:
            run.stopped_because = STOP_MAX_VIDEOS
            return concepts

        candidate = by_id[verdict.video_id]

        # Gate 1 asks whether a video is in the *library*, which since B1 means
        # it has a note. This asks a different question — do we already hold the
        # bytes? — and it has to be asked separately, because the two answers
        # genuinely differ: a video fetched for an earlier topic whose note has
        # not been written yet is not in the library and is already on disk.
        #
        # Gate 1 used to shadow this by dropping anything `stored`. Splitting
        # status from relevance unshadowed it, and `write_transcript` caught the
        # result immediately by refusing to overwrite an immutable file — which
        # is the guard working, not a reason to weaken Gate 1. The transcript
        # stays, no network call is made, and `marg note` will pick the video up
        # from `has_transcript` regardless of which topic first found it.
        if library.has_transcript(candidate.video_id):
            log.info(
                "discover: %s already has a transcript; not re-fetching",
                candidate.video_id,
            )
            report.known += 1
            continue

        _pace(config, fetched_any)
        fetched_any = True
        fetched = _fetch(ledger, candidate.video_id, provider, config)
        if fetched is None:
            report.failed += 1
            continue

        if verdict.verdict == VERDICT_MAYBE:
            if client.usage.requests >= max_requests:
                run.stopped_because = STOP_MAX_REQUESTS
                return concepts
            if _overspent(config, client):
                run.stopped_because = STOP_MAX_COST
                return concepts
            opening = judge.judge_opening(
                run.brief,
                candidate,
                opening=opening_words([line.text for line in fetched.lines]),
                chapters=[chapter.get("title", "") for chapter in fetched.chapters][:12],
            )
            _log(ledger, run, candidate.video_id, STAGE_OPENING, opening.verdict, opening.reason)
            concepts.extend(opening.concepts)
            if opening.verdict == VERDICT_NO:
                report.flipped_to_no += 1
                # The verdict is already logged against this topic just above.
                # All the row records is that we hold nothing — and it must not
                # be left `pending`, which would read as an interrupted fetch.
                ledger.mark_seen(candidate.video_id)
                continue

        _store(library, ledger, candidate, fetched, config)
        run.stored_ids.append(candidate.video_id)
        report.stored += 1

    return _dedupe_concepts(concepts)


# -- fetching and storing ---------------------------------------------------


def _fetch(
    ledger: Ledger, video_id: str, provider: TranscriptProvider | None, config: Config
) -> TranscriptResult | None:
    """Fetch a transcript, ledger row first. Returns None on any non-result."""
    provider = provider or get_provider(config.transcript_order[0])
    ledger.mark_pending(video_id)
    try:
        result = provider.fetch(video_id)
    except PermanentError as exc:
        log.info("discover: %s permanently unavailable (%s)", video_id, exc)
        ledger.mark_failed(video_id, FAIL_PERMANENT, provider=provider.name)
        return None
    except TransientError as exc:
        log.info("discover: %s failed transiently (%s)", video_id, exc)
        ledger.mark_failed(video_id, FAIL_TRANSIENT, provider=provider.name)
        return None

    if result is None:
        # No captions is a route, not a failure. M4 picks these up.
        ledger.mark_needs_asr(video_id, provider=provider.name)
        return None
    return result


def _store(
    library: Library,
    ledger: Ledger,
    candidate: SearchResult,
    result: TranscriptResult,
    config: Config,
) -> None:
    count = library.write_transcript(candidate.video_id, result.lines)
    library.write_meta(
        candidate.video_id,
        {
            "video_id": candidate.video_id,
            "title": result.title or candidate.title,
            "channel": result.channel or candidate.channel,
            "duration_s": result.duration_s,
            "language": result.language,
            "upload_date": result.upload_date,
            "source": result.source,
            "caption_kind": result.caption_kind,
            "provider": config.transcript_order[0],
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "chapters": result.chapters,
            "original_url": f"https://www.youtube.com/watch?v={candidate.video_id}",
            "line_count": count,
        },
    )
    ledger.mark_has_transcript(
        candidate.video_id,
        source=result.source,
        caption_kind=result.caption_kind,
        provider=config.transcript_order[0],
    )


def _overspent(config: Config, client: LlmClient) -> bool:
    """Money, not call count, is the real ceiling under per-token billing.

    One batch of fifty candidates and one of five cost the same number of
    requests and wildly different amounts, so ``max_requests`` alone stops
    being a guard the moment the provider bills per token.
    """
    ceiling = config.discover.max_cost_usd
    if ceiling <= 0:
        return False
    spent = config.llm.cost_usd(client.usage.input_tokens, client.usage.output_tokens)
    if spent >= ceiling:
        log.warning("discover: estimated $%.3f spent, ceiling is $%.2f", spent, ceiling)
        return True
    return False


def _pace(config: Config, already_fetched: bool) -> None:
    """Wait between fetches so YouTube does not start refusing.

    Observed: a run that pulled six videos quickly had its next thirteen
    fetches all fail within nine seconds. Real backoff and retry belong to M3;
    this is enough to keep a run from provoking the throttle in the first place.
    """
    delay = config.discover.fetch_delay_s
    if already_fetched and delay > 0:
        # Looked up on the module, not bound as a default argument: a default
        # captures the real sleep at import and a test could never replace it.
        time.sleep(delay)


def _log(
    ledger: Ledger, run: DiscoverRun, video_id: str, stage: str, verdict: str, reason: str
) -> None:
    """Every verdict at every stage, unless this is a dry run.

    A dry run records nothing: writing `no` rows would poison Gate 2 for the
    real run that follows, so tuning the judge would shrink the library.
    """
    if run.dry_run:
        return
    ledger.log_decision(
        video_id,
        run.topic,
        stage=stage,
        verdict=verdict,
        reason=reason,
        run_id=run.run_id,
        model=run.model,
    )


def _dedupe_concepts(concepts: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for concept in concepts:
        key = " ".join(concept.lower().split())
        if key and key not in seen:
            seen.add(key)
            unique.append(concept)
    return unique


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def looks_like_video(value: str) -> bool:
    """Used by the skill router: a URL or bare ID means ingest, not discover."""
    return extract_video_id(value) is not None
