"""The discovery loop: one topic in, a library out.

::

    once:
      write brief -> review brief              (M2b)
      the search plan -> items, title phrasing, languages, queries  (rule 39)
        or the territory map -> concepts, vocabulary, recency, queries   (B4)
        ([discover] search_source, `plan` by default; a plan that cannot be
        used runs the map)

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
from dataclasses import replace
import logging
import re
import time
import uuid
from typing import Any, Callable, Hashable, Mapping, Sequence, TypeVar

from ..brain.judge import (
    FULL_PROMPT,
    METADATA_PROMPT,
    RESOLVED_UNSHOWN,
    Judge,
    aim_words,
    item_lines,
    match_item,
)
from ..brain.planner import PLAN_PROMPT, Planner, apply_revisions
from ..brain.search_plan import SearchPlanner
from .web_context import context_lines, gather_context
from ..brain.territory import Mapper, validate, waiver_applies
from ..config import Config
from ..contracts import (
    EVIDENCE_CLEAR,
    EVIDENCE_DEGRADED,
    ITEM_HELD,
    ITEM_MISSING,
    ITEM_PARTLY,
    ITEM_STATES,
    ITEM_UNTAGGED,
    KNOWN,
    LoadedNote,
    NEW,
    ORIGIN_BRIEF,
    ORIGIN_THREAD,
    SOURCE_PLAN,
    WEB_CONTEXT_OFF,
    ACQUIRE_NO_CAPTIONS,
    ACQUIRE_TRANSLATED_ONLY,
    OUTCOME_JUDGED,
    OUTCOME_TOO_LARGE,
    RECOMMEND_STOP,
    STOP_REFUSED,
    REJECTED,
    STATUS_FAILED,
    STORED_THIS_RUN,
    VERDICT_MAYBE,
    VERDICT_NO,
    VERDICT_YES,
    CoverageItem,
    DiscoverRun,
    JudgedVideo,
    LibraryCoverage,
    MetadataVerdict,
    PlannedQuery,
    RoundPlan,
    RoundReport,
    UnreadCandidate,
    SearchFilters,
    SearchResult,
    TranscriptResult,
    Acquisition,
    Brief,
    TerritoryMap,
)
from ..core.ids import extract_video_id
from ..core.ledger import (
    FAIL_PERMANENT,
    STAGE_FULL,
    STAGE_METADATA,
    Ledger,
    judged_for_topic,
    normalise_topic,
)
from ..core.ledger import now as ledger_now
from ..ask import _CHARS_PER_TOKEN, load_notes, summarise
from ..quotes import note_sections
from ..core.library import Library, format_timestamp, render_transcript
from ..llm import LlmClient, LlmConfigError
from ..providers import PermanentError, TransientError
from ..providers.base import TranscriptProvider
from .acquire import acquire, transcript_providers
from .brief import run_brief
from .search import quota_headroom, run_search
from ..enrich.note import note_refusal
from .store import reconcile_stored, store_transcript

log = logging.getLogger(__name__)

T = TypeVar("T")

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
# Stage-2 requests kept overflowing the model's context window with none fitting
# between them: the configuration, not the videos. Its own word, so it reads
# neither as a crash nor as a provider refusal.
STOP_CONTEXT_WINDOW = "context_window"
# How many stage-2 overflows in a row, with nothing fitting between them, end the
# run. Above the client's own three: three long videos in a row are ordinary
# (R1 F5) and must not end a run; five with nothing fitting is a setting.
_OVERFLOW_STOP = 5
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
    STOP_REFUSED,
    STOP_CONTEXT_WINDOW,
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
    search_planner: SearchPlanner | None = None,
    search_source: str | None = None,
    web_context: bool | None = None,
    web_search: object | None = None,
) -> DiscoverRun:
    """Run the loop.

    With ``dry_run`` set it fetches nothing and stores nothing: no transcript,
    no note, no ``videos`` row and no ``decisions`` row — the last because a
    ``no`` row would poison Gate 2 for the real run that follows. It is not
    inert. The ledger still records the run with its brief, its territory map,
    every query it searched and the search quota those queries spent, because
    the searches really ran and the quota is really gone.
    """
    limits = config.discover
    max_rounds = max_rounds if max_rounds is not None else limits.max_rounds
    max_videos = max_videos if max_videos is not None else limits.max_videos
    max_requests = max_requests if max_requests is not None else limits.max_requests

    normalised = normalise_topic(topic)
    # Resolved before anything is written: a provider name the registry does
    # not know is a configuration error, not a fact about the first video.
    providers = [provider] if provider is not None else transcript_providers(config)
    judge = judge or Judge(client)
    mapper = mapper or Mapper(client)
    search_planner = search_planner or SearchPlanner(client)
    planner = planner or Planner(client)
    run_id = _new_run_id()

    # The brief costs two requests and must exist before the run row can be
    # written — but the row must exist before any *judging* happens, which is
    # what the ordering rule is protecting. A crash during brief-writing leaves
    # no run, and also no decisions to explain. So does `BriefUnusable`, raised
    # when no draft was usable: stopping here spends nothing beyond the calls
    # already made, where a run on a brief written from the topic alone would
    # spend quota and store what that brief let through.
    #
    # **The topic's approved brief, when it has one.** Every run used to write
    # a fresh brief, so the PARTS and their listed things moved between runs of
    # one topic and coverage was judged against whichever brief a run drew.
    # Reused, it costs no call, and every run of the topic shares one target.
    brief = run_brief(config, ledger, judge, topic)

    ledger.start_run(
        run_id,
        normalised,
        brief=brief.text,
        brief_version=brief.version,
        model=client.model,
        # A retried draft or an unreviewed brief is recorded on the run, not
        # only logged (rule 14). Stored whenever there is a brief at all, so a
        # NULL means a run made before drafts were checked.
        brief_outcome=json.dumps(brief.outcome),
    )

    run = DiscoverRun(
        run_id=run_id,
        topic=normalised,
        brief=brief,
        model=client.model,
        dry_run=dry_run,
    )

    try:
        # What the library *already* answers, before a single search runs.
        #
        # The planner cannot see this any other way. Gate 1 drops videos that
        # have notes before they become candidates, and the fetch-step disk
        # check skips stored-but-unnoted ones — so neither reaches
        # `report.kept`, and "no kept video covers X" has always meant "no
        # video this run newly fetched covers X". Measured: a run called
        # retrieval-scoring a gap while a note written the previous day gave
        # the exact formula the gap asked for, and the next round spent five of
        # six queries re-finding it.
        #
        # Skipped on a dry run and on an empty library — the first plans
        # nothing, and the second has nothing to assess.
        #
        # **Before round one's source, for both sources.** The search plan is
        # written from it: made first, the plan spent its queries one per PART
        # whatever the library held — on one topic all 8 went to parts the
        # baseline held, while no listed thing got a query. The map reads no
        # coverage, so for it the order changes nothing but the order; one
        # ordering for both keeps the two arms of a comparison alike.
        run.baseline = _assess_library(config, ledger, library, planner, run)

        # After the run row exists, so a crash mid-mapping leaves a run on the
        # ledger rather than nothing at all — the same ordering rule the brief
        # follows, applied to the next thing that costs a request. Inside the
        # `try`, so a refusal here is recorded as one rather than leaving the
        # run unfinished as though the process had died.
        source = search_source or config.discover.search_source
        # Step 4: the start-of-run web lookup, after the baseline (its queries
        # are aimed by the same coverage the plan is) and before the plan,
        # whose CURRENT CONTEXT slot is the only place its lines go. Only for
        # the plan: the map has no slot. Never a reason to stop the run.
        enabled = (web_context if web_context is not None else config.discover.web_context)
        run.web_context = gather_context(
            config, client, brief, plan_coverage(run.baseline)[0],
            enabled=bool(enabled) and source == SOURCE_PLAN, provider=web_search,
        )
        if run.web_context.status != WEB_CONTEXT_OFF:
            ledger.save_web_context(run_id, run.web_context.as_dict())
        elif enabled and source != SOURCE_PLAN:
            run.web_context.reason = "the map has no context slot"

        run.territory = round_one_source(
            config, mapper, search_planner, brief,
            source=source,
            baseline=run.baseline,
            context=context_lines(run.web_context),
        )
        ledger.save_territory(run_id, run.territory.as_json(), run.territory.version)
        if run.territory.rejected:
            log.info(
                "discover: %d proposed filter(s) rejected before any search ran",
                len(run.territory.rejected),
            )

        _loop(
            config, ledger, library, client, judge, planner, run, providers,
            max_rounds=max_rounds, max_videos=max_videos, max_requests=max_requests,
        )
    except LlmConfigError:
        # The provider refused — billing, a rejected key, a wait longer than the
        # run. Its own stop reason, the note drain's word for the same thing:
        # recorded as `incomplete` it read exactly like a crash.
        run.stopped_because = STOP_REFUSED
        raise
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


def plan_coverage(
    baseline: LibraryCoverage | None,
) -> tuple[list[CoverageItem], str, str]:
    """``(coverage, source, note)``: what the search plan is told the library holds.

    The baseline's items when it could be used. Otherwise none, so the plan
    proceeds exactly as on a fresh topic — every item missing — and says why:
    nothing held (an empty library, a dry run), or a baseline that failed or
    was degraded. A degraded baseline saw cut evidence, and a plan that spent
    its budget on its word would skip gaps it merely could not see; a fresh
    plan's cost is some queries on held items, which is the cheap direction.
    """
    if baseline is None or (not baseline.items and baseline.parsed and not baseline.problem
                            and not baseline.notes_read):
        return [], "fresh", "nothing is held for this topic"
    if not baseline.parsed or baseline.problem:
        why = baseline.problem or "its reply could not be read"
        return [], "unusable", f"the library baseline failed ({why}); planned as a fresh topic"
    if baseline.degraded:
        return [], "unusable", (
            f"the library baseline was degraded ({baseline.evidence_mode}, "
            f"{baseline.notes_cut} notes cut); planned as a fresh topic"
        )
    if not baseline.items:
        return [], "unusable", "the library baseline assessed no items; planned as a fresh topic"
    return list(baseline.items), "baseline", (
        f"the library baseline: {len(baseline.satisfied)} of {len(baseline.items)} item(s) held, "
        f"from {baseline.notes_read} note(s)"
    )


def round_one_source(
    config: Config,
    mapper: Mapper,
    search_planner: SearchPlanner,
    brief: Brief,
    *,
    source: str,
    context: Sequence[str] | None = None,
    baseline: LibraryCoverage | None = None,
) -> TerritoryMap:
    """What writes round one's planned queries: the search plan or the map.

    Either way the result is the run's territory, and everything that reads it
    — the recency window, round one, the judges' word list, the planner and its
    revisions, the report, the export — reads whichever ran (rule 25).

    **A failed plan falls back to the map, never to nothing** (rule 30). A
    plan with no query left after its checks — the call failed, the reply was
    cut at the ceiling, or no item named a PART of the brief — is replaced by
    the map, and the plan's problem travels on the map as `fallback`, so the
    run row, the plan line and the export say the plan was asked for and why
    it did not run. `context` is step 4's slot and is absent today.
    """
    limits = config.discover
    if source == SOURCE_PLAN:
        coverage, coverage_source, coverage_note = plan_coverage(baseline)
        plan = search_planner.plan(
            brief,
            count=limits.territory_queries,
            max_tokens=limits.territory_max_tokens,
            context=context,
            coverage=coverage,
            coverage_source=coverage_source,
            coverage_note=coverage_note,
        )
        if plan.queries:
            return plan
        reason = plan.problem or "no queries"
        log.warning("discover: the search plan was unusable (%s); running the map", reason)
        territory = mapper.map(
            brief, count=limits.territory_queries, max_tokens=limits.territory_max_tokens
        )
        return replace(
            territory,
            fallback=f"search plan {plan.version} unusable: {reason}",
            # What the failed plan cost, so the export can name both calls.
            input_tokens=territory.input_tokens + plan.input_tokens,
            output_tokens=territory.output_tokens + plan.output_tokens,
        )
    return mapper.map(brief, count=limits.territory_queries, max_tokens=limits.territory_max_tokens)


def _loop(
    config: Config,
    ledger: Ledger,
    library: Library,
    client: LlmClient,
    judge: Judge,
    planner: Planner,
    run: DiscoverRun,
    providers: Sequence[TranscriptProvider],
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

        expansion_counts: dict[str, int] = {}
        planned = _plan_round(
            config, judge, run, number, threads, from_planner, expansion_counts
        )
        # Validated again: the map's queries were validated inside `Mapper.map`
        # and the planner's when the plan was made. That is deliberate rather
        # than sloppy — this is the only gate between a proposed filter and a
        # search that excludes, so no future path can reach the provider around
        # it. It costs nothing because `validate` is idempotent, which it was
        # not until checkpoint 2: the first pass dropped a waiver's marker and
        # this pass derived the window onto the queries the map had exempted.
        # A waiver now travels on `PlannedQuery.waiver` and is recorded only by
        # the pass that read it, so this pass adds no second record and no
        # misleading "derived" line; what is new here is only this round's own
        # duplicates and the expansion's queries.
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
            # Counted off the queries this round will search, not off this
            # pass's records: a waiver is recorded where it was read — the map,
            # the planner — so this pass never sees one as new, and counting
            # records here missed every map and planner waiver. One per waived
            # query in the round, however many passes it went through.
            recency_waived=sum(
                1
                for query in planned
                if waiver_applies(
                    query, recency=run.territory.recency, topic=run.brief.topic
                )
            ),
            from_threads=bool(threads),
            expansion_unaimed=expansion_counts.get("unaimed", 0),
            expansion_capped=expansion_counts.get("capped", 0),
            expansion_unmatched=expansion_counts.get("unmatched", 0),
        )
        run.rounds.append(report)
        # On the run rather than only on the map: a filter rejected in round two
        # was proposed by query expansion, not by the mapper, and folding the
        # two together would make the map look like the source of both.
        run.rejected_filters.extend(rejections)

        if not planned:
            run.stopped_because = STOP_NO_NEW_VIDEOS
            _spend_remaining(config, ledger, library, client, judge, run, report, providers,
                             max_videos=max_videos, max_requests=max_requests)
            return

        searched: list[tuple[PlannedQuery, int | None]] = []
        candidates = _gather(config, ledger, run, planned, report, searched, library=library)
        if candidates is None:
            run.stopped_because = STOP_NO_SEARCH
            _spend_remaining(config, ledger, library, client, judge, run, report, providers,
                             max_videos=max_videos, max_requests=max_requests)
            return

        # This round's share of what is left, so round 1 cannot spend the run
        # and leave the planner no round to act in. A single-round run gets it
        # all; so does the last round of any run.
        threads = _judge_round(
            config, ledger, library, client, judge, run, report, candidates, providers,
            max_videos=max_videos, max_requests=max_requests,
            allowance=round_allowance(
                max_videos - len(run.stored_ids), max_rounds - number + 1
            ),
            last=number >= max_rounds,
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

        # Kept, not stored: a round that judged held transcripts relevant found
        # coverage, though it downloaded nothing.
        quiet_rounds = 0 if report.kept else quiet_rounds + 1
        if quiet_rounds >= config.discover.quiet_rounds:
            run.stopped_because = STOP_NO_NEW_VIDEOS
            _spend_remaining(config, ledger, library, client, judge, run, report, providers,
                             max_videos=max_videos, max_requests=max_requests)
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


def _spend_remaining(
    config: Config,
    ledger: Ledger,
    library: Library,
    client: LlmClient,
    judge: Judge,
    run: DiscoverRun,
    report: RoundReport,
    providers: Sequence[TranscriptProvider],
    *,
    max_videos: int,
    max_requests: int,
) -> None:
    """When the run stops for want of anything new, read what is still waiting.

    Rounds take a share of `max_videos` so the planner has rounds to act in. If
    the searching then dries up — a narrow topic whose later rounds find
    nothing — the shares not spent would simply vanish while candidates stage 1
    judged worth reading sit unread. So the rest of the budget goes to them,
    in the same spread order, on the round that stopped. Not done when the run
    stopped on a budget, or because the planner judged the brief covered.
    """
    remaining = max(0, max_videos - len(run.stored_ids))
    if run.dry_run or not run.backlog or remaining == 0:
        return
    stopped = run.stopped_because
    run.stopped_because = ""
    _judge_round(
        config, ledger, library, client, judge, run, report, [], providers,
        max_videos=max_videos, max_requests=max_requests, allowance=remaining, last=True,
    )
    run.stopped_because = run.stopped_because or stopped


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

    # **Evidence, not whole notes and not one line.** This used to send the
    # library entire, with no budget check, and at 646 notes it asked for
    # 1,833,240 tokens against a 1,048,576 ceiling; it failed *open*, so the
    # planner worked as though the library held nothing. Then it sent one index
    # line per note — a title and the first claim, 200 characters — which
    # scaled, and could not show coverage: against a brief whose parts each
    # list several things, no index line could pass a prompt that demands named
    # evidence, and one run's baseline marked 0 of 14 items held while about 25
    # stored keepers' own stage-2 reasons named five of the parts.
    #
    # Now each note brings what can show coverage and scales: the latest
    # stage-2 reason for its video (one line, written by a judge that read the
    # whole transcript) and the note's Claims, cut to a per-note share of a
    # fixed budget. When the budget forces a cut it says so, counted (rule 14).
    entries = [
        _note_evidence(note, summarise(note).published, ledger, run.topic) for note in notes
    ]
    rendered, evidence = library_evidence(entries, budget_tokens=_BASELINE_BUDGET_TOKENS)

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
        "discover: assessing %d notes as %s evidence, ~%d tokens",
        len(notes), evidence.mode, estimate,
    )
    if evidence.mode != EVIDENCE_FULL:
        log.warning(
            "discover: the library baseline is degraded — %s: %d of %d notes' claims "
            "cut to %d characters to fit ~%d tokens",
            evidence.mode, evidence.notes_cut, len(notes), evidence.claims_chars,
            _BASELINE_BUDGET_TOKENS,
        )

    baseline = planner.assess_library(
        run.brief,
        rendered,
        notes_read=len(notes),
        max_tokens=config.discover.baseline_max_tokens,
    )
    baseline = replace(
        baseline,
        evidence_mode=evidence.mode,
        evidence_tokens=estimate,
        claims_chars=evidence.claims_chars,
        notes_cut=evidence.notes_cut,
    )
    ledger.save_baseline(run.run_id, baseline.as_json())
    log.info(
        "discover: the library already covers %d of %d assessed brief item(s), from %d note(s)",
        len(baseline.satisfied),
        len(baseline.items),
        baseline.notes_read,
    )
    return baseline


# -- the baseline's evidence ---------------------------------------------------

# The baseline's input budget, in tokens. 37 notes take about 14k at full size;
# 646 notes cannot all be full, and at this budget get a share of about 600
# characters each — the judge's reason and the first claims. Sized against the
# cost of one call (about $0.015 here), not the model's ceiling: the call runs
# on every discovery.
_BASELINE_BUDGET_TOKENS = 100_000
# Per-note ceilings at full size. A stage-2 reason is one sentence; Claims are
# the note's own statements of what the video says, and the first ones carry
# its main points.
_REASON_CHARS = 300
_CLAIMS_CHARS = 1_500
# Below this a note's claims are not worth their header, and the evidence falls
# back to index lines: a title, the reason cut short and the first claim.
_CLAIMS_FLOOR = 200
_INDEX_CHARS = 160

EVIDENCE_FULL = "full"
EVIDENCE_CLAIMS_CUT = "claims cut"
EVIDENCE_INDEX = "index lines"

_INLINE_MARKER_RE = re.compile(r"\[note:[^\]]*\]", re.IGNORECASE)
_CLAIM_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


class NoteEvidence:
    """What one note brings to the baseline: built here, rendered by
    :func:`library_evidence`, sent to the model and nowhere else (rule 1)."""

    __slots__ = ("video_id", "published", "title", "judged", "claims")

    def __init__(
        self, video_id: str, published: str, title: str, judged: str, claims: Sequence[str]
    ) -> None:
        self.video_id = video_id
        self.published = published
        self.title = title
        self.judged = judged
        self.claims = list(claims)


class EvidenceSize:
    __slots__ = ("mode", "claims_chars", "notes_cut", "chars")

    def __init__(self, mode: str, claims_chars: int, notes_cut: int, chars: int) -> None:
        self.mode = mode
        self.claims_chars = claims_chars
        self.notes_cut = notes_cut
        self.chars = chars


def _note_evidence(
    note: LoadedNote, published: str, ledger: Ledger, topic: str
) -> NoteEvidence:
    """One note's evidence: its latest stage-2 reason, and its Claims.

    The reason is the latest stage-2 decision on the video that a judge actually
    made, preferring one for this topic. A reason written for another brief
    still says what the judge saw the transcript explain, so it is shown,
    labelled as such. Markers are dropped from the claims: `[note: …]` is the
    note-writer's inference, and coverage is about what the video says.
    """
    judged = "no stage-2 judgement on record"
    rows = [
        row for row in ledger.decisions_for(note.video_id)
        if row.stage == STAGE_FULL and row.reason and row.outcome in (None, OUTCOME_JUDGED)
    ]
    mine = [row for row in rows if row.topic == topic]
    chosen = (mine or rows)[-1] if rows else None
    if chosen is not None:
        whose = "this topic" if chosen.topic == topic else "another brief"
        judged = f"stage 2 said {chosen.verdict.upper()} for {whose}: " + " ".join(
            chosen.reason.split()
        )
    claims_text = note_sections(note.text).get("Claims", "")
    claims = []
    for line in claims_text.splitlines():
        text = " ".join(_INLINE_MARKER_RE.sub(" ", _CLAIM_BULLET_RE.sub("", line)).split())
        if text:
            claims.append(text)
    return NoteEvidence(note.video_id, published, " ".join(note.title.split()), judged, claims)


def library_evidence(
    entries: Sequence[NoteEvidence], *, budget_tokens: int
) -> tuple[str, EvidenceSize]:
    """The baseline's evidence, within ``budget_tokens``, and how it was cut.

    Three modes, in order, and the first that fits is used:

    - **full** — every note's reason and its claims up to `_CLAIMS_CHARS`;
    - **claims cut** — every note's claims cut to one equal share of what the
      budget leaves after the headers and reasons (degraded);
    - **index lines** — a share below `_CLAIMS_FLOOR`: each note's title, its
      reason cut to `_INDEX_CHARS` and its first claim likewise (degraded).

    Equal shares, so no note is dropped and none is favoured by its position.
    """
    budget_chars = int(budget_tokens * _CHARS_PER_TOKEN)

    def header(entry: NoteEvidence, reason_chars: int) -> str:
        return (
            f"{entry.video_id} | {entry.published or 'date unknown'} | {entry.title[:120]}\n"
            f"  judge: {entry.judged[:reason_chars]}\n"
        )

    def claims(entry: NoteEvidence, cap: int) -> tuple[str, bool]:
        joined = " / ".join(entry.claims)
        if not joined:
            return "  claims: (the note states none)\n", False
        return f"  claims: {joined[:cap]}\n", len(joined) > cap

    def render(reason_chars: int, cap: int) -> tuple[str, int]:
        parts, cut = [], 0
        for entry in entries:
            text, was_cut = claims(entry, cap)
            parts.append(header(entry, reason_chars) + text)
            cut += was_cut
        return "".join(parts), cut

    text, cut = render(_REASON_CHARS, _CLAIMS_CHARS)
    if len(text) <= budget_chars:
        return _with_mode(text, EVIDENCE_FULL), EvidenceSize(
            EVIDENCE_FULL, _CLAIMS_CHARS, cut, len(text)
        )
    fixed = sum(len(header(entry, _REASON_CHARS)) + len("  claims: \n") for entry in entries)
    share = (budget_chars - fixed) // max(1, len(entries))
    if share >= _CLAIMS_FLOOR:
        text, cut = render(_REASON_CHARS, share)
        return _with_mode(text, EVIDENCE_CLAIMS_CUT, share), EvidenceSize(
            EVIDENCE_CLAIMS_CUT, share, cut, len(text)
        )
    text, cut = render(_INDEX_CHARS, _INDEX_CHARS)
    return _with_mode(text, EVIDENCE_INDEX, _INDEX_CHARS), EvidenceSize(
        EVIDENCE_INDEX, _INDEX_CHARS, cut, len(text)
    )


def _with_mode(text: str, mode: str, cap: int = 0) -> str:
    """The evidence with a first line saying how complete it is, so the model
    reading it knows when a missing claim may only have been cut."""
    if mode == EVIDENCE_FULL:
        note = "EVIDENCE: full — every note's judge reason and its claims (the longest cut at "
        note += f"{_CLAIMS_CHARS} characters)"
    else:
        note = (
            f"EVIDENCE: DEGRADED ({mode}) — each note's claims cut to {cap} characters to fit "
            "the budget; a thing not shown may have been cut rather than missing"
        )
    return note + "\n\n" + text


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


# At most this many expansion queries aimed at one thing in a round. Three
# queries on one sub-topic in one round was measured; two still lets a gap be
# searched two ways.
_PER_AIM = 2


def coverage_so_far(run: DiscoverRun) -> list[CoverageItem]:
    """What the expansion is told is held: the latest plan's coverage, which
    already counts the library before this run, or the baseline before any plan."""
    for report in reversed(run.rounds):
        if report.plan is not None and report.plan.coverage:
            return list(report.plan.coverage)
    return list(run.baseline.items)


def cap_per_aim(queries: Sequence[str]) -> tuple[list[str], int, int]:
    """``(kept, unaimed, capped)``: at most `_PER_AIM` queries per named aim, in
    the reply's order. An unaimed query is kept and counted, never capped —
    dropping a query because its line lacked a label would narrow the search on
    a formatting slip."""
    kept: list[str] = []
    per: dict[str, int] = {}
    unaimed = capped = 0
    for query in queries:
        aim = " ".join(getattr(query, "aim", "").lower().split())
        if not aim:
            unaimed += 1
            kept.append(query)
            continue
        per[aim] = per.get(aim, 0) + 1
        if per[aim] > _PER_AIM:
            capped += 1
            continue
        kept.append(query)
    return kept, unaimed, capped


# The matcher lives beside the stage-1 tag matcher in `brain/judge.py`, so an
# aim and a tag are matched against a coverage line by the same rule.
_aim_words = aim_words


def unmatched_aims(queries: Sequence[str], coverage: Sequence[CoverageItem]) -> int:
    """How many named aims match no coverage line the expansion was given.

    An aim matches a line when every content word of the aim appears in that
    line's part, held, missing or detail text. A thread aim (`thread: …`) names
    a concept, not a line, and is not checked; an unaimed query is counted
    elsewhere. With no coverage given there is nothing to match, and nothing is
    counted. Counted only — a mismatched aim is still a query, and dropping it
    would narrow the search on a wording difference.
    """
    if not coverage:
        return 0
    lines = [
        _aim_words(" ".join([item.part, item.detail, *item.held, *item.missing]))
        for item in coverage
    ]
    count = 0
    for query in queries:
        aim = getattr(query, "aim", "")
        if not aim or aim.lower().startswith("thread:"):
            continue
        words = _aim_words(aim)
        if words and not any(words <= line for line in lines):
            count += 1
    return count


def _plan_round(
    config: Config,
    judge: Judge,
    run: DiscoverRun,
    number: int,
    threads: Sequence[str],
    from_planner: Sequence[PlannedQuery] = (),
    counts: dict[str, int] | None = None,
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
    counts = {} if counts is None else counts

    coverage = coverage_so_far(run)
    expanded = judge.expand_queries(
        run.brief,
        count=config.discover.queries_per_round,
        concepts=list(threads)[:8],
        coverage=coverage,
    )
    expanded, unaimed, capped = cap_per_aim(expanded)
    counts["unaimed"], counts["capped"] = unaimed, capped
    counts["unmatched"] = unmatched_aims(expanded, coverage)
    if capped:
        log.info(
            "discover: %d expansion quer(ies) dropped — more than %d aimed at one thing",
            capped, _PER_AIM,
        )
    origin = ORIGIN_THREAD if threads else ORIGIN_BRIEF
    fallback = ", ".join(list(threads)[:8]) if threads else run.brief.topic
    planned.extend(
        PlannedQuery(
            text=str(text),
            origin=origin,
            # The aim the reply named, which is what `queries.origin_detail`
            # needs to say whether a round went to its gaps; the threads or the
            # topic when it named none.
            origin_detail=f"aim: {aim}" if (aim := getattr(text, "aim", "")) else fallback,
        )
        for text in expanded
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
    *,
    library: Library | None = None,
) -> list[SearchResult] | None:
    """Search every query and apply gates 1 and 2. Returns survivors, in rank order.

    Also the only writer of the `queries` table. A row per search, with what it
    asked for and what it returned — because *"why did it search for that?"* is
    the first question asked when a round goes wrong, and without these rows a
    run's queries are a list with no causal story.
    """
    survivors: dict[str, SearchResult] = {}
    counted: set[str] = set()
    answered = False

    for query in queries:
        # Before the search, which writes its gate decisions as it runs; see
        # `Ledger.log_query`.
        started = ledger_now()
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
                stored_this_run=run.stored_ids + run.kept_unreadable,
                read_this_run=run.read_from_disk,
                holds=library.has_transcript if library is not None else None,
            )
        except (PermanentError, TransientError) as exc:
            # One dead query must not end a round that has nine good ones.
            log.warning("discover: search failed for %r: %s", query.text, exc)
            # Still recorded, with no result count. A query that failed is a
            # query that ran, and a table that only holds the successful ones
            # would answer "what did this round search for?" wrongly.
            _log_query(
                ledger, run, report.number, query, results=None, provider=None, units=0,
                ran_at=started,
            )
            if searched is not None:
                searched.append((query, None))
            continue

        answered = True
        # Videos, not search hits: a video three queries returned is one
        # candidate. These counters were summed per query, so the planner read
        # "9 candidates seen, 6 already in the library" about 3 videos.
        for candidate in result.candidates:
            if candidate.video_id in counted:
                continue
            counted.add(candidate.video_id)
            report.candidates += 1
            if candidate.verdict == KNOWN:
                report.known += 1
            elif candidate.verdict == STORED_THIS_RUN:
                report.known_this_run += 1
            elif candidate.verdict == REJECTED:
                previous = ledger.topic_rejection(candidate.video_id, run.topic)
                if previous is not None and previous.run_id == run.run_id:
                    report.rejected_this_run += 1
                else:
                    report.rejected_before += 1
        _log_query(
            ledger,
            run,
            report.number,
            query,
            results=len(result.candidates),
            provider=result.provider,
            units=result.quota_units,
            applied=result.filters_applied,
            ran_at=started,
        )
        if searched is not None:
            # With the filters that actually ran, as `_log_query` records them:
            # after a fall-through to a provider that cannot filter, the planner
            # was told `order=date` or a date window had been applied to a
            # search that was plain (R1 F12), and planned on it.
            searched.append((replace(query, filters=result.filters_applied), len(result.candidates)))

        for candidate in result.of(NEW):
            # Which origins surfaced this video, not just the first. "Would the
            # old expansion have found it?" is a question about *all* the
            # queries that returned it, and first-past-the-post would answer it
            # with whichever set happened to run first.
            report.surfaced_by.setdefault(candidate.video_id, set()).add(query.origin)
            # The first query that found it, which is the unit fetches are
            # spread across (see `spread_fetches`).
            report.found_by.setdefault(candidate.video_id, query.text)
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
    ran_at: str | None = None,
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
        # A waived query with no other filter still gets a row value: that the
        # window was kept off it, and why, is the part a reader needs.
        filters_json=query.filters_json(ran) if ran or query.waiver else None,
        results_count=results,
        provider=provider,
        quota_units=units,
        ran_at=ran_at,
    )


def spread_fetches(
    items: Sequence[T],
    source: Callable[[T], Hashable],
    rank: Callable[[Hashable], Any] | None = None,
) -> list[T]:
    """One from each source in turn, each source in its own order.

    Sources come in the order they first appear, or by ``rank`` when given
    (stable, so ties keep first-appearance order). The second item of the
    first source waits for the first item of every other. A pure function of
    its arguments, so the order can be tested — and simulated on a stored run —
    without a fetch.
    """
    groups: dict[Hashable, list[T]] = {}
    for item in items:
        groups.setdefault(source(item), []).append(item)
    keys = list(groups)
    if rank is not None:
        keys.sort(key=rank)
    queues = [groups[key] for key in keys]
    order: list[T] = []
    depth = 0
    while any(depth < len(queue) for queue in queues):
        order.extend(queue[depth] for queue in queues if depth < len(queue))
        depth += 1
    return order


# The order of the item levels a round reads in: what the coverage is missing,
# then a part held only in part, then candidates naming no item, then what is
# already held. Untagged before held because an untagged candidate has not been
# shown to repeat anything — a tag that matched no line may name a gap in other
# words — while a held one has. With no coverage every candidate is untagged,
# and the order is the one level it always was.
_LEVELS = (ITEM_MISSING, ITEM_PARTLY, ITEM_UNTAGGED, ITEM_HELD)
# States that fill a gap, and so are never capped.
_GAP_STATES = frozenset({ITEM_MISSING, ITEM_PARTLY})


def fetch_order(
    entries: Sequence[UnreadCandidate], fetched_so_far: Mapping[str, int]
) -> list[UnreadCandidate]:
    """The order a round reads its candidates in: by the state of the coverage
    item each would serve (`_LEVELS`), then YES before MAYBE within a level,
    and within each, one per query in turn.

    **By item first, because YES-first spent the budget on what was held.**
    YouTube ranks popular general explainers first even for a narrow gap query,
    and stage 1 says YES most readily to familiar subjects, so a round's share
    filled with YES on held parts before a gap's MAYBE was read. Measured on
    run `71cc2c8f5c57`: of 25 keepers, 16 to 22 were on parts the library
    already held, and 166 stage-1 MAYBEs were never read.

    Which query goes first in each turn is two rules, both general:

    - **Queries with fewer reads this run first.** A query whose finds have had
      nothing read yet goes before one that has had a read, whatever round it
      ran in — so what an earlier round left waiting is not pushed back behind
      each new round's queries, and a new round's gap queries are not starved
      by the backlog either.
    - **Then alternate between origins** — the brief's expansion, the map, the
      planner — so one way of searching does not take the turn's first places.
      Measured on run `d3e5d52d7138`: in first-seen order a 9-keeper round read
      only the nine expansion queries' finds, which name no part, before the
      map's queries, each aimed at a part.
    """
    origins: list[str] = []
    turn: dict[str, tuple[int, int]] = {}
    per_origin: dict[str, int] = {}
    for entry in entries:
        if entry.query in turn:
            continue
        if entry.origin not in origins:
            origins.append(entry.origin)
        number = per_origin.get(entry.origin, 0)
        turn[entry.query] = (number, origins.index(entry.origin))
        per_origin[entry.origin] = number + 1

    def rank(query: Hashable) -> tuple[int, int, int]:
        return (fetched_so_far.get(str(query), 0), *turn[str(query)])

    def by_query(entry: UnreadCandidate) -> str:
        return entry.query

    order: list[UnreadCandidate] = []
    for level in _LEVELS:
        here = [entry for entry in entries if _level(entry) == level]
        for word in (VERDICT_YES, VERDICT_MAYBE):
            order.extend(
                spread_fetches(
                    [entry for entry in here if entry.verdict.verdict == word], by_query, rank
                )
            )
    return order


def _level(entry: UnreadCandidate) -> str:
    return entry.item_state if entry.item_state in _LEVELS else ITEM_UNTAGGED


def held_cap(allowance: int) -> int:
    """How many of a round's keepers may be candidates on a held item or none.

    A third, rounded down, and never less than one. The rest of the share is
    left for the gaps: two thirds is the majority of every round's keepers,
    where run `71cc2c8f5c57` gave held parts 16 to 22 of 25. Not zero, because
    a further perspective on a held part has some value and a mis-tagged gap
    lands here; at least one, so a round of one slot is not shut to them. A cap
    reached is not a loss: the candidates wait in the queue and the slots stay
    in the budget for the next round's share, and the safeguard releases both
    when no later round could spend them.
    """
    if allowance <= 0:
        return 0
    return max(1, allowance // 3)


def coverage_has_gap(coverage: Sequence[CoverageItem]) -> bool:
    """Is anything in this coverage missing or partly held?"""
    return any(state in _GAP_STATES for state, _, _ in item_lines(coverage))


def round_allowance(remaining: int, rounds_left: int) -> int:
    """How many keepers this round may store, of the run's `remaining`.

    An even share of what is left over the rounds left, rounded up, and the
    whole of it in the last round. Before this the first round could spend the
    run's entire `max_videos`, which ended the run there: on checkpoint 4 round
    1 stored all 25, so the planner — whose job is the parts round 1 missed —
    never ran. A round that stores less than its share leaves the rest to the
    rounds after it, because each share is taken of what is still unspent.
    """
    if remaining <= 0:
        return 0
    if rounds_left <= 1:
        return remaining
    return -(-remaining // rounds_left)


def _judge_round(
    config: Config,
    ledger: Ledger,
    library: Library,
    client: LlmClient,
    judge: Judge,
    run: DiscoverRun,
    report: RoundReport,
    candidates: Sequence[SearchResult],
    providers: Sequence[TranscriptProvider],
    *,
    max_videos: int,
    max_requests: int,
    allowance: int | None = None,
    last: bool = False,
) -> list[str]:
    """Screen this round's candidates, then fetch and judge within `allowance`.

    ``allowance`` is how many keepers this round may store; ``None`` means the
    run's whole remaining `max_videos`. What it does not reach is carried on
    `run.backlog` and recorded on the report, never dropped.

    ``last`` says no later round can spend what this one leaves: the final
    round, or the remaining budget spent when the search dried up. Only then
    may candidates on a held item or none take keepers past `held_cap`.
    """
    if not candidates and not run.backlog:
        return []
    if allowance is None:
        allowance = max(0, max_videos - len(run.stored_ids))
    # The coverage this round has: the plan the last round ended with, or the
    # baseline in round one — the lines the expansion was aimed with.
    coverage = coverage_so_far(run)

    # **Partition before stage 1.** Stage 1 decides whether to *fetch* (rule
    # 13), and a transcript already on disk has nothing left to fetch — so a
    # held candidate goes straight to stage 2, or is skipped when stage 2 has
    # already judged it for this topic. It used to be screened by stage 1
    # first, and a title-only NO then stopped stage 2 from ever reading bytes
    # the library was holding: once that run, and permanently afterwards
    # through Gate 2. It also re-judged every held keeper at stage 1 in every
    # later round, and the planner read each repeat as a new finding.
    held: list[SearchResult] = []
    to_screen: list[SearchResult] = []
    waiting = {entry.video_id for entry in run.backlog}
    for candidate in candidates:
        if candidate.video_id in waiting:
            # Judged by stage 1 in an earlier round and queued to be read.
            # Screening it again would spend a call on a second opinion and
            # write a second decision row for one video.
            report.already_waiting += 1
            continue
        row = ledger.get(candidate.video_id)
        if row is not None and row.status == STATUS_FAILED and row.fail_kind == FAIL_PERMANENT:
            # The provider said this video is gone — deleted, private,
            # members-only. Ingest has always honoured that (`skip_reason`);
            # discovery screened it, fetched it again and counted another
            # failure every round it resurfaced in search results.
            report.unavailable += 1
            continue
        if not library.has_transcript(candidate.video_id):
            to_screen.append(candidate)
        elif judged_for_topic(
            ledger.decisions_for(candidate.video_id), run.topic, model=run.model
        ):
            log.info(
                "discover: %s is on disk and already judged for this topic",
                candidate.video_id,
            )
            report.already_held += 1
            # Carried on the run like a video read this run, so later rounds'
            # Gate 1 drops it instead of counting it again (independent review, F9).
            if candidate.video_id not in run.read_from_disk:
                run.read_from_disk.append(candidate.video_id)
        else:
            held.append(candidate)

    # Stage 1 within the request budget. The round was admitted with one
    # request of headroom and then ran every batch, so `max_requests` could be
    # passed by a dozen calls before the first per-fetch check — and the NOs
    # written past it are permanent for the topic (R1 F17). Candidates beyond
    # what the budget pays for keep rank order out of it: no row is written, so
    # they stay new for a later run.
    affordable = max(0, max_requests - client.usage.requests) * config.llm.judge_batch_size
    screened_short = len(to_screen) > affordable
    if screened_short:
        log.warning(
            "discover: the request budget pays for screening %d of %d candidates; "
            "the rest are left for a later run",
            affordable,
            len(to_screen),
        )
        # Spread before cutting, for the reason the fetches are spread: a cut
        # in discovery order keeps the first queries' finds and drops the
        # last queries' entirely.
        to_screen = spread_fetches(
            to_screen, lambda candidate: report.found_by.get(candidate.video_id, "")
        )[:affordable]

    verdicts = (
        judge.judge_metadata(
            run.brief,
            to_screen,
            batch_size=config.llm.judge_batch_size,
            # Stage 1 especially. This is where a video whose title never says
            # "agent" is dropped before anything reads it, and the vocabulary is
            # the only thing that can stop that — under the rule, stated in the
            # prompt, that a word from the map is never a reason to keep.
            territory=run.territory,
            # Each verdict also names the coverage item the video would add
            # to, which orders the fetches below and decides nothing else.
            coverage=coverage,
        )
        if to_screen
        else []
    )
    by_id = {candidate.video_id: candidate for candidate in candidates}
    concepts: list[str] = []
    fetched_any = False

    metadata_version = judge.prompt(METADATA_PROMPT).version if verdicts else None
    for verdict in verdicts:
        _log(
            ledger, run, verdict.video_id, STAGE_METADATA, verdict.verdict, verdict.reason,
            prompt_version=metadata_version, outcome=verdict.outcome, said=verdict.said,
            item=_item_record(verdict),
        )
        if verdict.said == VERDICT_NO:
            # A NO kept as MAYBE, counted by its ground: a guess, or what the
            # metadata did not show. Also under `verdict_maybe` below, because
            # that is the verdict it now has; these say how it got there.
            if verdict.resolved == RESOLVED_UNSHOWN:
                report.unshown_no += 1
            else:
                report.guessed_no += 1
        state = verdict.item_state if verdict.item_state in ITEM_STATES else ITEM_UNTAGGED
        report.tagged[state] = report.tagged.get(state, 0) + 1
        if verdict.unmatched:
            report.tags_unmatched += 1
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

    # Held transcripts first: they cost no fetch and no `max_videos` slot.
    # Then YES before MAYBE — not because it is cheaper (both cost a fetch and a
    # stage-2 call) but so that when the allowance truncates the round, the
    # candidates stage 1 was confident about are the ones that got the budget.
    #
    # **Within each, spread across the queries that found them.** Discovery
    # order is query order, so the budget used to go to the first few queries'
    # finds: checkpoint 4 fetched all 40 videos from queries 0-4 of 17, left
    # 74 candidates from queries 5-16 unread, and three of the brief's eight
    # parts with nothing. Each query was aimed at something, so one from each
    # in turn reaches what the round searched for. What an earlier round left
    # unread joins the same queue; `fetch_order` puts queries with nothing read
    # yet first, and alternates origins among equals.
    origin_of = {query.text: query.origin for query in report.planned}
    fresh = [
        UnreadCandidate(
            by_id[v.video_id], v, report.found_by.get(v.video_id, ""), report.number,
            origin=origin_of.get(report.found_by.get(v.video_id, ""), ""),
            item=v.item, item_state=v.item_state,
        )
        for v in verdicts
        if v.verdict in (VERDICT_YES, VERDICT_MAYBE)
    ]
    # Waiting first, so that among queries with equal reads an older one wins.
    # A waiting candidate's tag is matched again against this round's coverage:
    # a gap an earlier round filled is held now, and its candidates wait behind
    # the gaps still open.
    pool = [_retagged(entry, coverage) for entry in run.backlog] + fresh
    run.backlog = []
    queue = fetch_order(pool, run.fetched_by_query)
    # **What is held gets a capped share.** Applied only where the round has
    # coverage to tag against; a fresh topic has none, and reads as it always
    # did. Released past the cap only when no later round could spend the slots
    # *and* the coverage still names a gap: when nothing is missing any more,
    # what is left would buy only repeats, and stopping is the planner's call.
    capped = bool(coverage)
    cap = held_cap(allowance) if capped else 0
    release = last and coverage_has_gap(coverage)
    report.held_cap = max(report.held_cap, cap)
    off_gap_here = 0
    ordered: list[tuple[SearchResult, object | None, UnreadCandidate | None]] = [
        (c, None, None) for c in held
    ] + [(entry.result, entry.verdict, entry) for entry in queue]
    report.allowance += allowance
    stored_here = 0
    unreadable_here = 0
    stop_at = len(ordered)

    for index, (candidate, verdict, entry) in enumerate(ordered):
        if len(run.stored_ids) >= max_videos:
            run.stopped_because = STOP_MAX_VIDEOS
            stop_at = index
            break
        if _overflowed(run):
            stop_at = index
            break
        if entry is not None and stored_here >= allowance:
            # This round's share is spent. Not a stop: the rest wait for the
            # next round, whose planner is told they are waiting.
            stop_at = index
            break
        off_gap = entry is not None and capped and _level(entry) not in _GAP_STATES
        if off_gap and off_gap_here >= cap and not release:
            # The held share is spent. Everything after this in the queue is on
            # a held item or none, so they wait together; the slots stay in the
            # budget for the next round's share.
            stop_at = index
            report.held_capped += sum(1 for _, _, rest in ordered[index:] if rest is not None)
            break
        if entry is not None:
            run.fetched_by_query[entry.query] = run.fetched_by_query.get(entry.query, 0) + 1
            if entry.found_in_round < report.number:
                report.read_from_earlier += 1

        # Gate 1 asks whether a video is in the *library*, which since B1 means
        # it has a note. Holding the bytes is a different question, asked in the
        # partition above: a video fetched for an earlier topic whose note has
        # not been written yet is not in the library and is already on disk —
        # and **held is not judged**, so it is read from the stored file below
        # unless stage 2 has already judged it for this topic.
        is_held = verdict is None
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
        if not is_held and config.discover.trust_caption_hint and candidate.has_captions is False:
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
            stop_at = index
            break
        if _overspent(config, client):
            run.stopped_because = STOP_MAX_COST
            stop_at = index
            break

        if is_held:
            report.already_held += 1
            report.judged_from_disk += 1
            # A store interrupted between the file and the row leaves the bytes
            # here and the row `pending`, and nothing downstream ever notes a
            # `pending` video. Judged from disk without this, it was counted as
            # coverage that could never become a note.
            if reconcile_stored(library, ledger, candidate.video_id):
                report.reconciled += 1
            material = _held_material(library, ledger, candidate.video_id)
        else:
            _pace(config, fetched_any)
            fetched_any = True
            if entry is not None:
                state = _level(entry)
                report.fetched_by_state[state] = report.fetched_by_state.get(state, 0) + 1
            acquisition = acquire(ledger, candidate.video_id, providers)
            if acquisition.outcome == ACQUIRE_NO_CAPTIONS:
                # A route, not a failure — counted as one in discovery alone,
                # so a round with captionless videos reported fetch failures.
                report.no_captions += 1
                continue
            if acquisition.outcome == ACQUIRE_TRANSLATED_ONLY:
                # Routed as a captionless video is, and counted on its own: the
                # tracks existed, only not in the language the video is spoken in.
                report.translated_only += 1
                continue
            if not acquisition.acquired:
                report.failed += 1
                continue
            fetched = acquisition.result
            assert fetched is not None
            if fetched.audio_label:
                report.audio_label_disagreed += 1

            # **Stored before it is judged.** Rule 12 stores the transcript
            # whatever stage 2 says, so nothing about storing waits on the
            # verdict — and storing first means a judge that raises (a 402, a
            # rejected key, an interrupt) leaves the bytes on disk and the row
            # `has_transcript`, instead of a fetched transcript lost with the
            # process. The next run then judges it from disk, above.
            _store(library, ledger, candidate, acquisition)
            material = _Material(
                text=_transcript_text(fetched),
                chapters=[chapter.get("title", "") for chapter in fetched.chapters][:12],
                duration_s=fetched.duration_s,
                stored_chars=len(render_transcript(fetched.lines)),
            )

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
        # Handed to the judge as text. `brain/` cannot reach the filesystem, so
        # "the judge never reads a stored transcript into a report" is
        # structural here, not careful.
        #
        # Each video's context-window overflow is its own, as in the note
        # drain: three long transcripts in a row used to trip the client's
        # "every request is too large" streak and end the run as a config
        # error, with the advice to lower settings discovery does not use.
        # The small calls each round (stage 1, expansion, planning) still trip
        # it when the configuration genuinely cannot fit.
        client.forget_overflows()
        judgement = judge.judge_full(
            run.brief,
            candidate,
            transcript=material.text,
            chapters=material.chapters,
            duration_s=material.duration_s or candidate.duration_s,
            territory=run.territory,
            max_tokens=config.discover.judge_full_max_tokens,
        )
        if is_held:
            run.read_from_disk.append(candidate.video_id)
        # The other half of forgetting overflows per call: a run in which
        # *nothing* fits is not a run of long videos, it is a model or a
        # `max_tokens` that cannot hold a transcript — and judged one at a time
        # it would store every candidate as `too_large`, unread, which sticks
        # for that model. Stopped after `_OVERFLOW_STOP` in a row with no
        # stage-2 call fitting in between (independent review, F2).
        if judgement.outcome == OUTCOME_TOO_LARGE:
            run.overflow_streak += 1
        else:
            run.overflow_streak = 0
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
            outcome=judgement.outcome,
            # Only where it differs: a resolution the ledger must not present
            # as the judge's own word.
            said=judgement.said if judgement.said != judgement.verdict else None,
        )
        if not judgement.parsed:
            report.unparsed += 1
        if not judgement.judged:
            # Kept, per rule 8, and not coverage: nothing read it against the
            # brief. The planner is shown it apart, and the next run on this
            # topic judges it again.
            report.unjudged += 1
        elif judgement.verdict == VERDICT_MAYBE and judgement.evidence != EVIDENCE_DEGRADED:
            # Read, and the judge would not commit. Kept and noted, not coverage,
            # and not asked again — asking again buys a second opinion on the
            # same text, which is a sampling strategy, not a retry.
            report.uncertain += 1
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
            if verdict is None:
                # Read from disk with no stage-1 call: neither a stage-1 YES
                # overturned nor a MAYBE flipped, and counted as neither.
                pass
            elif verdict.verdict == VERDICT_YES:
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
        stored_chars = material.stored_chars
        too_long = stored_chars > config.note.max_transcript_chars
        if too_long:
            report.too_long_to_note += 1
            log.info(
                "discover: %s is %d characters, past the note ceiling of %d; "
                "it is stored but will never be noted",
                candidate.video_id,
                stored_chars,
                config.note.max_transcript_chars,
            )
        # The ceiling's own counter stays; every other ground the note worker
        # refuses on comes from the worker's own predicate, so the two cannot
        # disagree about whether a note will ever exist (R1 F3).
        unnoteable = ""
        if not too_long and judgement.evidence != EVIDENCE_DEGRADED:
            unnoteable = note_refusal(
                ledger.get(candidate.video_id),
                ledger.decisions_for(candidate.video_id),
                stored_chars,
                config.note,
            )
            if unnoteable:
                report.not_noteable += 1
                log.info("discover: %s will not be noted (%s)", candidate.video_id, unnoteable)

        # **A keeper no note will ever be written from does not spend the
        # budget that buys readable coverage** — within a bound. Measured: a run
        # stored 25 and stopped at `max_videos` with 6 of them never noteable
        # (3 degraded, 3 under the note floor), so 6 of its 25 places bought
        # nothing ask or the baseline can read. They stay kept and counted;
        # they are stored off the budget, up to one round's share per round and
        # `max_videos` per run. Past either, an unreadable keeper spends the
        # budget like any other, so a topic made mostly of uncaptioned or
        # garbled clips cannot fetch without limit: at most twice the video
        # budget is ever stored.
        unreadable = too_long or judgement.evidence == EVIDENCE_DEGRADED or bool(unnoteable)
        if not is_held and unreadable and unreadable_here < allowance and (
            len(run.kept_unreadable) < max_videos
        ):
            run.kept_unreadable.append(candidate.video_id)
            report.stored_unreadable += 1
            unreadable_here += 1
        elif not is_held:
            # Stored by this run. A video judged from disk was stored by an
            # earlier one, and counting it here would inflate `videos_stored`
            # and spend `max_videos` on a download that never happened.
            run.stored_ids.append(candidate.video_id)
            report.stored += 1
            stored_here += 1
            if off_gap:
                off_gap_here += 1
                if off_gap_here > cap:
                    # Past the cap, because nothing later could spend the slot.
                    report.released += 1
        # The other half of what the planner reads, and the half that makes
        # coverage answerable at all. A planner shown only rejections can only
        # ever learn what to avoid; it needs what was kept to say which part of
        # the brief is already satisfied.
        #
        # Always the stage-2 judgement now. There is no longer a keeper whose
        # reason describes a title rather than a transcript, which is what made
        # a coverage claim built on one a claim built on a guess.
        report.kept.append(
            _judged(candidate, STAGE_FULL, judgement, too_long=too_long, unnoteable=unnoteable)
        )

    # Nothing is left unread silently. What the loop did not reach is counted,
    # listed for the planner, and carried to the next round's queue.
    leftover = [entry for _, _, entry in ordered[stop_at:] if entry is not None]
    run.backlog = leftover
    report.unread = list(leftover)
    report.unread_yes = sum(1 for e in leftover if e.verdict.verdict == VERDICT_YES)
    report.unread_maybe = sum(1 for e in leftover if e.verdict.verdict == VERDICT_MAYBE)
    if leftover:
        log.info(
            "discover: round %d left %d judged candidate(s) unread (%d YES, %d MAYBE) "
            "from %d quer(ies); carried forward",
            report.number,
            len(leftover),
            report.unread_yes,
            report.unread_maybe,
            len({(e.found_in_round, e.query) for e in leftover}),
        )

    # The threshold is checked where it is reached, not only before a next
    # candidate: a fifth overflow that was the round's last left the run going
    # on to search and screen another round (second independent review, F).
    # Checked after the loop, so the fifth is stored, logged and counted like
    # the four before it.
    if not run.stopped_because:
        _overflowed(run)
    if screened_short and not run.stopped_because:
        run.stopped_because = STOP_MAX_REQUESTS
    return _dedupe_concepts(concepts)


def _retagged(entry: UnreadCandidate, coverage: Sequence[CoverageItem]) -> UnreadCandidate:
    """A waiting candidate with its tag matched against this round's coverage.

    Kept as it was when there is no tag, or when the tag no longer matches a
    line because the coverage was worded differently: a change of wording is
    not evidence the item's state changed.
    """
    if not entry.verdict.serves or not coverage:
        return entry
    state, item, unmatched = match_item(entry.verdict.serves, coverage)
    if unmatched:
        return entry
    return replace(entry, item=item, item_state=state)


def _overflowed(run: DiscoverRun) -> bool:
    """Has stage 2 overflowed `_OVERFLOW_STOP` times in a row? Stops the run if so."""
    if run.overflow_streak < _OVERFLOW_STOP:
        return False
    log.warning(
        "discover: %d transcripts in a row were too large for %r and none fit "
        "between them — the model's window or [llm] max_tokens cannot hold a "
        "transcript; stopping rather than storing every candidate unread",
        run.overflow_streak,
        run.model,
    )
    run.stopped_because = STOP_CONTEXT_WINDOW
    return True


# -- fetching and storing ---------------------------------------------------


class _Material:
    """What stage 2 is shown of one video, from a fetch or from disk."""

    def __init__(
        self, *, text: str, chapters: list[str], duration_s: int | None, stored_chars: int
    ) -> None:
        self.text = text
        self.chapters = chapters
        self.duration_s = duration_s
        self.stored_chars = stored_chars


_STORED_CUE_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2}\.\d{3}) --> [^\]]*\] ?", re.MULTILINE)


def _held_material(library: Library, ledger: Ledger, video_id: str) -> _Material:
    """A stored transcript, in the shape stage 2 reads a fetched one.

    Read through the library's one transcript path and recorded with
    `touch_accessed`, as every transcript read is. The stored cue prefix
    `[start --> end]` becomes the `[start]` the judge is shown for a fresh
    fetch, so both arrive in one format.
    """
    stored = library.read_transcript(video_id)
    ledger.touch_accessed(video_id)
    meta = library.read_meta(video_id) or {}
    chapters = [
        str(chapter.get("title", ""))
        for chapter in (meta.get("chapters") or [])
        if isinstance(chapter, dict)
    ][:12]
    return _Material(
        text=_STORED_CUE_RE.sub(lambda match: f"[{match.group(1)}] ", stored).rstrip("\n"),
        chapters=chapters,
        duration_s=meta.get("duration_s") or None,
        stored_chars=len(stored),
    )


def _store(
    library: Library,
    ledger: Ledger,
    candidate: SearchResult,
    acquisition: Acquisition,
) -> None:
    """The one store path, with the provenance of the provider that answered —
    not `transcript_order[0]`, which this used to record whoever fetched."""
    assert acquisition.result is not None
    store_transcript(
        library,
        ledger,
        candidate.video_id,
        acquisition.result,
        provider=acquisition.provider or "unknown",
        title=candidate.title,
        channel=candidate.channel,
    )


def _judged(
    candidate: SearchResult,
    stage: str,
    verdict,
    *,
    too_long: bool = False,
    unnoteable: str = "",
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
        too_long=too_long,
        unnoteable=unnoteable,
        judged=getattr(verdict, "judged", True),
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
    outcome: str,
    said: str | None = None,
    item: str | None = None,
) -> None:
    """Every verdict at every stage, unless this is a dry run.

    A dry run records nothing: writing `no` rows would poison Gate 2 for the
    real run that follows, so tuning the judge would shrink the library.

    ``outcome`` is required here: every judgement row discovery writes says
    whether a judge decided anything, so no later reader has to infer it.
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
        outcome=outcome,
        said=said,
        item=item,
    )


def _item_record(verdict: MetadataVerdict) -> str:
    """A stage-1 tag as the decision row stores it: `state: tag`."""
    if verdict.unmatched:
        return f"unmatched: {verdict.serves}"
    if not verdict.serves:
        return ITEM_UNTAGGED
    return f"{verdict.item_state}: {verdict.serves}"


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
