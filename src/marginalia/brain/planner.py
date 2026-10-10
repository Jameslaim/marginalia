"""Deciding what the next round should look for, having seen what this one found.

Until this module existed the agent planned all of its searching before seeing a
single result: a brief, a map, and then three rounds of the same idea rephrased.
One call at the end of each round now reads everything the round produced and
answers two questions — *what is the brief still missing*, and *how should I look
for it?*

Same shape as everything in `brain/`: evidence in, decision out, no I/O beyond
`llm`. Three parts, in the order `territory.py` established: pure parsing, then
a pure validator deciding which proposed changes may execute, then the one call.

**The risk this module carries is the whole of its design.**

Rejection reasons come from another model's judgement, not from the world. The
concrete case is in this project's own history: when the judge had a duration
bias, twelve of twenty-five rejections cited runtime. A planner reading that
pattern would conclude *"we keep rejecting short videos, so stop searching for
them"* — which reads as intelligent adaptation, and would have permanently
blinded the library to short-form content because of a bug. The redirect would
have looked like the system working.

**Consistency is what a systematic error looks like from the outside.** So the
prompt states, as a rule rather than a note, that rejection reasons are evidence
about what the judge decided and not facts about what exists on YouTube; that
the brief is the authority; and that the primary question is what the brief asks
for that has not been found, never why things keep being rejected.

Two structural defences back the prompt up, because a rule that only lives in a
prompt is a rule enforced by a model:

* **The planner is shown what was kept as well as what was rejected.** A planner
  given only failures only ever learns what to avoid.
* **A planner query may never carry `videoDuration`.** It is a main query by
  construction — no variants — so B4's validator refuses the band outright. The
  worst version of the failure above is a duration filter on every subsequent
  search, and that one cannot happen regardless of what the prompt achieves.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, Sequence

from ..contracts import (
    ORIGIN_PLANNER,
    LibraryCoverage,
    RECOMMEND_CONTINUE,
    RECOMMEND_STOP,
    REVISE_ADD,
    REVISE_CONCEPT,
    REVISE_REMOVE,
    REVISE_VOCABULARY,
    Brief,
    CoverageItem,
    JudgedVideo,
    PlannedQuery,
    RoundPlan,
    RoundReport,
    TerritoryMap,
    TerritoryRevision,
)
from ..llm import LlmClient, LlmConfigError, Prompt, load_prompt, safe_reason
from .territory import parse_filters, tidy, validate

log = logging.getLogger(__name__)

__all__ = [
    "PLAN_PROMPT",
    "BASELINE_PROMPT",
    "Planner",
    "parse_plan",
    "parse_baseline",
    "validate_revisions",
    "render_round",
]

# Beside `territory/map.md`, because a planner revising a map is the same
# decision continued rather than a different one.
PLAN_PROMPT = "plan/round"

# The library-coverage call, run once before round one. Beside the round plan
# because it answers the same question — what does the brief still need — from
# the other half of the evidence.
BASELINE_PROMPT = "plan/baseline"

_LABEL_RE = re.compile(
    r"^\s*\**\s*(COVERAGE|QUERY|FROM|FILTERS|REVISE|WHY|EVIDENCE|RECOMMENDATION)"
    r"\s*\**\s*[:\-]\s*(.*)$",
    re.IGNORECASE,
)

# `remove vocabulary: semantic cache` / `add concept "cache warming"`
_REVISE_RE = re.compile(
    r"^\s*[-*•]*\s*(add|remove)\s+(concept|vocabulary)s?\s*[:\-]?\s*(.+?)\s*$",
    re.IGNORECASE,
)

_RECOMMEND_RE = re.compile(r"^\s*\**\s*(CONTINUE|STOP)\b\**\s*[-–—:]?\s*(.*)$", re.IGNORECASE)

# `REVISE: none` — the planner saying, correctly, that it wants no map changes.
# **Wanting nothing is an answer, not a malformed one.** The prompt asks for a
# REVISE line and a round needing no revision is the common case, so refusing
# this filed "unreadable revision 'none'" into `plans.problem` on a run where
# nothing had gone wrong. That column is what anyone later reads to judge whether
# the planner is healthy, and a parser that records the normal case as a defect
# makes the column useless for exactly that.
#
# **A bare "-" is deliberately not on this list.** A model writing one hyphen to
# mean "no changes" is rare; a model writing a bulleted revision is not, and
# `REVISE: - add vocabulary: PgCat` must never be read as "no changes" because
# the bullet came first. An unreadable hyphen is reported as unreadable, which
# costs a log line; a swallowed bullet costs a revision nobody sees go missing.
# Matching is on the whole normalised body, never a prefix, for the same reason:
# `no changes to concepts, add vocabulary: PgCat` has to fall through.
_NO_REVISION = frozenset(
    {
        "", "n/a", "na", "no", "none", "nothing",
        "no change", "no changes", "no revision", "no revisions",
        "none needed", "none required", "no map changes",
    }
)

# Ceilings. One over-enthusiastic reply must not become thirty searches at a
# hundred quota units each, and truncation is logged rather than silent.
_MAX_QUERIES = 12
_MAX_COVERAGE = 12
_MAX_QUERY_WORDS = 12
_MAX_DETAIL = 240
_MAX_REASON = 300

# **A cap on narrowing, and only on narrowing.** Additions are unlimited: they
# widen the search and each one shows up in the next round's results. Removals
# are the excluding direction, so a single bad round must not be able to gut a
# map — three is enough for a real correction and too few for a collapse.
_MAX_REMOVALS = 3

# How many videos of each kind the planner is shown. Enough to see a pattern,
# bounded so a round of sixty candidates does not become the whole prompt.
_MAX_SHOWN = 25
_MAX_TITLE = 90


# -- parsing ------------------------------------------------------------------


def parse_plan(text: str, *, round_number: int) -> RoundPlan:
    """Read a planning reply into a :class:`RoundPlan`.

    Tolerant in the same places every parser here is, and biased the same way:
    a reply that cannot be read **does not stop the loop**. An unparseable plan
    yields no queries and a `continue`, which leaves the round to the existing
    expansion and budget ceilings — the same asymmetry as everywhere else, since
    a parser bug that stopped a run early would silently shrink the library.
    """
    coverage: list[CoverageItem] = []
    queries: list[PlannedQuery] = []
    revisions: list[TerritoryRevision] = []
    recommendation = RECOMMEND_CONTINUE
    reason = ""
    problems: list[str] = []

    pending_query: dict[str, str] | None = None
    pending_revise: dict[str, str] | None = None

    def close_query() -> None:
        nonlocal pending_query
        if pending_query is None:
            return
        filters, reasons, bad = parse_filters(pending_query.get("FILTERS", ""))
        problems.extend(
            f"{pending_query['QUERY']!r}: {field}={value!r} {why}" for field, value, why in bad
        )
        queries.append(
            PlannedQuery(
                text=pending_query["QUERY"],
                origin=ORIGIN_PLANNER,
                origin_detail=tidy(pending_query.get("FROM", ""), limit=_MAX_DETAIL),
                filters=filters,
                reasons=reasons,
            )
        )
        pending_query = None

    def close_revise() -> None:
        nonlocal pending_revise
        if pending_revise is None:
            return
        body = pending_revise["REVISE"]
        if _means_no_revision(body):
            # Not a problem, and not a revision either. The planner answered.
            pending_revise = None
            return
        match = _REVISE_RE.match(body)
        if match is None:
            problems.append(f"unreadable revision {body!r}")
            pending_revise = None
            return
        action = match.group(1).lower()
        kind = match.group(2).lower()
        term = tidy(match.group(3), limit=80).strip("\"'*.")
        if term:
            revisions.append(
                TerritoryRevision(
                    action=REVISE_ADD if action == "add" else REVISE_REMOVE,
                    kind=REVISE_CONCEPT if kind == "concept" else REVISE_VOCABULARY,
                    term=term,
                    reason=tidy(pending_revise.get("WHY", ""), limit=_MAX_REASON),
                    evidence=tidy(pending_revise.get("EVIDENCE", ""), limit=_MAX_REASON),
                )
            )
        pending_revise = None

    for raw in text.splitlines():
        match = _LABEL_RE.match(raw)
        if match is None:
            continue
        label, body = match.group(1).upper(), match.group(2).strip()

        if label == "QUERY":
            close_query()
            close_revise()
            query = tidy(body, limit=200)
            if not query or len(query.split()) > _MAX_QUERY_WORDS:
                if query:
                    problems.append(f"query too long to be typed by a person: {query!r}")
                continue
            pending_query = {"QUERY": query}
        elif label == "REVISE":
            close_query()
            close_revise()
            pending_revise = {"REVISE": body}
        elif label in ("FROM", "FILTERS"):
            if pending_query is not None:
                pending_query.setdefault(label, body)
        elif label in ("WHY", "EVIDENCE"):
            # WHY and EVIDENCE belong to a revision. A stray one after a QUERY
            # block is the model explaining itself, and is ignored rather than
            # attached to something it was not about.
            if pending_revise is not None:
                pending_revise.setdefault(label, body)
        elif label == "COVERAGE":
            item = _parse_coverage(body)
            if item is None:
                problems.append(f"unreadable coverage line {body[:60]!r}")
            elif len(coverage) < _MAX_COVERAGE:
                coverage.append(item)
        elif label == "RECOMMENDATION":
            found = _RECOMMEND_RE.match(body)
            if found is None:
                problems.append("recommendation was neither CONTINUE nor STOP")
            else:
                recommendation = (
                    RECOMMEND_STOP if found.group(1).upper() == "STOP" else RECOMMEND_CONTINUE
                )
                reason = tidy(found.group(2), limit=_MAX_REASON)

    close_query()
    close_revise()

    if len(queries) > _MAX_QUERIES:
        log.warning(
            "planner: %d queries proposed, keeping the first %d", len(queries), _MAX_QUERIES
        )
        queries = queries[:_MAX_QUERIES]

    if not coverage:
        problems.append("no coverage assessment")
    if recommendation == RECOMMEND_CONTINUE and not queries:
        # Saying "keep going" without saying where is not a plan. It is not
        # fatal — the loop falls back to its own expansion — but it is the
        # reply not doing the one thing it was asked for.
        problems.append("recommended continuing without proposing a query")

    problem = "; ".join(problems)
    if problem:
        log.warning("planner: reply had problems (%s)", problem)

    return RoundPlan(
        round_number=round_number,
        coverage=coverage,
        queries=queries,
        revisions=revisions,
        recommendation=recommendation,
        reason=reason or "no reason given",
        parsed=not problems,
        problem=problem,
    )


def parse_baseline(text: str, *, notes_read: int = 0) -> LibraryCoverage:
    """Read a library-coverage reply. Only COVERAGE lines; everything else drops.

    Deliberately narrower than :func:`parse_plan`: this call proposes no
    queries, revises no map and recommends nothing, so a reply containing any of
    those has misunderstood the task and the extra lines are ignored rather than
    acted on.

    An unreadable reply yields an **empty** assessment, not a guessed one. Empty
    means "assume the library covers nothing", which leaves the planner exactly
    where it was before this call existed — the same direction every failure in
    this project resolves: toward doing more work, never toward silently
    skipping it.
    """
    items: list[CoverageItem] = []
    problems: list[str] = []
    for line in text.splitlines():
        found = _LABEL_RE.match(line.strip())
        if found is None:
            continue
        label, body = found.group(1).upper(), found.group(2).strip()
        if label != "COVERAGE":
            continue
        item = _parse_coverage(body)
        if item is None:
            problems.append(f"unreadable coverage line {body[:60]!r}")
        elif len(items) < _MAX_COVERAGE:
            items.append(item)
    return LibraryCoverage(
        items=items,
        notes_read=notes_read,
        parsed=not problems and bool(items),
        problem="; ".join(problems[:3]) or ("no coverage lines in the reply" if not items else ""),
    )


def _means_no_revision(body: str) -> bool:
    """Is this REVISE line the planner declining to change the map?

    Stripped of the decoration a model puts around a one-word answer — quotes,
    asterisks, brackets, a trailing full stop. A bare "none" can never be a real
    revision anyway: the grammar is `add|remove concept|vocabulary: <term>`, so
    nothing that means "no changes" could ever have parsed as one.
    """
    return body.strip().strip("*_\"'()[]. ").lower() in _NO_REVISION


def _parse_coverage(body: str) -> CoverageItem | None:
    """``<part of the brief> | YES | <which videos>`` -> a :class:`CoverageItem`.

    A coverage line with no verdict is not a coverage assessment, so it is a
    problem rather than a default. Guessing `satisfied` either way would be
    inventing the one claim the line exists to make.
    """
    pieces = [piece.strip() for piece in body.split("|")]
    if len(pieces) < 2 or not pieces[0]:
        return None
    verdict = pieces[1].strip("*. ").upper()
    if verdict.startswith("YES"):
        satisfied = True
    elif verdict.startswith("NO"):
        satisfied = False
    else:
        return None
    detail = tidy(pieces[2], limit=_MAX_DETAIL) if len(pieces) > 2 else ""
    return CoverageItem(
        part=tidy(pieces[0], limit=_MAX_DETAIL).strip("*"),
        satisfied=satisfied,
        detail=detail,
    )


# -- validating the revisions -------------------------------------------------


def validate_revisions(
    revisions: Sequence[TerritoryRevision],
    *,
    territory: TerritoryMap,
    queries_run: Iterable[str] = (),
) -> tuple[list[TerritoryRevision], list[tuple[TerritoryRevision, str]]]:
    """Decide which map changes may execute. Additions are free; removals are not.

    The asymmetry is the point, and it is this project's asymmetry rather than a
    new one. An addition widens the search: worst case it costs one query whose
    results are visible. A removal narrows it, and a term removed in round two
    is a class of video that will never be searched for again — invisible, and
    nothing later in the pipeline can notice the absence.

    So a removal must:

    * name a term the map actually holds, or it is describing a different map
    * carry evidence naming at least one query that really ran, because "this
      term is not working" is a claim about searches, and a claim about
      searches that cannot point at one is an impression
    * be one of at most three this round, so a single bad round cannot gut a map

    Refusals are returned rather than swallowed, counted the same way a rejected
    filter is: a drop nobody counts is indistinguishable from a proposal that
    was never made.
    """
    ran = [" ".join(query.lower().split()) for query in queries_run]
    concepts = {term.lower() for term in territory.concepts}
    vocabulary = {term.lower() for term in territory.vocabulary}

    kept: list[TerritoryRevision] = []
    refused: list[tuple[TerritoryRevision, str]] = []
    removals = 0

    for revision in revisions:
        held = concepts if revision.kind == REVISE_CONCEPT else vocabulary
        key = revision.term.lower()

        if revision.action == REVISE_ADD:
            if key in held:
                refused.append((revision, "the map already holds this term"))
                continue
            held.add(key)
            kept.append(revision)
            continue

        if key not in held:
            refused.append((revision, f"the map holds no {revision.kind} by that name"))
            continue
        if removals >= _MAX_REMOVALS:
            refused.append(
                (revision, f"more than {_MAX_REMOVALS} removals in one round")
            )
            continue
        problem = _removal_problem(revision, ran)
        if problem is not None:
            refused.append((revision, problem))
            continue

        held.discard(key)
        removals += 1
        kept.append(revision)

    for revision, why in refused:
        log.warning("planner: refused revision %r — %s", revision.line(), why)
    return kept, refused


def _removal_problem(revision: TerritoryRevision, ran: Sequence[str]) -> str | None:
    """Why this removal may not execute, or ``None`` if it may.

    The evidence test is deliberately mechanical: does the stated evidence
    quote a query that actually ran this run? It cannot check that the reasoning
    is sound — nothing can — but it can check that the claim is about real
    searches rather than about a feeling that a term is unhelpful. That is the
    difference between "this term returned nothing" and "this term seems off
    topic", and only the first is a reason to stop searching for something.
    """
    evidence = " ".join(revision.evidence.lower().split())
    if not evidence:
        return "a removal must name the queries that carried the term"
    if not ran:
        return "no queries ran this round, so no removal can be evidenced"
    # Whole words: as a substring, a query `rag` was "named" by any evidence
    # containing "leverage", and a term was removed on a sentence that cited
    # nothing at all.
    if not any(
        query and re.search(rf"(?<!\w){re.escape(query)}(?!\w)", evidence) for query in ran
    ):
        return "the evidence names no query that actually ran"
    return None


def apply_revisions(
    territory: TerritoryMap, revisions: Sequence[TerritoryRevision]
) -> TerritoryMap:
    """A new map revision, with the old one kept beside it.

    Pure, and it returns a new map rather than mutating: the previous revision
    has to survive, or "both versions stay on the run" is a claim the ledger
    cannot back up. A revision you cannot see the before of is a change nobody
    can judge, which is the same argument that put the map on the run at all.
    """
    if not revisions:
        return territory

    concepts = list(territory.concepts)
    vocabulary = list(territory.vocabulary)

    for revision in revisions:
        target = concepts if revision.kind == REVISE_CONCEPT else vocabulary
        if revision.action == REVISE_ADD:
            target.append(revision.term)
            continue
        lowered = revision.term.lower()
        target[:] = [term for term in target if term.lower() != lowered]

    return TerritoryMap(
        topic=territory.topic,
        version=territory.version,
        concepts=concepts,
        vocabulary=vocabulary,
        recency=territory.recency,
        queries=territory.queries,
        rejected=territory.rejected,
        parsed=territory.parsed,
        problem=territory.problem,
        input_tokens=territory.input_tokens,
        output_tokens=territory.output_tokens,
        latency_s=territory.latency_s,
        revision=territory.revision + 1,
        revisions=[*territory.revisions, *revisions],
        history=[*territory.history, territory.snapshot()],
    )


# -- what the planner is shown ------------------------------------------------


def render_round(
    report: RoundReport,
    *,
    searched: Sequence[tuple[PlannedQuery, int | None]] = (),
    quota_remaining: int | None = None,
    kept_so_far: Sequence[tuple[int, JudgedVideo]] = (),
    baseline: Sequence[CoverageItem] = (),
) -> str:
    """The round as evidence, in plain text.

    Everything here is metadata and verdicts. **No transcript text, ever** — the
    judge reads transcripts and returns a sentence, and that sentence is what
    reaches this prompt. The structure the planner is given cannot carry more
    than that, which makes hard rule 1 a property of the types rather than a
    habit of this function.

    ``kept_so_far`` is **the run's kept videos, not this round's**, each marked
    with the round that kept it. Coverage is a claim about what the library
    holds, and it cannot be made from one round's worth of it. Measured, in run
    `ea18299bcea5`: round 1 marked "comparison across two or more frameworks"
    covered, citing a video titled *"Agent Memory Explained: LangChain vs CrewAI
    Approaches Compared"*; round 2, shown only its own six keepers, called the
    same item a gap; round 3 then spent five of six queries chasing it. Third
    instance of one bug family — Gate 1 not knowing what the run had already
    stored was the second.

    **Rejections stay per-round, deliberately.** They are evidence about the
    judge rather than about the world, and accumulating them across rounds would
    make any systematic judge error look more and more like a fact about
    YouTube — which is precisely the failure the whole module is built around.
    Coverage needs everything kept; nothing needs everything rejected.
    """
    lines = [f"ROUND {report.number}"]

    lines.append("")
    lines.append("QUERIES RUN:")
    if searched:
        for query, results in searched:
            found = "search failed" if results is None else f"{results} results"
            filters = query.filters.as_dict()
            suffix = f", filters {filters}" if filters else ""
            lines.append(f"- [{query.origin}] {query.text} — {found}{suffix}")
    else:
        lines.append("- none")

    # Split, not merely labelled. A `DEGRADED` keeper is a video stage 2 could
    # not read: it was stored, it gets no note, and `/ask` will never answer
    # from it. Listed among the ordinary keepers it reads as coverage — the
    # planner would conclude a part of the brief is satisfied by evidence that
    # does not exist, and stop searching for the video that would have
    # satisfied it. Two headings say what one adjective inside a list does not.
    # Falls back to this round alone when the caller gave nothing, so a direct
    # call to this function still renders something coherent.
    #
    # And a third heading for a keeper stage 2 never gave a verdict — a failed
    # call, a transcript over the model's window, a cut-off reply. It may well
    # get a note; what it lacks is anyone having read it against the brief, so
    # it is not evidence of coverage either. Listed under KEPT it was: rule 8's
    # stage-2 paragraph names a wrong YES as the error that hides here.
    cumulative = list(kept_so_far) or [(report.number, video) for video in report.kept]
    readable = [(number, v) for number, v in cumulative if v.covers]
    unreadable = [(number, v) for number, v in cumulative if not v.answerable]
    # Answerable but not coverage: no verdict at all, or a MAYBE the judge
    # would not turn into a YES. Both are kept and both get notes; neither is
    # evidence that a part of the brief is served.
    unjudged = [(number, v) for number, v in cumulative if v.answerable and not v.covers]

    lines.append("")
    lines.append(
        f"KEPT SO FAR IN THIS RUN ({len(readable)}) — every round, not just this "
        "one. This is what the library now holds for this topic, and it is what "
        "coverage must be judged against. The round number says which round "
        "found each one, so you can still see what this round added."
    )
    lines.extend(_marked_video_lines(readable) or ["- none"])

    lines.append("")
    lines.append(
        f"KEPT BUT NOT ANSWERABLE ({len(unreadable)}) — no note will be written for "
        "these, either because the transcript was too damaged to read or because it "
        "is past the note worker's length ceiling (or under its floor), so nothing "
        "can ever be answered "
        "from them. They do NOT count toward coverage: treat the part of the brief "
        "they look like they cover as still unmet."
    )
    lines.extend(_marked_video_lines(unreadable) or ["- none"])

    lines.append("")
    lines.append(
        f"KEPT BUT NOT JUDGED ({len(unjudged)}) — stored, but stage 2 produced no "
        "verdict on these (the call failed, the transcript was too large for the "
        "model, or the reply was cut off) or answered only MAYBE, so nothing has "
        "confirmed them against the brief. They do NOT count toward coverage; the "
        "ones with no verdict will be judged again by the next run on this topic."
    )
    lines.extend(_marked_video_lines(unjudged) or ["- none"])

    lines.append("")
    # Stage-2 rejections first, here and only here. The loop appends every
    # stage-1 NO before any transcript is read, so with 25 or more of those the
    # cap below showed none of the rejections made after reading a transcript —
    # the most informative ones. A stable sort keeps each stage's own order, and
    # `report.rejected` itself is left as it was. The literal is `STAGE_FULL`,
    # which `brain/` may not import from `core/`.
    rejected = sorted(report.rejected, key=lambda video: video.stage != "full")
    lines.append(f"REJECTED ({len(report.rejected)}):")
    lines.extend(_video_lines(rejected) or ["- none"])

    lines.append("")
    already = [item for item in baseline if item.satisfied]
    lines.append(
        f"ALREADY IN THE LIBRARY BEFORE THIS RUN ({len(already)}) — assessed from the "
        "notes the library already held, which this run never re-fetched and which "
        "therefore appear nowhere above. **Treat these parts of the brief as "
        "covered.** Naming one as a gap sends the next round searching for "
        "something that can already be answered."
    )
    lines.extend(
        [f"- {item.part} — {item.detail}" for item in already]
        or ["- none (or the library was not assessed)"]
    )

    lines.append("")
    lines.append("MENTIONED BUT UNEXPLAINED (from videos that were kept):")
    lines.append("- " + ", ".join(report.concepts) if report.concepts else "- none")

    lines.append("")
    lines.append("COUNTERS:")
    lines.append(
        f"- {report.candidates} candidates seen, {report.known} already in the library, "
        f"{report.rejected_before} carried a NO for this topic from an earlier run"
    )
    lines.append(
        f"- stage 1: {report.verdict_yes} yes, {report.verdict_maybe} maybe, "
        f"{report.verdict_no} no"
    )
    lines.append(
        f"- stage 2: {report.stored} kept and stored, {report.flipped_to_no} declined, "
        f"{report.degraded} unreadable transcripts"
    )
    lines.append(
        f"- {report.failed} fetches failed, {report.no_captions} had no captions, "
        f"{report.already_held} were already on disk "
        f"({report.judged_from_disk} of them judged for this topic from disk, and "
        f"listed above like any other)"
    )

    if quota_remaining is not None:
        lines.append("")
        lines.append(f"SEARCH QUOTA REMAINING: {quota_remaining} units (a search costs ~101)")

    return "\n".join(lines)


def _marked_video_lines(videos: Sequence[tuple[int, JudgedVideo]]) -> list[str]:
    """Kept videos, each carrying the round that kept it.

    The cap is applied to the *oldest* first when it bites, so the most recent
    round — the one the planner is reasoning about — is never the part that
    disappears. Said out loud either way, for the reason every cap here is.
    """
    if len(videos) <= _MAX_SHOWN:
        shown, dropped = videos, 0
    else:
        shown, dropped = videos[-_MAX_SHOWN:], len(videos) - _MAX_SHOWN
    lines = [
        f"- [round {number}] {video.title[:_MAX_TITLE]} [{video.stage}] — {video.reason}"
        for number, video in shown
    ]
    if dropped:
        lines.insert(0, f"- (+{dropped} kept in earlier rounds, not shown)")
    return lines


def _video_lines(videos: Sequence[JudgedVideo]) -> list[str]:
    shown = videos[:_MAX_SHOWN]
    lines = [
        f"- {video.title[:_MAX_TITLE]} [{video.stage}] — {video.reason}" for video in shown
    ]
    if len(videos) > len(shown):
        # Said out loud. A truncated list read as the whole round is exactly the
        # kind of silent cap that makes a pattern look stronger than it is.
        lines.append(f"- (+{len(videos) - len(shown)} more, not shown)")
    return lines


def render_territory(territory: TerritoryMap) -> str:
    """The map, labelled by half — the planner may revise either one."""
    if not territory:
        return "TERRITORY MAP: none was built for this topic."
    return "\n".join(
        [
            f"TERRITORY MAP (revision {territory.revision}):",
            "CONCEPTS: " + (", ".join(territory.concepts) or "none"),
            "VOCABULARY: " + (", ".join(territory.vocabulary) or "none"),
            f"RECENCY: {territory.recency.label} — {territory.recency.reason or 'no reason given'}",
        ]
    )


# -- the planner ---------------------------------------------------------------


class Planner:
    """Owns the planning prompt and the model client. Knows nothing about the loop."""

    def __init__(self, client: LlmClient, *, prompts_directory=None) -> None:
        self.client = client
        self._directory = prompts_directory
        self._cache: dict[str, Prompt] = {}

    def prompt(self, name: str) -> Prompt:
        if name not in self._cache:
            self._cache[name] = load_prompt(name, directory=self._directory)
        return self._cache[name]

    def assess_library(
        self,
        brief: Brief,
        notes: str,
        *,
        notes_read: int = 0,
        max_tokens: int | None = None,
    ) -> LibraryCoverage:
        """What the library already satisfies, before any searching.

        ``notes`` arrives as rendered text. `brain/` may not touch the disk, so
        the caller loads the notes and hands them over — the same arrangement
        that keeps the judge from ever reading a stored transcript itself.

        Never raises, and a failure returns an **empty** assessment rather than
        a guessed one. Empty means "assume nothing is covered", which is exactly
        the behaviour that existed before this call, so a new step cannot make a
        working loop worse.
        """
        prompt = self.prompt(BASELINE_PROMPT)
        try:
            reply = self.client.complete(
                "You audit what a library of notes already covers.",
                prompt.render(brief=brief.text, notes=notes, count=notes_read),
                task="assess_library",
                max_tokens=max_tokens,
            )
        except LlmConfigError:
            raise  # a refusal, not a failed assessment — rule 24
        except Exception as exc:  # noqa: BLE001 - must not end the run
            log.warning(
                "planner: library assessment failed (%s) — assuming nothing is covered",
                safe_reason(exc),
            )
            return LibraryCoverage(
                parsed=False, problem=f"assessment call failed: {safe_reason(exc)}"
            )

        if reply.finish_reason == "length":
            # Same guard as the map and the plan. A half-read assessment is
            # worse than none: the items that happened to fit read as the whole
            # of what the library holds, and the planner would stop looking for
            # everything the truncated tail would have marked NO.
            log.warning(
                "planner: library assessment was truncated (%d output tokens) — ignoring it",
                reply.usage.output_tokens,
            )
            return LibraryCoverage(
                parsed=False,
                problem="reply hit the token ceiling before finishing",
                notes_read=notes_read,
                input_tokens=reply.usage.input_tokens,
                output_tokens=reply.usage.output_tokens,
                latency_s=reply.latency_s,
            )

        baseline = parse_baseline(reply.text, notes_read=notes_read)
        if not baseline.parsed:
            log.warning("planner: library assessment unusable — %s", baseline.problem)
        return LibraryCoverage(
            items=baseline.items,
            notes_read=notes_read,
            parsed=baseline.parsed,
            problem=baseline.problem,
            input_tokens=reply.usage.input_tokens,
            output_tokens=reply.usage.output_tokens,
            latency_s=reply.latency_s,
        )

    def plan(
        self,
        brief: Brief,
        territory: TerritoryMap,
        report: RoundReport,
        *,
        searched: Sequence[tuple[PlannedQuery, int | None]] = (),
        already_run: Iterable[tuple] = (),
        quota_remaining: int | None = None,
        rounds_left: int = 1,
        count: int = 6,
        kept_so_far: Sequence[tuple[int, JudgedVideo]] = (),
        baseline: Sequence[CoverageItem] = (),
        max_tokens: int | None = None,
    ) -> RoundPlan:
        """One call. A failure leaves the loop exactly as it was before B5.

        Never raises. The loop still has query expansion, threads and every
        budget ceiling, so a planner that failed makes the run less directed
        rather than dead — and a new, unproven step must not be able to kill a
        loop that worked before it existed.
        """
        prompt = self.prompt(PLAN_PROMPT)
        try:
            reply = self.client.complete(
                "You decide what a research round should look for next.",
                prompt.render(
                    brief=brief.text,
                    territory=render_territory(territory),
                    round=render_round(
                        report,
                        searched=searched,
                        quota_remaining=quota_remaining,
                        kept_so_far=kept_so_far,
                        baseline=baseline,
                    ),
                    rounds_left=rounds_left,
                    count=count,
                ),
                task="plan_round",
                # The cumulative kept set makes both halves of this call bigger:
                # more evidence in, and a coverage assessment that now cites
                # across rounds out. At 8,192 the round-2 call was already at
                # 68% of its ceiling, and a truncated plan is discarded whole.
                max_tokens=max_tokens,
            )
        except LlmConfigError:
            raise  # a refusal, not a failed plan — rule 24
        except Exception as exc:  # noqa: BLE001 - a failed plan must not end the run
            log.warning(
                "planner: call failed (%s) — the round falls back to expansion", safe_reason(exc)
            )
            return RoundPlan(
                round_number=report.number,
                parsed=False,
                problem=f"planning call failed: {safe_reason(exc)}",
            )

        if reply.finish_reason == "length":
            # A fragment, not a plan. Parsing it would take whichever queries
            # happened to fit and whatever coverage lines came first, and a
            # half-read coverage assessment is worse than none: it reads as a
            # complete account of what the brief is missing.
            log.warning(
                "planner: reply was truncated (%d output tokens) — ignoring it",
                reply.usage.output_tokens,
            )
            return RoundPlan(
                round_number=report.number,
                parsed=False,
                problem="reply hit the token ceiling before finishing",
                input_tokens=reply.usage.input_tokens,
                output_tokens=reply.usage.output_tokens,
                latency_s=reply.latency_s,
            )

        parsed = parse_plan(reply.text, round_number=report.number)

        # The same validator B4's map queries go through, and deliberately the
        # same one: a filter is exactly as dangerous proposed by the planner as
        # proposed by the mapper. Planner queries carry no `variant_of`, so a
        # `videoDuration` band is refused by construction — which is the
        # structural half of this module's defence against a poisoned log.
        queries, rejected = validate(
            parsed.queries,
            recency=territory.recency,
            topic=brief.topic,
            already_run=already_run,
        )
        revisions, refused = validate_revisions(
            parsed.revisions,
            territory=territory,
            # Searches that came back. A query whose search failed returned
            # nothing to judge, so it cannot be the evidence rule 19 asks for —
            # "a query that actually ran, and what came back".
            queries_run=[query.text for query, results in searched if results is not None],
        )

        return RoundPlan(
            round_number=parsed.round_number,
            coverage=parsed.coverage,
            queries=queries,
            revisions=revisions,
            recommendation=parsed.recommendation,
            reason=parsed.reason,
            rejected_filters=rejected,
            refused_revisions=refused,
            parsed=parsed.parsed,
            problem=parsed.problem,
            input_tokens=reply.usage.input_tokens,
            output_tokens=reply.usage.output_tokens,
            latency_s=reply.latency_s,
        )
