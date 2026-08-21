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
from ..llm import LlmClient, Prompt, load_prompt
from .territory import parse_filters, tidy, validate

log = logging.getLogger(__name__)

__all__ = [
    "PLAN_PROMPT",
    "Planner",
    "parse_plan",
    "validate_revisions",
    "render_round",
]

# Beside `territory/map.md`, because a planner revising a map is the same
# decision continued rather than a different one.
PLAN_PROMPT = "plan/round"

_LABEL_RE = re.compile(
    r"^\s*\**\s*(COVERAGE|QUERY|FROM|FILTERS|REVISE|WHY|EVIDENCE|RECOMMENDATION)"
    r"\s*\**\s*[:\-]\s*(.*)$",
    re.IGNORECASE,
)

# `remove vocabulary: semantic cache` / `add concept "cache warming"`
_REVISE_RE = re.compile(
    r"^\s*(add|remove)\s+(concept|vocabulary)s?\s*[:\-]?\s*(.+?)\s*$", re.IGNORECASE
)

_RECOMMEND_RE = re.compile(r"^\s*\**\s*(CONTINUE|STOP)\b\**\s*[-–—:]?\s*(.*)$", re.IGNORECASE)

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
        match = _REVISE_RE.match(pending_revise["REVISE"])
        if match is None:
            problems.append(f"unreadable revision {pending_revise['REVISE']!r}")
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
    if not any(query and query in evidence for query in ran):
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
) -> str:
    """The round as evidence, in plain text.

    Everything here is metadata and verdicts. **No transcript text, ever** — the
    judge reads transcripts and returns a sentence, and that sentence is what
    reaches this prompt. The structure the planner is given cannot carry more
    than that, which makes hard rule 1 a property of the types rather than a
    habit of this function.
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
    readable = [video for video in report.kept if not video.unreadable]
    unreadable = [video for video in report.kept if video.unreadable]

    lines.append("")
    lines.append(f"KEPT ({len(readable)}):")
    lines.extend(_video_lines(readable) or ["- none"])

    lines.append("")
    lines.append(
        f"KEPT BUT UNREADABLE ({len(unreadable)}) — the transcript was too damaged to "
        "read, so no note will be written and nothing can be answered from these. "
        "They do NOT count toward coverage: treat the part of the brief they look "
        "like they cover as still unmet."
    )
    lines.extend(_video_lines(unreadable) or ["- none"])

    lines.append("")
    lines.append(f"REJECTED ({len(report.rejected)}):")
    lines.extend(_video_lines(report.rejected) or ["- none"])

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
        f"{report.already_held} were already on disk"
    )

    if quota_remaining is not None:
        lines.append("")
        lines.append(f"SEARCH QUOTA REMAINING: {quota_remaining} units (a search costs ~101)")

    return "\n".join(lines)


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
                        report, searched=searched, quota_remaining=quota_remaining
                    ),
                    rounds_left=rounds_left,
                    count=count,
                ),
                task="plan_round",
            )
        except Exception as exc:  # noqa: BLE001 - a failed plan must not end the run
            log.warning("planner: call failed (%s) — the round falls back to expansion", exc)
            return RoundPlan(
                round_number=report.number,
                parsed=False,
                problem=f"planning call failed: {exc}",
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
            queries_run=[query.text for query, _ in searched],
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
