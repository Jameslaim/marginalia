"""One discovery run, rendered as a folder you can read.

::

    exports/<run_id>/
    ├── report.md       what happened, round by round, and each round's plan
    ├── brief.md
    ├── territory.md    including revisions, with diffs and reasons
    ├── queries.tsv     query, origin, filters, results, quota
    └── decisions.tsv   video, stage, verdict, evidence, reason

Generated on demand and never automatically. **The ledger stays the single
source of truth** — everything here is derived, nothing is stored back, and a
second export of the same run overwrites the first. That is the difference
between a view and a copy, and it is the reason this can be deleted at any time
without losing anything.

What it replaces is writing SQL by hand. Diagnosing a run meant five joins
against a schema you had to remember, which in practice meant runs went
undiagnosed. This is also deliberately readable by a model: hand the folder over
and ask what went wrong.

**Rounds are reconstructed, not stored.** `queries` carries a round number;
`decisions` does not. The loop is strictly sequential — round N's queries all run
before round N+1's first query — so a decision is attributed to the last round
whose queries had already started when it was made. That inference is stated in
``report.md`` itself rather than presented as a recorded fact, because a report
that quietly invents precision is worse than one that admits its method.

No transcript text and no note text, here as everywhere. A decision's ``reason``
is the judge's own sentence about a video, which is the same thing the planner
is shown and the largest piece of content that travels anywhere in this system.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Sequence

from ..config import Config
from ..contracts import EVIDENCE_DEGRADED, ExportedRun
from ..core.ledger import DecisionRow, Ledger, PlanRow, QueryRow, RunRow
from ..core.library import Library

log = logging.getLogger(__name__)

# `render_map` rather than `render_territory`: `brain/planner.py` already has a
# `render_territory`, and it renders the map *for a model* while this one renders
# it for a person. Two functions with one name doing different things is how this
# project ended up with two `Citation` types nothing could tell apart.
__all__ = ["export_run", "render_report", "render_map", "MISSING"]

# What an empty cell says in a TSV. Not the empty string: a run of tabs tells
# you a column exists and says nothing about whether anyone ever wrote to it,
# and these files are read in a terminal as often as in a spreadsheet.
MISSING = "-"


def export_run(config: Config, ledger: Ledger, library: Library, run_id: str) -> ExportedRun:
    """Write the folder for one run. Raises ``LookupError`` if it does not exist."""
    run = ledger.get_run(run_id)
    if run is None:
        raise LookupError(run_id)

    queries = ledger.queries_for_run(run_id)
    decisions = ledger.decisions_for_run(run_id)
    plans = ledger.plans_for_run(run_id)
    territory = _territory(run)

    written = [
        library.write_export(
            run_id, "report.md", render_report(config, run, queries, decisions, plans)
        ),
        library.write_export(run_id, "brief.md", _render_brief(run)),
        library.write_export(run_id, "territory.md", render_map(run, territory)),
        library.write_export(run_id, "queries.tsv", _render_queries(queries)),
        library.write_export(run_id, "decisions.tsv", _render_decisions(decisions)),
    ]
    log.info("export: wrote %d files for run %s", len(written), run_id)
    return ExportedRun(
        run_id=run_id,
        directory=library.export_dir(run_id),
        files=written,
        rounds=run.rounds,
        queries=len(queries),
        decisions=len(decisions),
    )


# -- report -------------------------------------------------------------------


def render_report(
    config: Config,
    run: RunRow,
    queries: Sequence[QueryRow],
    decisions: Sequence[DecisionRow],
    plans: Sequence[PlanRow] = (),
) -> str:
    """The run in prose and tables. Pure: everything it needs is an argument.

    ``plans`` default to none, and none is what a run made before the `plans`
    table had a writer genuinely has. Such a run's planning survives only as the
    gap label on each query it produced, and the report says so where the plan
    would have been rather than leaving a silent hole.
    """
    rounds = sorted({query.round for query in queries})
    by_round = {plan.round: plan for plan in plans}
    lines = [
        f"# {run.topic}",
        "",
        f"Run `{run.run_id}`, {run.rounds} round(s).",
        "",
        "| | |",
        "|---|---|",
        f"| started | {run.started_at} |",
        f"| finished | {run.finished_at or '**never — the run did not complete**'} |",
        f"| stopped because | {run.stopped_because or MISSING} |",
        f"| model | {run.model or MISSING} |",
        f"| brief prompt | v{run.brief_version or MISSING} |",
        f"| territory map | {run.territory_version or 'none built'} |",
        f"| round-one queries from | {_round_one_source(run)} |",
        f"| candidates seen | {run.candidates_seen} |",
        f"| videos stored | {run.videos_stored} |",
        f"| model requests | {run.requests_made} |",
        f"| tokens | {run.input_tokens} in, {run.output_tokens} out |",
        f"| estimated cost | ${config.llm.cost_usd(run.input_tokens, run.output_tokens):.4f} |",
        "",
    ]

    if not run.finished_at:
        # Said once, loudly, at the top. Every count below it is a partial
        # count, and a reader who misses that will diagnose the wrong thing.
        lines.extend(
            [
                "> This run has no `finished_at`. It crashed or was interrupted, so",
                "> every number above is whatever had been reached at that moment.",
                "",
            ]
        )

    lines.extend(_baseline_section(run))
    lines.extend(["## Rounds", ""])
    if not rounds:
        lines.extend(
            [
                "No queries were recorded against this run. Either it stopped before",
                "searching, or it predates the `queries` table gaining a writer in B4.",
                "",
            ]
        )
    else:
        boundaries = _round_boundaries(queries, rounds)
        lines.append(
            "Decisions are attributed to a round by timestamp: the loop is "
            "sequential, so a decision belongs to the last round whose queries had "
            "already started. The `queries` table records its round directly; the "
            "`decisions` table does not."
        )
        lines.append("")
        for number in rounds:
            lines.extend(
                _round_section(
                    number,
                    [query for query in queries if query.round == number],
                    _decisions_in(decisions, boundaries, number),
                    by_round.get(number),
                    planned_at_all=bool(plans),
                    is_last=(number == rounds[-1]),
                )
            )

    lines.extend(["## Decisions in total", ""])
    lines.extend(_verdict_table(decisions))
    degraded = [
        row for row in decisions if (row.evidence or "").upper() == EVIDENCE_DEGRADED
    ]
    if degraded:
        lines.extend(
            [
                "",
                f"**{len(degraded)} verdict(s) rested on degraded evidence** — the "
                "transcript could not be read. A degraded keeper gets no note, so "
                "nothing can be answered from it and it satisfies no part of the brief:",
                "",
            ]
        )
        lines.extend(
            f"- `{row.video_id}` [{row.stage}/{row.verdict}] {_flat(row.reason)}"
            + (f" — relevant: {row.relevant}" if row.relevant else "")
            for row in degraded
        )
    lines.append("")
    return "\n".join(lines)


def _coverage_mark(item: dict) -> str:
    """A stored coverage item's verdict. Rows stored before the itemised format
    carry no ``partly`` and read as they did: YES or NO."""
    if item.get("satisfied"):
        return "YES"
    return "**PARTLY**" if item.get("partly") else "**NO**"


def _baseline_section(run: RunRow) -> list[str]:
    """What the library already covered when the run started.

    Rendered above the rounds because that is when it was computed and what it
    describes: the shelf the run began from. A run with no baseline says so
    rather than showing nothing — every run before this existed has none, and an
    absent section would read as "the library covered nothing".
    """
    if not run.library_baseline:
        return [
            "## Starting coverage",
            "",
            "*No library assessment was stored. Runs before the planner had a "
            "coverage baseline have none, and so does any run on an empty library.*",
            "",
        ]
    try:
        body = json.loads(run.library_baseline)
    except ValueError:
        log.warning("export: run %s has an unreadable library baseline", run.run_id)
        return ["## Starting coverage", "", "*The stored assessment could not be read.*", ""]
    if not isinstance(body, dict):
        return ["## Starting coverage", "", "*The stored assessment could not be read.*", ""]

    items = body.get("items") or []
    covered = [item for item in items if item.get("satisfied")]
    partly = sum(1 for item in items if item.get("partly"))
    lines = [
        "## Starting coverage",
        "",
        f"Assessed from {body.get('notes_read', 0)} note(s) already in the library, "
        f"before any search ran. **{len(covered)} of {len(items)}** parts of the brief "
        "were already satisfied — the planner is told to treat these as covered, so it "
        "does not send a round looking for what the library can already answer."
        + (f" {partly} more were held partly." if partly else ""),
        "",
    ]
    if body.get("evidence_mode") and body.get("evidence_mode") != "full":
        lines.extend([
            f"> **Degraded evidence ({_flat(body['evidence_mode'])}):** "
            f"{body.get('notes_cut', 0)} note(s)' claims were cut to "
            f"{body.get('claims_chars', 0)} characters to fit the budget.",
            "",
        ])
    if body.get("problem"):
        lines.extend([f"> {_flat(body['problem'])}", ""])
    if items:
        lines.extend(["| | part of the brief | what already covers it |", "|---|---|---|"])
        for item in items:
            mark = _coverage_mark(item)
            lines.append(
                f"| {mark} | {_flat(item.get('part')) or MISSING} "
                f"| {_flat(item.get('detail')) or MISSING} |"
            )
    else:
        lines.append("The assessment returned nothing.")
    lines.append("")
    return lines


def _round_section(
    number: int,
    queries: Sequence[QueryRow],
    decisions: Sequence[DecisionRow],
    plan: PlanRow | None = None,
    *,
    planned_at_all: bool = False,
    is_last: bool = False,
) -> list[str]:
    found = sum(query.results_count or 0 for query in queries)
    quota = sum(query.quota_units or 0 for query in queries)
    lines = [
        f"### Round {number}",
        "",
        f"{len(queries)} quer(ies), {found} result(s), {quota} quota unit(s), "
        f"{len(decisions)} decision(s).",
        "",
    ]
    for query in queries:
        detail = f" ({query.origin_detail})" if query.origin_detail else ""
        results = MISSING if query.results_count is None else str(query.results_count)
        filters = _filters(query.filters_json)
        suffix = f" — filters: {filters}" if filters != MISSING else ""
        lines.append(f"- `{query.origin}`{detail} **{query.query}** → {results} results{suffix}")
    lines.append("")
    if decisions:
        lines.extend(_verdict_table(decisions))
        lines.append("")
    lines.extend(
        _plan_section(number, plan, planned_at_all=planned_at_all, is_last=is_last)
    )
    return lines


def _plan_section(
    number: int, plan: PlanRow | None, *, planned_at_all: bool, is_last: bool = False
) -> list[str]:
    """One round's plan, in full, under the round that produced it.

    The gap labels on the next round's queries are the *conclusion*. This is the
    reasoning — which parts of the brief the planner judged covered and by what,
    which it judged missing, what it wanted to do about it, and whether it
    thought the run should continue. Without it, "why did the planner decide
    that?" has no answer, which is the question storing a run is for.
    """
    if plan is None:
        # Three different absences, and one message for all of them would
        # misattribute two of them. The first real broad-topic run said a run
        # "predates the `plans` table" about a run three hours younger than the
        # table — it had simply stopped on `max_videos` inside round one, and
        # the last round is never planned.
        if is_last:
            # The common and entirely expected case, whatever else is true.
            return [
                "*No plan after this round — the last round is never planned, "
                "and a run that a budget ceiling ended has no round after this "
                "one to plan for.*",
                "",
            ]
        if not planned_at_all:
            # A round in the middle with no plan anywhere in the run. Silence
            # would read as "the planner said nothing", which is a different
            # claim from "nothing recorded what it said".
            return [
                "*No plan was stored for this round. Runs before the `plans` "
                "table gained a writer have none — their planning survives only "
                "as the gap label on each query it produced.*",
                "",
            ]
        return [
            "*No plan after this round, though the run planned elsewhere — the "
            "planning call was skipped, which happens when a budget ceiling was "
            "already reached.*",
            "",
        ]

    try:
        body = json.loads(plan.plan_json)
    except ValueError:
        log.warning("export: run %s has an unreadable plan for round %d", plan.run_id, number)
        return ["*The stored plan for this round could not be read.*", ""]
    if not isinstance(body, dict):
        return ["*The stored plan for this round could not be read.*", ""]

    lines = [f"#### The plan after round {number}", ""]
    if plan.prompt_version:
        lines.extend([f"Planning prompt v{plan.prompt_version}, {plan.planned_at}.", ""])

    recommendation = body.get("recommendation") or MISSING
    reason = _flat(body.get("reason")) or MISSING
    lines.extend([f"**{recommendation}** — {reason}", ""])

    if body.get("problem"):
        lines.extend([f"> Reply problem: {_flat(body['problem'])}", ""])
    if body.get("parsed") is False:
        lines.extend(
            [
                "> This reply could not be parsed. Everything below is whatever "
                "survived, and a plan the code could not read is a plan the loop "
                "largely ignored.",
                "",
            ]
        )

    coverage = body.get("coverage") or []
    lines.extend(["**Coverage against the brief**", ""])
    if coverage:
        lines.extend(["| | part of the brief | what it found |", "|---|---|---|"])
        for item in coverage:
            mark = _coverage_mark(item)
            lines.append(
                f"| {mark} | {_flat(item.get('part')) or MISSING} "
                f"| {_flat(item.get('detail')) or MISSING} |"
            )
    else:
        lines.append("The planner assessed no part of the brief.")
    lines.append("")

    queries = body.get("queries") or []
    lines.extend(["**Queries it proposed**", ""])
    if queries:
        for query in queries:
            detail = _flat(query.get("origin_detail"))
            suffix = f" — {detail}" if detail else ""
            lines.append(f"- **{_flat(query.get('text')) or MISSING}**{suffix}")
    else:
        lines.append("- none")
    lines.append("")

    lines.extend(_plan_refusals(body))
    return lines


def _plan_refusals(body: dict[str, Any]) -> list[str]:
    """Map revisions, and everything the code refused.

    Printed even when empty is not the rule here — an absent section is fine —
    but a refusal that happened and is not printed would be a narrowing nobody
    can see, which is the failure the validator exists to prevent.
    """
    lines: list[str] = []
    revisions = body.get("revisions") or []
    if revisions:
        lines.extend(["**Map revisions it asked for**", ""])
        for item in revisions:
            sign = "+" if item.get("action") == "add" else "−"
            lines.append(
                f"- {sign} **{item.get('kind')}**: {item.get('term')} — "
                f"{_flat(item.get('reason')) or MISSING}"
            )
            if item.get("evidence"):
                lines.append(f"  - evidence: {_flat(item['evidence'])}")
        lines.append("")

    refused = body.get("refused_revisions") or []
    if refused:
        lines.extend(["**Revisions the code refused**", ""])
        lines.extend(
            f"- {item.get('action')} `{item.get('term')}` — {_flat(item.get('why'))}"
            for item in refused
        )
        lines.append("")

    rejected = body.get("rejected_filters") or []
    if rejected:
        lines.extend(["**Filters the validator refused** — each one widened the search", ""])
        lines.extend(
            f"- `{item.get('field')}={item.get('value')}` on “{item.get('query')}” — "
            f"{_flat(item.get('reason'))}"
            for item in rejected
        )
        lines.append("")

    cost = (
        f"{body.get('input_tokens', 0)} in, {body.get('output_tokens', 0)} out, "
        f"{body.get('seconds', 0)}s"
    )
    lines.extend([f"*The planning call cost {cost}.*", ""])
    return lines


def _verdict_table(decisions: Sequence[DecisionRow]) -> list[str]:
    """Counts per stage and verdict. Every stage that appears, in ledger order."""
    if not decisions:
        return ["No decisions."]
    stages: dict[str, dict[str, int]] = {}
    for row in decisions:
        stages.setdefault(row.stage, {})
        stages[row.stage][row.verdict] = stages[row.stage].get(row.verdict, 0) + 1
    verdicts = sorted({verdict for counts in stages.values() for verdict in counts})
    lines = [
        "| stage | " + " | ".join(verdicts) + " | total |",
        "|---|" + "---|" * (len(verdicts) + 1),
    ]
    for stage, counts in stages.items():
        cells = [str(counts.get(verdict, 0)) for verdict in verdicts]
        lines.append(f"| {stage} | " + " | ".join(cells) + f" | {sum(counts.values())} |")
    return lines


def _round_boundaries(queries: Sequence[QueryRow], rounds: Sequence[int]) -> dict[int, str]:
    """When each round's searching began, as a timestamp string.

    The earliest `ran_at` in the round. Timestamps here are ISO-8601 in UTC to
    the second, so string comparison is chronological comparison and no parsing
    is needed to order them.
    """
    return {
        number: min(query.ran_at for query in queries if query.round == number)
        for number in rounds
    }


def _decisions_in(
    decisions: Sequence[DecisionRow], boundaries: dict[int, str], number: int
) -> list[DecisionRow]:
    """Decisions belonging to one round, under the sequential-loop assumption.

    A decision made *before* the first round's first query — which happens when
    a gate rejects a video the search already returned in an earlier run — falls
    into the first round rather than nowhere. Dropping it would make the
    per-round counts silently disagree with the total, and a report whose parts
    do not sum to its whole is one nobody trusts twice.
    """
    ordered = sorted(boundaries.items())
    start = boundaries[number]
    following = [when for other, when in ordered if other > number]
    end = following[0] if following else None
    first = ordered[0][0]
    return [
        row
        for row in decisions
        if (row.decided_at >= start or (number == first and row.decided_at < start))
        and (end is None or row.decided_at < end)
    ]


# -- the other four files ------------------------------------------------------


def _render_brief(run: RunRow) -> str:
    return "\n".join(
        [
            f"# Brief — {run.topic}",
            "",
            f"Run `{run.run_id}`, written by prompt v{run.brief_version or MISSING}.",
            *_brief_made(run.brief_outcome),
            "",
            run.brief.strip() or "(the run stored no brief)",
            "",
        ]
    )


def _brief_made(brief_outcome: str | None) -> list[str]:
    """How the brief was made, when it was not one draft and one review.

    Nothing for a run made before drafts were checked (NULL) or for the
    ordinary case, so the line appears exactly when a reader needs it: a
    retried draft or an unreviewed brief changes what every verdict in the run
    was measured against.
    """
    if not brief_outcome:
        return []
    try:
        outcome = json.loads(brief_outcome)
    except ValueError:
        return ["", f"How it was made: {_cell(brief_outcome)}"]
    lines = []
    # Which brief the run used. Absent on runs made before approved briefs,
    # which all wrote a fresh one.
    if outcome.get("source") == "approved":
        lines.append(
            f"- The topic's approved brief v{outcome.get('approved_version')} was reused "
            f"(sha256 {str(outcome.get('sha256') or '')[:12]}); no brief was written."
        )
    for number, problem in enumerate(outcome.get("draft_problems") or [], start=1):
        lines.append(f"- Draft {number} was unusable ({problem}) and was drawn again.")
    if outcome.get("review_problem"):
        lines.append(
            f"- The review was unusable ({outcome['review_problem']}); "
            "this is the unreviewed draft."
        )
    return ["", *lines] if lines else []


def render_map(run: RunRow, territory: dict[str, Any] | None) -> str:
    """The map, its revisions, and the diff each revision made.

    A revision that says only "removed `caching`" is a change you cannot judge.
    The previous version's term lists are stored on the run for exactly this,
    so the diff is rendered against them rather than described.
    """
    lines = [f"# Territory — {run.topic}", ""]
    if not territory:
        lines.extend(
            [
                "No territory map was stored for this run. Runs before B4 have none,",
                "and a mapping call that failed leaves discovery narrower, not broken.",
                "",
            ]
        )
        return "\n".join(lines)

    revision = territory.get("revision", 1)
    plan = territory.get("plan") if territory.get("source") == "plan" else None
    if territory.get("fallback"):
        lines.extend([f"**A search plan was asked for and not used**: {territory['fallback']}. "
                      "The map below wrote round one's queries.", ""])
    if plan is not None:
        lines.extend(_render_plan(plan))
    lines.extend(
        [
            f"Prompt v{territory.get('version') or MISSING}, revision {revision}.",
            "",
            "## Final map",
            "",
            "**Concepts** — what falls under the brief:",
            "",
        ]
    )
    lines.extend(_bullets(territory.get("concepts")))
    lines.extend(["", "**Vocabulary** — words the subject uses, never on their own a "
                  "reason to keep a video:", ""])
    lines.extend(_bullets(territory.get("vocabulary")))

    recency = territory.get("recency") or {}
    months = recency.get("months")
    lines.extend(
        [
            "",
            "**Recency**: "
            + (f"{months} months" if months else "any time")
            + f" — {recency.get('reason') or 'no reason given'}",
            "",
            "## Queries the " + ("plan" if plan is not None else "map") + " proposed",
            "",
        ]
    )
    for query in territory.get("queries") or []:
        filters = query.get("filters") or {}
        suffix = f" — filters: {filters}" if filters else ""
        variant = f" (variant of “{query['variant_of']}”)" if query.get("variant_of") else ""
        lines.append(f"- **{query.get('text')}**{variant}{suffix}")
    if not territory.get("queries"):
        lines.append("- none")

    rejected = territory.get("rejected") or []
    lines.extend(["", "## Filters the validator refused", ""])
    if rejected:
        # Every refusal widened the search. Listed so that "why did this run see
        # more than I asked for?" has an answer that is not a shrug.
        lines.extend(
            f"- `{item.get('field')}={item.get('value')}` on “{item.get('query')}” — "
            f"{item.get('reason')}"
            for item in rejected
        )
    else:
        lines.append("- none")

    lines.extend(["", "## Revisions", ""])
    revisions = territory.get("revisions") or []
    if not revisions:
        lines.append("The map was never revised.")
    else:
        for item in revisions:
            sign = "+" if item.get("action") == "add" else "−"
            lines.append(f"- {sign} **{item.get('kind')}**: {item.get('term')}")
            lines.append(f"  - reason: {item.get('reason') or MISSING}")
            if item.get("evidence"):
                lines.append(f"  - evidence: {item['evidence']}")

    history = territory.get("history") or []
    if history:
        lines.extend(["", "## Diffs", ""])
        previous = history[0]
        for snapshot in [*history[1:], territory]:
            lines.extend(_diff(previous, snapshot))
            previous = snapshot
    lines.append("")
    return "\n".join(lines)


def _round_one_source(run: RunRow) -> str:
    """Which source wrote round one's planned queries, from the stored map."""
    territory = _territory(run)
    if not territory:
        return "none"
    if territory.get("source") == "plan":
        return f"search plan ({territory.get('version') or MISSING})"
    if territory.get("fallback"):
        return f"territory map — {territory['fallback']}"
    return "territory map"


def _render_plan(plan: dict[str, Any]) -> list[str]:
    """The search plan's items, languages and counters, as `plan/search` wrote them."""
    lines = ["## Search plan", "", "Round one's planned queries came from this plan, not from a map.", ""]
    for item in plan.get("items") or []:
        lines.append(f"### P{item.get('part')}: {item.get('item')}")
        lines.append("")
        lines.append(f"- intent: {item.get('intent') or MISSING}")
        lines.append(f"- titled: {', '.join(item.get('titled') or []) or MISSING}")
        lines.append(f"- languages: {', '.join(item.get('languages') or []) or 'none set'}")
        lines.append(f"- tier: {item.get('tier') or MISSING}"
                     + (f" (line: {item['line']})" if item.get("line") else ""))
        lines.append(f"- coverage: {item.get('state') or MISSING}"
                     + (f" (covers: {item['covers']})" if item.get("covers") else ""))
        lines.append("- queries:")
        lines.extend(f"  - {query}" for query in item.get("queries") or [])
        if not item.get("queries"):
            lines.append("  - none")
        lines.append("")
    lines.extend(["### Languages", ""])
    languages = plan.get("languages") or []
    lines.extend(
        f"- {language.get('name')} ({language.get('code') or 'no code'}, "
        f"{language.get('script') or 'script not given'}): {language.get('how') or MISSING}"
        for language in languages
    )
    if not languages:
        lines.append("- none named")
    lines.extend(
        [
            "",
            "### Counted",
            "",
            f"- PARTS with no query: {len(plan.get('parts_without_query') or [])}",
            *(f"  - {name}" for name in plan.get("parts_without_query") or []),
            f"- items naming no PART of the brief: {len(plan.get('unknown_items') or [])}",
            f"- queries under those items, not run: {plan.get('queries_dropped', 0)}",
            f"- listed things the brief does not list: {plan.get('things_unmatched', 0)}",
            f"- queries not in their language's title script: {plan.get('script_mismatches', 0)}",
            f"- languages with no query: {len(plan.get('languages_without_query') or [])}",
            f"- coverage: {plan.get('coverage') or MISSING} — {plan.get('coverage_note') or 'no note'}",
            "- queries by state: " + (", ".join(
                f"{state} {count}" for state, count in (plan.get("queries_by_state") or {}).items()
            ) or "none"),
            "- queries by language: " + (", ".join(
                f"{code} {count}" for code, count in (plan.get("queries_by_language") or {}).items()
            ) or "none") + f" (main {plan.get('main_language') or 'not named'})",
            "- language items: " + (", ".join(f"P{n}" for n in plan.get("language_items") or [])
                                    or "none") + f"; broad item: P{plan.get('broad_item') or '?'}",
            f"- queries in another language aimed at a narrow part: {plan.get('language_misaimed', 0)}",
            "- queries by tier: " + (", ".join(
                f"{tier} {count}" for tier, count in (plan.get("queries_by_tier") or {}).items()
            ) or "none"),
            f"- distinct gaps: {plan.get('distinct_gaps', 0)}; duplicates avoided: "
            f"{plan.get('duplicates_avoided', 0)}",
            f"- gaps skipped as excluded: {len(plan.get('excluded_gaps') or [])}",
            *(f"  - {gap}" for gap in plan.get("excluded_gaps") or []),
            f"- coverage gaps with no query: {len(plan.get('unserved_gaps') or [])}",
            *(f"  - {gap}" for gap in plan.get("unserved_gaps") or []),
            f"- queries on held items while a gap had none: {plan.get('held_while_gaps', 0)}",
            f"- queries over the budget, not run: {plan.get('over_budget', 0)}",
            f"- items matching no coverage line: {plan.get('coverage_unmatched', 0)}",
            f"- current context: {plan.get('context') or 'absent'}"
            + (f", {plan.get('context_lines')} untrusted line(s)" if plan.get("context") == "supplied" else ""),
            "",
        ]
    )
    return lines


def _diff(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    lines = [
        f"### revision {before.get('revision', '?')} → {after.get('revision', '?')}",
        "",
    ]
    for kind in ("concepts", "vocabulary"):
        old = list(before.get(kind) or [])
        new = list(after.get(kind) or [])
        added = [term for term in new if term not in old]
        removed = [term for term in old if term not in new]
        if not added and not removed:
            continue
        lines.append(f"- {kind}:")
        lines.extend(f"  - + {term}" for term in added)
        lines.extend(f"  - − {term}" for term in removed)
    if len(lines) == 2:
        lines.append("- no change to either term list")
    lines.append("")
    return lines


def _render_queries(queries: Sequence[QueryRow]) -> str:
    header = (
        "round\tquery\torigin\torigin_detail\tfilters\tresults\tprovider\tquota\tran_at"
    )
    rows = [
        "\t".join(
            [
                str(query.round),
                _cell(query.query),
                _cell(query.origin),
                _cell(query.origin_detail),
                _filters(query.filters_json),
                MISSING if query.results_count is None else str(query.results_count),
                _cell(query.provider),
                MISSING if query.quota_units is None else str(query.quota_units),
                _cell(query.ran_at),
            ]
        )
        for query in queries
    ]
    return "\n".join([header, *rows])


def _render_decisions(decisions: Sequence[DecisionRow]) -> str:
    header = (
        "video_id\tstage\tverdict\tevidence\tprompt_version\tdecided_at\ttopic"
        "\trelevant\treason"
    )
    rows = [
        "\t".join(
            [
                _cell(row.video_id),
                _cell(row.stage),
                _cell(row.verdict),
                _cell(row.evidence),
                _cell(row.prompt_version),
                _cell(row.decided_at),
                _cell(row.topic),
                _cell(row.relevant),
                _cell(row.reason),
            ]
        )
        for row in decisions
    ]
    return "\n".join([header, *rows])


# -- shared -------------------------------------------------------------------


def _territory(run: RunRow) -> dict[str, Any] | None:
    if not run.territory_map:
        return None
    try:
        loaded = json.loads(run.territory_map)
    except ValueError:
        log.warning("export: run %s has an unreadable territory map", run.run_id)
        return None
    return loaded if isinstance(loaded, dict) else None


def _filters(filters_json: str | None) -> str:
    """Filters as ``key=value`` pairs, or ``-``. Reasons are dropped here.

    ``filters_json`` stores each filter's value *and* the reason the model gave
    for it. The reason belongs in ``territory.md``, where there is room for a
    sentence; a TSV cell holding a paragraph is a TSV nobody can read.
    """
    if not filters_json:
        return MISSING
    try:
        loaded = json.loads(filters_json)
    except ValueError:
        return _cell(filters_json)
    if not isinstance(loaded, dict) or not loaded:
        return MISSING
    parts = []
    for key, value in sorted(loaded.items()):
        if isinstance(value, dict):
            value = value.get("value", "")
        if value not in (None, ""):
            parts.append(f"{key}={value}")
    return " ".join(parts) or MISSING


def _cell(value: str | None) -> str:
    """One TSV cell: never empty, never containing a tab or a newline.

    A stray tab inside a judge's reason would shift every column after it, and
    the file would still open — silently wrong, which is the failure mode this
    project keeps finding. Collapsing whitespace makes that impossible.
    """
    if value is None:
        return MISSING
    flat = _flat(value)
    return flat or MISSING


def _flat(value: str | None) -> str:
    return " ".join((value or "").split())


def _bullets(items: Iterable[str] | None) -> list[str]:
    listed = [f"- {item}" for item in (items or [])]
    return listed or ["- none"]
