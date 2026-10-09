"""The discovery loop: one topic in, a library out.

::

    once:
      write brief -> review brief              (M2b)
      map the territory -> concepts, vocabulary, recency, queries   (B4)

    round:
      expand brief -> queries, plus the map's own queries in round 1
      validate every filter before it executes (B4)
      search each query -> candidates          (M2a, unchanged)
      gates 1 and 2, including what this run already stored
      stage 1, batched -> no / fetch-or-not
      yes or maybe -> check the ceilings -> fetch -> store -> stage 2
      stage-2 yes -> counts as coverage, its concepts become threads
      stage-2 no -> kept on disk, does not count (both get a note later)
      planner, then decide whether to run again

The loop-back is the whole point. Everything else is plumbing.

**Stage 1 decides whether to fetch. Stage 2 decides whether to keep.** A
stage-1 YES used to skip stage 2 as the cheaper keeper, which meant a video
could enter the library without anything having read it — and B3 measured this
judge inventing content from titles, so "nothing read it" is a real risk and not
a tidiness complaint. Every candidate that will be stored is now judged on its
transcript.

**The map runs once, before the first round.** It is what makes a query out of a
word the user never typed — searching "AI agent memory" never returns the video
called "Building ReAct loops with tool calling", and no judge can rescue a video
that was never returned. Revising it between rounds is the planner's job (B5), and
the planner's revisions are applied here, after each round's plan.

**Threads come free.** Stage 2 already reads the transcript, so it returns the
concepts a video mentions without explaining in the same response — no second
request. A kept video on prompt caching that says "KV cache" and moves on has
written round two's query for us. Only kept videos contribute: a rejected
video's jargon is usually the subject the judge just turned down.

Cost discipline runs through the whole file. Model requests are the scarce
resource on a free tier, so: gates run before judging, judging runs before
fetching, and every stopping condition is checked before spending rather than
after.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Sequence

from ..brain.judge import FULL_PROMPT, Judge
from ..brain.planner import PLAN_PROMPT, Planner, apply_revisions
from ..brain.territory import Mapper, validate
from ..config import Config
from ..contracts import (
    EVIDENCE_CLEAR,
    EVIDENCE_DEGRADED,
    KNOWN,
    NEW,
    ORIGIN_BRIEF,
    ORIGIN_THREAD,
    RECOMMEND_STOP,
    REJECTED,
    STORED_THIS_RUN,
    VERDICT_MAYBE,
    VERDICT_NO,
    VERDICT_YES,
    DiscoverRun,
    JudgedVideo,
    LibraryCoverage,
    PlannedQuery,
    RoundPlan,
    RoundReport,
    SearchFilters,
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
from ..ask import _CHARS_PER_TOKEN, load_notes, summarise
from ..core.library import Library, format_timestamp, render_transcript
from ..llm import LlmClient
from ..providers import PermanentError, TransientError, get_provider
from ..providers.base import TranscriptProvider
from .search import quota_headroom, run_search
from .store import store_transcript

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
# B5. The planner said the brief is covered. **Kept distinct from every budget
# ceiling above it**, and that is the whole reason it is its own word: a run
# that stopped because the brief was satisfied and a run that stopped because it
# ran out of money are opposite outcomes, and one integer holding both would
# hide the interesting one. A planner declaring satisfaction in round one is a
# real failure mode, and it is only findable if it is labelled.
STOP_COVERAGE_SATISFIED = "coverage_satisfied"
# The search allowance could not pay for the next round. Checked *before* the
# round rather than discovered inside it: a round that dies halfway leaves the
# planner reading half a round's evidence as though it were the whole thing.
STOP_QUOTA = "quota"
STOP_REASONS = (
    STOP_MAX_ROUNDS,
    STOP_NO_NEW_VIDEOS,
    STOP_MAX_VIDEOS,
    STOP_MAX_REQUESTS,
    STOP_MAX_COST,
    STOP_NO_SEARCH,
    STOP_DRY_RUN,
    STOP_COVERAGE_SATISFIED,
    STOP_QUOTA,
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
    planner: Planner | None = None,
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
    planner = planner or Planner(client)
    run_id = _new_run_id()

    # The brief costs two requests and must exist before the run row can be
    # written — but the row must exist before any *judging* happens, which is
    # what the ordering rule is protecting. A crash during brief-writing leaves
    # no run, and also no decisions to explain.
    brief = judge.write_brief(topic, max_tokens=config.discover.brief_max_tokens)

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
    run.territory = mapper.map(
        brief,
        count=config.discover.territory_queries,
        max_tokens=config.discover.territory_max_tokens,
    )
    ledger.save_territory(run_id, run.territory.as_json(), run.territory.version)

    # What the library *already* answers, before a single search runs.
    #
    # The planner cannot see this any other way. Gate 1 drops videos that have
    # notes before they become candidates, and the fetch-step disk check skips
    # stored-but-unnoted ones — so neither reaches `report.kept`, and "no kept
    # video covers X" has always meant "no video this run newly fetched covers
    # X". Measured: a run called retrieval-scoring a gap while a note written
    # the previous day gave the exact formula the gap asked for, and the next
    # round spent five of six queries re-finding it.
    #
    # Skipped on a dry run and on an empty library — the first plans nothing,
    # and the second has nothing to assess.
    run.baseline = _assess_library(config, ledger, library, planner, run)
    if run.territory.rejected:
        log.info(
            "discover: %d proposed filter(s) rejected before any search ran",
            len(run.territory.rejected),
        )

    try:
        _loop(
            config, ledger, library, client, judge, planner, run, provider,
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
    planner: Planner,
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
    # What the planner asked for at the end of the previous round. Empty in
    # round one, which is what makes round one the same round it always was.
    from_planner: list[PlannedQuery] = []

    for number in range(1, max_rounds + 1):
        if client.usage.requests + _ROUND_OVERHEAD_REQUESTS > max_requests:
            run.stopped_because = STOP_MAX_REQUESTS
            return
        if _overspent(config, client):
            run.stopped_because = STOP_MAX_COST
            return

        planned = _plan_round(config, judge, run, number, threads, from_planner)
        # Validated again, and the planner's queries were validated once already
        # when the plan was made. That is deliberate rather than sloppy: this is
        # the only gate between a proposed filter and a search that excludes,
        # and it is a pure function, so running it twice costs nothing and means
        # no future path can reach the provider around it.
        planned, rejections = validate(
            planned, recency=run.territory.recency, topic=run.brief.topic,
            already_run=seen_queries,
        )
        seen_queries.update(query.key for query in planned)

        # Before anything is spent, and before the round row is written. A
        # round that runs out of quota halfway through leaves the planner
        # reading a partial round as though it were a whole one — which is the
        # one input this design must never produce.
        short = _quota_short(config, ledger, len(planned))
        if short is not None:
            log.warning("discover: %s", short)
            run.stopped_because = STOP_QUOTA
            return

        report = RoundReport(
            number=number,
            queries=[query.text for query in planned],
            planned=planned,
            # Three outcomes, three counters. A refused filter widened a search
            # that ran; a skipped duplicate means a search did not run at all;
            # a waiver means the reasoned date window was kept off one query.
            # One number for the first two told a round it had "8 filters
            # rejected" when every one was a duplicate skip.
            filters_rejected=sum(
                1
                for item in rejections
                if not item.is_duplicate_query and not item.is_recency_waiver
            ),
            duplicates_skipped=sum(
                1 for item in rejections if item.is_duplicate_query
            ),
            recency_waived=sum(
                1 for item in rejections if item.is_recency_waiver
            ),
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

        searched: list[tuple[PlannedQuery, int | None]] = []
        candidates = _gather(config, ledger, run, planned, report, searched)
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

        # The planning call, and only if another round could actually run.
        # Planning the end of the last round is a request spent on advice
        # nobody will read — the same discipline as checking the ceilings
        # before the round rather than after it.
        if number >= max_rounds:
            run.stopped_because = STOP_MAX_ROUNDS
            return
        if client.usage.requests + _ROUND_OVERHEAD_REQUESTS >= max_requests:
            run.stopped_because = STOP_MAX_REQUESTS
            return

        plan = _run_planner(
            config, ledger, planner, run, report, searched, seen_queries,
            rounds_left=max_rounds - number,
        )
        from_planner = list(plan.queries)

        # Advisory, never sovereign. Budget ceilings are checked above and
        # below this; what the planner gets is a stop reason of its own, so
        # "the brief is covered" is never read back as "the money ran out".
        if plan.wants_to_stop:
            log.info("discover: planner recommends stopping — %s", plan.reason)
            run.stopped_because = STOP_COVERAGE_SATISFIED
            return
        if plan.recommendation == RECOMMEND_STOP:
            # Said, and not acted on: the plan behind it did not parse.
            log.warning(
                "discover: the planner said STOP in a reply that did not parse (%s) — continuing",
                plan.problem or "no coverage assessment",
            )

    run.stopped_because = STOP_MAX_ROUNDS


def _assess_library(
    config: Config,
    ledger: Ledger,
    library: Library,
    planner: Planner,
    run: DiscoverRun,
) -> LibraryCoverage:
    """One call over the notes already on disk. Returns empty when there are none.

    The notes are loaded **here** rather than in `brain/`, which may not touch
    the filesystem. Same arrangement as stage 2, where the transcript is read by
    the loop and handed to the judge: it makes "the reasoning layer never opens a
    file" a property of the import graph instead of a habit.
    """
    if run.dry_run:
        return LibraryCoverage()
    notes = load_notes(library, ledger)
    if not notes:
        # A fresh topic on an empty library. Nothing to assess, and the call
        # would cost the whole prompt to be told so.
        log.info("discover: the library holds no notes; nothing to assess")
        return LibraryCoverage()

    # **Summary lines, not whole notes.** This used to send the library entire,
    # with no budget check of any kind, and at 646 notes it asked for 1,833,240
    # tokens against a 1,048,576 ceiling — 1.75x over. It failed *open*
    # ("assuming nothing is covered"), so nothing crashed and the planner simply
    # worked as though the library held nothing, re-searching ground already on
    # disk. That is why nobody noticed.
    #
    # The question here is coarse — *which brief items does the library already
    # cover?* — and a coarse question does not need whole notes. One line per
    # note is what the selector's stage 1 already builds, and 646 of them is
    # about 20k tokens against 1.87M.
    #
    # **Not the M7 selector**, which was the obvious repair and the wrong one:
    # selection would eventually hit this same ceiling again, and this is the
    # second caller to hit it already. Summary lines scale indefinitely — 5,000
    # notes is still ~150k tokens — cost nothing against a selection pass, and
    # when they are too thin to decide they err toward "not covered", which
    # means more searching rather than less. Rule 8's direction.
    summaries = [summarise(note) for note in notes]
    rendered = "\n".join(summary.line for summary in summaries)

    # The guard the original never had, using the estimator that would have
    # caught this: `estimate_tokens` puts 646 notes at 1,873,146, within 2% of
    # what the provider counted. With summary lines it should never fire, and
    # that is the point — a guard that never fires is cheap, and its absence
    # cost a silent failure on every discovery run since the library crossed the
    # ceiling.
    #
    # `[ask] max_input_tokens` is the one place the provider's context ceiling
    # is recorded, and rule 7 says not to guess a provider fact — so when it is
    # unset there is no ceiling to check against and the guard says so rather
    # than inventing one.
    ceiling = config.ask.max_input_tokens
    estimate = int(len(rendered) / _CHARS_PER_TOKEN)
    if ceiling and estimate > ceiling:
        # Fails open exactly as the 400 did, but *reported* rather than
        # discovered from a provider rejection several seconds later.
        log.warning(
            "discover: the library baseline would send ~%d tokens against a "
            "%d-token ceiling — skipping it, and the planner will assume "
            "nothing is covered",
            estimate,
            ceiling,
        )
        return LibraryCoverage(
            parsed=False,
            problem=(
                f"{len(notes)} summary lines estimate ~{estimate:,} tokens, "
                f"over the {ceiling:,}-token ceiling"
            ),
        )
    log.info(
        "discover: assessing %d notes as summary lines, ~%d tokens", len(notes), estimate
    )

    baseline = planner.assess_library(
        run.brief,
        rendered,
        notes_read=len(notes),
        max_tokens=config.discover.baseline_max_tokens,
    )
    ledger.save_baseline(run.run_id, baseline.as_json())
    log.info(
        "discover: the library already covers %d of %d assessed brief item(s), from %d note(s)",
        len(baseline.satisfied),
        len(baseline.items),
        baseline.notes_read,
    )
    return baseline


def _run_planner(
    config: Config,
    ledger: Ledger,
    planner: Planner,
    run: DiscoverRun,
    report: RoundReport,
    searched: Sequence[tuple[PlannedQuery, int | None]],
    seen_queries: set[tuple],
    *,
    rounds_left: int,
) -> RoundPlan:
    """One call at the end of a round, and everything that follows from it.

    Three outputs, applied here rather than in `brain/`: the queries the next
    round runs, the map revisions, and the recommendation. `brain/` decided all
    three and touched nothing; this is the half that writes.
    """
    remaining, _ = quota_headroom(config, ledger)
    plan = planner.plan(
        run.brief,
        run.territory,
        report,
        searched=searched,
        already_run=seen_queries,
        quota_remaining=remaining,
        # From the ceiling this run is actually working to, not the configured
        # one: `--max-rounds 2` on a config that says 3 means the planner is
        # writing for a round that may not happen, and "how much room is left"
        # is a real input to how ambitious a plan should be.
        rounds_left=max(0, rounds_left),
        count=config.discover.planner_queries,
        # Every video this run has kept, marked with the round that kept it —
        # not just this round's. Coverage is a claim about what the library
        # holds; built from one round's keepers it contradicts itself between
        # rounds, which is exactly what run `ea18299bcea5` did. The run already
        # carries the rounds, so this needs no new state, only for the question
        # to be asked of the right set.
        kept_so_far=[
            (report.number, video)
            for report in run.rounds
            for video in report.kept
        ],
        # What the library already held when this run started. Without it the
        # planner reads an empty shelf and names gaps the library can answer.
        baseline=run.baseline.items,
        max_tokens=config.discover.planner_max_tokens,
    )
    report.plan = plan
    # Stored whole, immediately, for the same reason the map is. Until now a
    # plan survived only as the one-line gap label attached to each query it
    # produced — which says what it asked for and never why, and "why did the
    # planner decide that?" is precisely the question storing runs exists to
    # answer. Written before the revisions are applied, so a crash between the
    # two leaves the reasoning on disk rather than only its consequences.
    ledger.save_plan(
        run.run_id,
        round=report.number,
        plan_json=json.dumps(plan.as_dict(), ensure_ascii=False),
        prompt_version=planner.prompt(PLAN_PROMPT).version,
    )
    # On the run, not on this round's counter. A filter the planner proposed was
    # rejected *between* rounds, for a query the next round would have run —
    # folding it into "filters rejected on the queries this round ran" would put
    # two meanings in one integer, which is the thing rule 14 exists to stop.
    # The plan carries its own list, and the report prints it on its own line.
    run.rejected_filters.extend(plan.rejected_filters)

    if plan.revisions:
        # A new map revision, with the previous one kept beside it. Written to
        # the run immediately: a revision that only exists in memory is a
        # change nobody can inspect afterwards, which is the same argument that
        # put the map on the run in the first place.
        run.territory = apply_revisions(run.territory, plan.revisions)
        ledger.save_territory(
            run.run_id, run.territory.as_json(), run.territory.label
        )
        for revision in plan.revisions:
            log.info("discover: territory %s — %s", run.territory.label, revision.line())

    return plan


def _quota_short(config: Config, ledger: Ledger, queries: int) -> str | None:
    """Why the next round cannot be afforded, or ``None`` if it can.

    ``quota_headroom`` returns ``None`` remaining when an unmetered provider is
    in the chain, and that is not a shortage — the overflow provider exists so
    a run continues when the metered one runs dry. Stopping then would be
    inventing a ceiling that does not apply.
    """
    remaining, per_search = quota_headroom(config, ledger)
    if remaining is None or per_search <= 0 or queries <= 0:
        return None
    needed = queries * per_search
    if remaining >= needed:
        return None
    return (
        f"search quota cannot pay for this round: {remaining} units left, "
        f"{queries} queries would cost {needed}"
    )


def _plan_round(
    config: Config,
    judge: Judge,
    run: DiscoverRun,
    number: int,
    threads: Sequence[str],
    from_planner: Sequence[PlannedQuery] = (),
) -> list[PlannedQuery]:
    """The queries this round will run, each carrying where it came from.

    Round one runs two sets, and running both is the point. The brief's own
    expansion is what discovery did before B4; the map's queries are written
    from words the topic never contained. Keeping them apart in the ledger — by
    ``origin`` — is what turns "did the map help?" from an opinion into a query
    against the `queries` table.

    From round two, the planner's queries go **first**, and that ordering is the
    point of B5: they were written against the specific gaps the last round left
    open, while expansion is still working from the brief alone. The expansion
    stays anyway, for the same reason the brief's queries survived B4 — a new
    step that silently replaced a working one would make its own contribution
    unmeasurable, and `queries.origin` is what makes it measurable instead.
    """
    planned = list(from_planner)

    expanded = judge.expand_queries(
        run.brief, count=config.discover.queries_per_round, concepts=list(threads)[:8]
    )
    origin = ORIGIN_THREAD if threads else ORIGIN_BRIEF
    detail = ", ".join(list(threads)[:8]) if threads else run.brief.topic
    planned.extend(
        PlannedQuery(text=text, origin=origin, origin_detail=detail) for text in expanded
    )

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
    searched: list[tuple[PlannedQuery, int | None]] | None = None,
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
                # The run-scoped half of Gate 1. `in_library` cannot see these
                # — their notes are written by a worker that starts after the
                # run ends — so without this every round re-judges everything
                # the previous rounds kept. Measured on run `19941ec23cb7`:
                # `EGNykTqFDBs` was judged and accepted in round 1 and again in
                # round 2. The disk check stopped the re-download; nothing
                # stopped the model call or the second row in the round's
                # evidence, which the planner then read as two findings.
                stored_this_run=run.stored_ids,
            )
        except (PermanentError, TransientError) as exc:
            # One dead query must not end a round that has nine good ones.
            log.warning("discover: search failed for %r: %s", query.text, exc)
            # Still recorded, with no result count. A query that failed is a
            # query that ran, and a table that only holds the successful ones
            # would answer "what did this round search for?" wrongly.
            _log_query(
                ledger, run, report.number, query, results=None, provider=None, units=0
            )
            if searched is not None:
                searched.append((query, None))
            continue

        answered = True
        report.candidates += len(result.candidates)
        report.known += len(result.of(KNOWN))
        report.known_this_run += len(result.of(STORED_THIS_RUN))
        report.rejected_before += len(result.of(REJECTED))
        _log_query(
            ledger,
            run,
            report.number,
            query,
            results=len(result.candidates),
            provider=result.provider,
            units=result.quota_units,
            applied=result.filters_applied,
        )
        if searched is not None:
            searched.append((query, len(result.candidates)))

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
    applied: SearchFilters | None = None,
) -> None:
    """Record one search. Written even on a dry run, unlike a decision.

    The two are different kinds of record and the difference matters. A `no`
    row poisons Gate 2 for the real run that follows, so a dry run must not
    write one. A queries row poisons nothing — it says what was searched for,
    which a dry run genuinely did, and a dry run whose queries left no trace
    would be the one mode where "why did it search for that?" is unanswerable.

    ``filters_json`` records **what actually ran**, not what was asked for. B4
    stored the proposal, which was wrong in exactly one case and wrong in a way
    that matters now: the overflow provider takes no parameters, so a fall-
    through recorded `order=date` for a search that was plain. B5's planner
    reads this history to decide what to try next, and a record of an experiment
    that did not happen is worse than no record at all.
    """
    ran = query.filters if applied is None else applied
    ledger.log_query(
        run.run_id,
        round=number,
        query=query.text,
        origin=query.origin,
        origin_detail=query.origin_detail or None,
        filters_json=query.filters_json(ran) if ran else None,
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
            # Recorded with the judge's own words, because the planner has to
            # read them. Stage 1 is where most rejections happen, so leaving
            # them out would hand the planner a view of the round in which
            # almost nothing was turned away.
            report.rejected.append(
                _judged(by_id[verdict.video_id], STAGE_METADATA, verdict)
            )
            report.verdict_no += 1
        else:
            report.verdict_maybe += 1

    if run.dry_run:
        run.stopped_because = STOP_DRY_RUN
        return []

    # YES first, and no longer because it is cheaper — it is not. Both verdicts
    # now cost a fetch and a stage-2 call, so the ordering buys one thing only:
    # when `max_videos` truncates the round, the candidates stage 1 was
    # confident about are the ones that got the budget.
    ordered = [v for v in verdicts if v.verdict == VERDICT_YES]
    ordered += [v for v in verdicts if v.verdict == VERDICT_MAYBE]

    for verdict in ordered:
        if len(run.stored_ids) >= max_videos:
            run.stopped_because = STOP_MAX_VIDEOS
            return _dedupe_concepts(concepts)

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

        # The ceilings, before the fetch rather than between it and stage 2.
        # Checked after, a run that hit one threw away the transcript it had
        # just paid for and left the row `pending` — rule 6's sign of a fetch
        # that *died*, when what happened was a budget doing its job.
        if client.usage.requests >= max_requests:
            run.stopped_because = STOP_MAX_REQUESTS
            return _dedupe_concepts(concepts)
        if _overspent(config, client):
            run.stopped_because = STOP_MAX_COST
            return _dedupe_concepts(concepts)

        _pace(config, fetched_any)
        fetched_any = True
        fetched = _fetch(ledger, candidate.video_id, provider, config)
        if fetched is None:
            report.failed += 1
            continue

        # **Stored before it is judged.** Rule 12 stores the transcript whatever
        # stage 2 says, so nothing about storing waits on the verdict — and
        # storing first means a judge that raises (a 402, a rejected key, an
        # interrupt) leaves the bytes on disk and the row `has_transcript`,
        # instead of a fetched transcript lost with the process.
        _store(library, ledger, candidate, fetched, config)

        # **Every keeper is read before it is kept**, whatever stage 1 said.
        #
        # This used to skip a stage-1 YES, on the reasoning that it "needs no
        # second model call, so it is the cheaper keeper". The saving is real
        # and tiny — B3 measured stage 2 at 0.09¢ and 2.5s per video — and what
        # it bought was a video entering the library that nothing had read.
        #
        # That is not a hypothetical cost. B3 measured this judge confabulating
        # from metadata: three videos kept on reasons describing numbers their
        # transcripts do not contain. Those were caught only because they were
        # MAYBEs and stage 2 read them. A confident YES never reached the check
        # that would have caught it — and one did exactly that in run
        # `19941ec23cb7`, where the planner then reported a gap the library
        # already held, because the only evidence anything had about the video
        # was one sentence about its title.
        #
        # So stage 1 is a filter that decides whether to *fetch*, and stage 2 is
        # the verdict that decides whether to keep.
        #
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
            relevant=judgement.relevant_text,
        )
        if not judgement.parsed:
            report.unparsed += 1
        if judgement.unmarked_currency:
            # Its own counter, and nothing else happens. The verdict stands —
            # dating an API is usually a correct judgement and the marker is
            # about attribution, not about relevance — so what this measures is
            # how much era knowledge is carrying verdicts without saying so.
            report.unmarked_currency += 1
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
            report.rejected.append(_judged(candidate, STAGE_FULL, judgement))
            # Two counters, because they mean different things. A MAYBE that
            # stage 2 declines is stage 2 doing its job; a YES that stage 2
            # declines is stage 1 having been wrong, and that is the number
            # this change exists to make visible.
            if verdict.verdict == VERDICT_YES:
                report.overturned += 1
                log.info(
                    "discover: %s was a stage-1 YES and stage 2 declined it — %s",
                    candidate.video_id,
                    judgement.reason,
                )
            else:
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
            # terms — and by then the download is already done. (Stored
            # above, before the judge was asked anything.)
            continue

        # Only a kept video's unexplained concepts feed the next round. Both
        # prompts that read them — query expansion and the planner — say they
        # come from videos judged relevant, and a rejected video's jargon is
        # often the other meaning of a shared word: the mistake the stage-2
        # prompt says once cost eight good videos, fed forward as a query.
        concepts.extend(judgement.concepts)

        # Knowable now, and only now. The note worker refuses a transcript past
        # its ceiling, which happens after this run has ended — so without this
        # check the planner counts the video as coverage and `/ask` can never
        # reach it. Four of one run's 24 keepers were in exactly that state.
        #
        # Measured in the stored file's own format, because that is what the
        # worker measures. The judge's rendering is shorter per line, so
        # checking it here undercounted videos the worker would then refuse.
        stored_chars = len(render_transcript(fetched.lines))
        if stored_chars > config.note.max_transcript_chars:
            report.too_long_to_note += 1
            log.info(
                "discover: %s is %d characters, past the note ceiling of %d; "
                "it is stored but will never be noted",
                candidate.video_id,
                stored_chars,
                config.note.max_transcript_chars,
            )

        run.stored_ids.append(candidate.video_id)
        # The other half of what the planner reads, and the half that makes
        # coverage answerable at all. A planner shown only rejections can only
        # ever learn what to avoid; it needs what was kept to say which part of
        # the brief is already satisfied.
        #
        # Always the stage-2 judgement now. There is no longer a keeper whose
        # reason describes a title rather than a transcript, which is what made
        # a coverage claim built on one a claim built on a guess.
        report.kept.append(_judged(candidate, STAGE_FULL, judgement))
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
    store_transcript(
        library,
        ledger,
        candidate.video_id,
        result,
        provider=config.transcript_order[0],
        title=candidate.title,
        channel=candidate.channel,
    )


def _judged(
    candidate: SearchResult, stage: str, verdict, *, too_long: bool = False
) -> JudgedVideo:
    """One decision, as the planner will be shown it.

    Title and reason only. **No description and no transcript** — the reason is
    the judge's one-sentence account of a decision it made after reading, and
    that sentence is the entire evidence the planner gets about the video. What
    keeps hard rule 1 true here is that :class:`JudgedVideo` has nowhere to put
    anything larger.

    ``evidence`` comes across too, and only stage 2 has one to give: a
    ``MetadataVerdict`` never read a transcript and so never claims its evidence
    was clear or damaged. Defaulting a stage-1 verdict to ``CLEAR`` is the
    honest reading — the metadata was legible — and it keeps the discriminator
    meaning one thing, which is *stage 2 could not read the transcript*.

    The only stage-1 verdicts that now reach here are **rejections**. Every
    keeper is judged on its transcript, so a kept video's grade is always one
    stage 2 actually assigned rather than a default standing in for a reader
    nobody sent.
    """
    return JudgedVideo(
        video_id=candidate.video_id,
        title=candidate.title,
        stage=stage,
        verdict=verdict.verdict,
        reason=verdict.reason,
        evidence=getattr(verdict, "evidence", EVIDENCE_CLEAR),
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
    relevant: str | None = None,
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
        relevant=relevant,
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
