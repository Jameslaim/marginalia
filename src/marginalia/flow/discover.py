"""The discovery loop: one topic in, a library out.

::

    once:
      write brief -> review brief              (M2b)
      map the territory -> concepts, vocabulary, recency, queries   (B4)

    round:
      expand brief -> queries, plus the map's own queries in round 1
      validate every filter before it executes (B4)
      search each query -> candidates          (M2a, unchanged)
      gates 1 and 2                            (M2a, unchanged)
      stage 1, batched -> yes / no / maybe
      maybe -> fetch -> stage 2 -> yes / no + threads
      yes -> store
      decide whether to run again

The loop-back is the whole point. Everything else is plumbing.

**The map runs once, before the first round.** It is what makes a query out of a
word the user never typed — searching "AI agent memory" never returns the video
called "Building ReAct loops with tool calling", and no judge can rescue a video
that was never returned. Revising the map between rounds is B5's planner and is
deliberately not here: this phase builds the map, not the thing that changes it.

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

from ..brain.judge import FULL_PROMPT, Judge
from ..brain.territory import Mapper, validate
from ..config import Config
from ..contracts import (
    EVIDENCE_DEGRADED,
    NEW,
    ORIGIN_BRIEF,
    ORIGIN_THREAD,
    VERDICT_MAYBE,
    VERDICT_NO,
    VERDICT_YES,
    DiscoverRun,
    PlannedQuery,
    RoundReport,
    SearchResult,
    TranscriptResult,
)
from ..core.ids import extract_video_id
from ..core.ledger import (
    FAIL_PERMANENT,
    FAIL_TRANSIENT,
    STAGE_FULL,
    STAGE_METADATA,
    Ledger,
    normalise_topic,
)
from ..core.library import Library, format_timestamp
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
    mapper: Mapper | None = None,
    provider: TranscriptProvider | None = None,
) -> DiscoverRun:
    """Run the loop. Writes nothing to the library when ``dry_run`` is set."""
    limits = config.discover
    max_rounds = max_rounds if max_rounds is not None else limits.max_rounds
    max_videos = max_videos if max_videos is not None else limits.max_videos
    max_requests = max_requests if max_requests is not None else limits.max_requests

    normalised = normalise_topic(topic)
    judge = judge or Judge(client)
    mapper = mapper or Mapper(client)
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

    # After the run row exists, so a crash mid-mapping leaves a run with no
    # `finished_at` rather than nothing at all — the same ordering rule the
    # brief follows, applied to the second thing that costs a request.
    run.territory = mapper.map(brief, count=config.discover.territory_queries)
    ledger.save_territory(run_id, run.territory.as_json(), run.territory.version)
    if run.territory.rejected:
        log.info(
            "discover: %d proposed filter(s) rejected before any search ran",
            len(run.territory.rejected),
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
    # Identity is (text, filters), so the same query by `relevance` and by
    # `date` are two searches rather than one. That is the width `order` buys.
    seen_queries: set[tuple] = set()
    quiet_rounds = 0

    for number in range(1, max_rounds + 1):
        if client.usage.requests + _ROUND_OVERHEAD_REQUESTS > max_requests:
            run.stopped_because = STOP_MAX_REQUESTS
            return
        if _overspent(config, client):
            run.stopped_because = STOP_MAX_COST
            return

        planned = _plan_round(config, judge, run, number, threads)
        planned, rejections = validate(
            planned, recency=run.territory.recency, topic=run.brief.topic,
            already_run=seen_queries,
        )
        seen_queries.update(query.key for query in planned)

        report = RoundReport(
            number=number,
            queries=[query.text for query in planned],
            planned=planned,
            filters_rejected=len(rejections),
            from_threads=bool(threads),
        )
        run.rounds.append(report)
        # On the run rather than only on the map: a filter rejected in round two
        # was proposed by query expansion, not by the mapper, and folding the
        # two together would make the map look like the source of both.
        run.rejected_filters.extend(rejections)

        if not planned:
            run.stopped_because = STOP_NO_NEW_VIDEOS
            return

        candidates = _gather(config, ledger, run, planned, report)
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


def _plan_round(
    config: Config,
    judge: Judge,
    run: DiscoverRun,
    number: int,
    threads: Sequence[str],
) -> list[PlannedQuery]:
    """The queries this round will run, each carrying where it came from.

    Round one runs two sets, and running both is the point. The brief's own
    expansion is what discovery did before this phase; the map's queries are
    written from words the topic never contained. Keeping them apart in the
    ledger — by ``origin`` — is what turns "did the map help?" from an opinion
    into a query against the `queries` table.

    Later rounds are the loop-back, unchanged: concepts stage 2 found mentioned
    but unexplained become the next round's searches. The map is not consulted
    again, and not revised; revising it is B5's planner.
    """
    expanded = judge.expand_queries(
        run.brief, count=config.discover.queries_per_round, concepts=list(threads)[:8]
    )
    origin = ORIGIN_THREAD if threads else ORIGIN_BRIEF
    detail = ", ".join(list(threads)[:8]) if threads else run.brief.topic
    planned = [
        PlannedQuery(text=text, origin=origin, origin_detail=detail) for text in expanded
    ]

    if number == 1:
        # After the brief's, not before: the brief's queries are the ones this
        # loop already trusted, and a map that returns nothing must leave the
        # round exactly as it was.
        planned.extend(run.territory.queries)
    return planned


def _gather(
    config: Config,
    ledger: Ledger,
    run: DiscoverRun,
    queries: Sequence[PlannedQuery],
    report: RoundReport,
) -> list[SearchResult] | None:
    """Search every query and apply gates 1 and 2. Returns survivors, in rank order.

    Also the only writer of the `queries` table. A row per search, with what it
    asked for and what it returned — because *"why did it search for that?"* is
    the first question asked when a round goes wrong, and without these rows a
    run's queries are a list with no causal story.
    """
    survivors: dict[str, SearchResult] = {}
    answered = False

    for query in queries:
        try:
            result = run_search(
                config,
                ledger,
                query.text,
                limit=config.discover.results_per_query,
                topic=run.topic,
                log_decisions=not run.dry_run,
                run_id=run.run_id,
                filters=query.filters or None,
            )
        except (PermanentError, TransientError) as exc:
            # One dead query must not end a round that has nine good ones.
            log.warning("discover: search failed for %r: %s", query.text, exc)
            # Still recorded, with no result count. A query that failed is a
            # query that ran, and a table that only holds the successful ones
            # would answer "what did this round search for?" wrongly.
            _log_query(ledger, run, report.number, query, results=None, provider=None, units=0)
            continue

        answered = True
        report.candidates += len(result.candidates)
        report.known += len(result.of("known"))
        report.rejected_before += len(result.of("rejected"))
        _log_query(
            ledger,
            run,
            report.number,
            query,
            results=len(result.candidates),
            provider=result.provider,
            units=result.quota_units,
        )

        for candidate in result.of(NEW):
            # Which origins surfaced this video, not just the first. "Would the
            # old expansion have found it?" is a question about *all* the
            # queries that returned it, and first-past-the-post would answer it
            # with whichever set happened to run first.
            report.surfaced_by.setdefault(candidate.video_id, set()).add(query.origin)
            survivors.setdefault(candidate.video_id, candidate.result)

    if not answered:
        return None
    return list(survivors.values())


def _log_query(
    ledger: Ledger,
    run: DiscoverRun,
    number: int,
    query: PlannedQuery,
    *,
    results: int | None,
    provider: str | None,
    units: int,
) -> None:
    """Record one search. Written even on a dry run, unlike a decision.

    The two are different kinds of record and the difference matters. A `no`
    row poisons Gate 2 for the real run that follows, so a dry run must not
    write one. A queries row poisons nothing — it says what was searched for,
    which a dry run genuinely did, and a dry run whose queries left no trace
    would be the one mode where "why did it search for that?" is unanswerable.
    """
    ledger.log_query(
        run.run_id,
        round=number,
        query=query.text,
        origin=query.origin,
        origin_detail=query.origin_detail or None,
        filters_json=query.filters_json() if query.filters else None,
        results_count=results,
        provider=provider,
        quota_units=units,
    )


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
        run.brief,
        candidates,
        batch_size=config.llm.judge_batch_size,
        # Stage 1 especially. This is where a video whose title never says
        # "agent" is dropped before anything reads it, and the vocabulary is
        # the only thing that can stop that — under the rule, stated in the
        # prompt, that a word from the map is never a reason to keep.
        territory=run.territory,
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
            report.already_held += 1
            continue

        # YouTube already told us there is no caption track, in the same
        # `videos.list` call that supplied the duration. Trying anyway costs a
        # request and arrives at `failed` on the way to `needs_asr`, which is
        # both a wasted fetch and a misleading row in between.
        #
        # A hint, not a verdict, and only in one direction: an explicit False
        # skips, `None` and True fetch exactly as before. Never a reason to
        # reject — this is a caption fact, not a content fact, and the verdict
        # above already stands.
        #
        # **Off by default**, and that is a measurement rather than caution. The
        # API reported `caption: "false"` for 31 of the 37 videos in this library
        # that yielded a working transcript, because it counts only *manually
        # uploaded* tracks: 6 of 6 manual right, 31 of 31 auto wrong. Trusting it
        # would route most of what discovery finds into `needs_asr`, whose worker
        # does not exist until M4. See `DEFAULT_TRUST_CAPTION_HINT`.
        if config.discover.trust_caption_hint and candidate.has_captions is False:
            log.info(
                "discover: %s reports no caption track; routing to ASR unfetched",
                candidate.video_id,
            )
            # `mark_pending` is the only insert, so the row has to pass through
            # it even though nothing is in flight. It resolves in the next
            # statement with no network call between them, so the `pending`
            # that rule 6 watches for cannot be observed by anyone.
            ledger.mark_pending(candidate.video_id)
            ledger.mark_needs_asr(candidate.video_id)
            report.no_captions += 1
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
            # The whole transcript, not the first thousand words. An opening
            # cannot distinguish a video that delivers from one that promises
            # well and never arrives — the two are identical for a thousand
            # words, and padding is exactly what stage 2 exists to catch.
            #
            # Built from the lines in hand rather than read back from disk.
            # `brain/` cannot reach the filesystem, so "the judge never reads a
            # stored transcript into a report" is structural here, not careful.
            judgement = judge.judge_full(
                run.brief,
                candidate,
                transcript=_transcript_text(fetched),
                chapters=[chapter.get("title", "") for chapter in fetched.chapters][:12],
                duration_s=fetched.duration_s or candidate.duration_s,
                territory=run.territory,
            )
            _log(
                ledger,
                run,
                candidate.video_id,
                STAGE_FULL,
                judgement.verdict,
                judgement.reason,
                evidence=judgement.evidence,
                prompt_version=judge.prompt(FULL_PROMPT).version,
            )
            concepts.extend(judgement.concepts)
            if not judgement.parsed:
                report.unparsed += 1
            if judgement.evidence == EVIDENCE_DEGRADED:
                # Kept, stored, and deliberately not noted. The transcript is
                # worth keeping — it is the thing ASR would replace — but a
                # note written from text the judge could not read would be
                # loaded by `/ask` with no way to know it was noise.
                report.degraded += 1
                log.info(
                    "discover: %s kept on degraded evidence; no note will be written",
                    candidate.video_id,
                )
            if judgement.verdict == VERDICT_NO:
                report.flipped_to_no += 1
                # Keep the bytes. Acquisition is the one step this project
                # calls fragile, and it is fragile in a measured way — thirteen
                # of thirteen fetches died in nine seconds when YouTube
                # throttled a run. Throwing away a transcript already in hand
                # means paying that risk again for content we have.
                #
                # What keeps it out of the library is the absence of a *note*,
                # not the absence of a transcript. `in_library` stays False, so
                # a later topic still gets to judge this video on its own
                # terms — and by then the download is already done.
                _store(library, ledger, candidate, fetched, config)
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


def _transcript_text(result: TranscriptResult) -> str:
    """The transcript as timestamped lines, for stage 2 to read.

    Timestamps included because the judge is asked to name *where* the value is,
    and a range it cannot see is a range it will invent. The format matches what
    `library.write_transcript` puts on disk, so the judge reads the same shape a
    person opening the file would.

    Built from the lines already in memory. Reading the stored file back would
    work and is deliberately not done: it would create a path from disk to a
    model prompt inside `flow/`, and the one rule about transcripts is that they
    do not travel.
    """
    return "\n".join(
        f"[{format_timestamp(line.start_ms)}] {line.text}" for line in result.lines
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
    ledger: Ledger,
    run: DiscoverRun,
    video_id: str,
    stage: str,
    verdict: str,
    reason: str,
    *,
    evidence: str | None = None,
    prompt_version: str | None = None,
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
        evidence=evidence,
        prompt_version=prompt_version,
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
