"""Which gap a query or a keeper is on, and when a gap has been chased enough.

Every source of queries — the brief's expansion, its threads, the planner and
the search plan — names what it aims at in its own words: an expansion aim, a
planner's gap label, a plan item's coverage line. Placed here by one rule, the
same for a query and for a keeper's stage-1 tag, so "queries aimed at a gap"
and "keepers on it" count the same thing.

**While a gap is open, no source spends a query on what is held.** Measured on
two real runs: after round one the planner named one gap left, and the
expansion's threads then spent 20 queries on held parts and their concepts and
brought about 12 keepers, nearly all repeats. A query is placed:

- on a gap, when the stage-1 matcher puts its aim on a missing or partly held
  line, or when its aim names most of a gap line's own words (a planner's label
  is free text, and rarely every word of a coverage line);
- held, when the matcher puts it on a held line and it names no gap;
- a thread, when its aim is a concept (``thread: …``). A thread on a concept
  only a keeper on no gap gave is held back while a gap is open; a query a
  held keeper's concept led to that aims at a gap is a gap query, and runs;
- unaimed or unmatched otherwise, and kept: dropping a query on a wording slip
  narrows the search, the expensive direction.

**A gap is the brief item it belongs to.** The planner rewords its coverage
every round — measured, the three missing things of one part were named in
other words in each of three rounds, sharing almost none — so a gap followed
by its wording is a new gap every round and never exhausted. The brief's items
do not move: each gap line is placed on its item as rule 42's resolver places
a baseline line (`BriefAnchor`). With no brief items to place on, a gap is its
own words.

**A gap is exhausted** when its most recent rounds — at least two, in a row,
each of which aimed queries at it or kept something on it — brought no
stage-2 keeper on it and at least ``after`` queries. Counted over the latest
rounds rather than the whole run, because an item can hold two gaps: one
filled in round one, and one that has been failing since. It gets no more
queries this run, and when every open gap is exhausted the run stops.

Pure: no I/O, and nothing here calls a model.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence

from ..contracts import (
    ITEM_HELD,
    ITEM_MISSING,
    ITEM_PARTLY,
    CoverageItem,
    GapTally,
    PlannedQuery,
)
from .coverage import BriefAnchor
from .judge import aim_words, match_item
from .search_plan import _gap_lines, gap_key

__all__ = [
    "KIND_GAP", "KIND_HELD", "KIND_THREAD", "KIND_UNAIMED", "KIND_UNMATCHED",
    "Gap", "Placement", "credit_keeper", "exhausted", "gate", "held_thread", "open_gaps", "place",
    "query_aim", "tally_queries", "unexhausted", "window",
]

KIND_GAP = "gap"
KIND_HELD = "held"
KIND_THREAD = "thread"
KIND_UNAIMED = "unaimed"
KIND_UNMATCHED = "unmatched"

# An aim names a gap when it carries at least this share of a gap line's words.
_GAP_SHARE = 0.5


@dataclass(frozen=True)
class Gap:
    """One gap: its identity, its name, and the coverage lines that name it."""

    key: str
    label: str
    lines: tuple[str, ...]
    parts: tuple[str, ...]
    words: tuple[frozenset[str], ...]


@dataclass(frozen=True)
class Placement:
    kind: str
    gaps: tuple[Gap, ...] = ()


def open_gaps(coverage: Sequence[CoverageItem], anchor: BriefAnchor | None = None) -> list[Gap]:
    """Every gap the coverage names, once per brief item (or per wording, with
    no brief item to place it on)."""
    found: dict[str, list] = {}
    for line, item in _gap_lines(coverage):
        index = anchor.item(item.part) if anchor is not None else None
        if index is not None:
            key, label = f"item {index}", anchor.items[index][0]
        else:
            words = gap_key(line, item)
            if not words:
                continue
            key, label = "words " + " ".join(sorted(words)), line
        entry = found.setdefault(key, [label, [], [], []])
        entry[1].append(line)
        entry[2].append(item.part)
        entry[3].append(frozenset(gap_key(line, item)))
    return [
        Gap(key, label, tuple(lines), tuple(dict.fromkeys(parts)), tuple(words))
        for key, (label, lines, parts, words) in found.items()
    ]


def query_aim(query: PlannedQuery) -> str:
    """What a query says it aims at, from its `origin_detail`, or "".

    ``aim: …`` from the expansion and the plan, ``gap — …`` from the planner. An
    expansion query with no aim carries its threads or the topic there, and
    that is not an aim.
    """
    detail = query.origin_detail.split(" | ", 1)[0].strip()
    lowered = detail.lower()
    if lowered.startswith("aim:"):
        return detail[4:].strip()
    for mark in ("gap — ", "gap – ", "gap - ", "gap: "):
        if lowered.startswith(mark):
            return detail[len(mark):].strip()
    return ""


def place(aim: str, coverage: Sequence[CoverageItem], gaps: Sequence[Gap]) -> Placement:
    """Where an aim — a query's, or a keeper's stage-1 tag — sits in the coverage."""
    if not aim.strip():
        return Placement(KIND_UNAIMED)
    if aim.strip().lower().startswith("thread:"):
        return Placement(KIND_THREAD)
    state, line, _unmatched = match_item(aim, coverage)
    if state in (ITEM_MISSING, ITEM_PARTLY):
        named = tuple(gap for gap in gaps if line in gap.lines or line in gap.parts)
        if named:
            return Placement(KIND_GAP, named)
    loose = _named_gaps(aim, gaps)
    if loose:
        return Placement(KIND_GAP, loose)
    if state == ITEM_HELD:
        return Placement(KIND_HELD)
    return Placement(KIND_UNMATCHED)


def _named_gaps(aim: str, gaps: Sequence[Gap]) -> tuple[Gap, ...]:
    """The gaps one of whose lines an aim carries the most of, at `_GAP_SHARE` or more."""
    words = aim_words(aim)
    if not words:
        return ()
    scored = [
        (max((len(words & line) / len(line) for line in gap.words if line), default=0.0), gap)
        for gap in gaps
    ]
    best = max((share for share, _ in scored), default=0.0)
    if best < _GAP_SHARE:
        return ()
    return tuple(gap for share, gap in scored if share == best)


# -- the tallies --------------------------------------------------------------


def _tally(tallies: list[GapTally], gap: Gap) -> GapTally:
    for tally in tallies:
        if tally.key == gap.key:
            return tally
    tally = GapTally(key=gap.key, line=gap.label)
    tallies.append(tally)
    return tally


def _add(tallies: list[GapTally], gap: Gap, number: int, *, queries: int = 0, keepers: int = 0) -> None:
    tally = _tally(tallies, gap)
    rounds = list(tally.rounds)
    if rounds and rounds[-1][0] == number:
        _, q, k = rounds[-1]
        rounds[-1] = (number, q + queries, k + keepers)
    else:
        rounds.append((number, queries, keepers))
    tallies[tallies.index(tally)] = replace(
        tally, rounds=tuple(rounds), carried=tally.carried and not keepers
    )


def tally_queries(
    tallies: list[GapTally],
    queries: Sequence[PlannedQuery],
    coverage: Sequence[CoverageItem],
    number: int,
    anchor: BriefAnchor | None = None,
) -> None:
    """Count the queries a round ran against the gaps they aimed at, once per gap."""
    gaps = open_gaps(coverage, anchor)
    for query in queries:
        for gap in place(query_aim(query), coverage, gaps).gaps:
            _add(tallies, gap, number, queries=1)


def credit_keeper(
    tallies: list[GapTally],
    tag_line: str,
    coverage: Sequence[CoverageItem],
    number: int,
    anchor: BriefAnchor | None = None,
) -> int:
    """Credit a keeper to the gaps its stage-1 tag names; how many it credited.

    A keeper tagged with a part held in part credits that part's gaps:
    crediting too widely keeps a gap open, the cheap direction.
    """
    placed = place(tag_line, coverage, open_gaps(coverage, anchor))
    for gap in placed.gaps:
        _add(tallies, gap, number, keepers=1)
    return len(placed.gaps)


def window(tally: GapTally) -> tuple[int, int]:
    """``(rounds, queries)`` of the gap's latest rounds in a row with no keeper."""
    rounds = queries = 0
    for _, q, k in reversed(tally.rounds):
        if k:
            break
        rounds += 1
        queries += q
    return rounds, queries


def exhausted(tally: GapTally, after: int) -> bool:
    """At least `after` queries over at least two rounds in a row, and no keeper."""
    rounds, queries = window(tally)
    return rounds >= 2 and queries >= after


def unexhausted(
    coverage: Sequence[CoverageItem],
    tallies: Sequence[GapTally],
    after: int,
    anchor: BriefAnchor | None = None,
) -> list[Gap]:
    """The coverage's open gaps that are not exhausted."""
    spent = {tally.key for tally in tallies if exhausted(tally, after)}
    return [gap for gap in open_gaps(coverage, anchor) if gap.key not in spent]


def held_thread(aim: str, held_concepts: Sequence[str]) -> bool:
    """Is this ``thread: …`` aim one of the concepts only held keepers gave?"""
    words = aim_words(aim.split(":", 1)[1] if ":" in aim else aim)
    return bool(words) and any(
        (concept := aim_words(text)) and (concept <= words or words <= concept)
        for text in held_concepts
    )


def gate(
    queries: Sequence[PlannedQuery],
    coverage: Sequence[CoverageItem],
    tallies: Sequence[GapTally],
    after: int,
    anchor: BriefAnchor | None = None,
    held_concepts: Sequence[str] = (),
) -> tuple[list[PlannedQuery], dict[str, int], dict[str, int], dict[str, int]]:
    """``(kept, held_while_gaps, exhausted_skipped, threads_held_back)``: this
    round's queries with those on what is held, on an exhausted gap, or on a
    concept only keepers on no gap gave (``held_concepts``), taken out while a
    gap that is not exhausted is open. The counts are per source (`origin`).

    A source left with nothing aimed at a gap runs fewer queries, never held
    ones. A carried gap — one an earlier run recorded as exhausted — takes
    queries only up to the threshold, so it gets its one probe and no more.
    With no open gap nothing is taken out: when nothing is missing, stopping
    is the planner's call (rule 37).
    """
    gaps = open_gaps(coverage, anchor)
    if not unexhausted(coverage, tallies, after, anchor):
        return list(queries), {}, {}, {}
    by_key = {tally.key: tally for tally in tallies}
    admitted: dict[str, int] = {}
    kept: list[PlannedQuery] = []
    held: dict[str, int] = {}
    spent: dict[str, int] = {}
    threads: dict[str, int] = {}
    for query in queries:
        aim = query_aim(query)
        placed = place(aim, coverage, gaps)
        if placed.kind == KIND_HELD:
            held[query.origin] = held.get(query.origin, 0) + 1
            continue
        if placed.kind == KIND_THREAD and held_thread(aim, held_concepts):
            threads[query.origin] = threads.get(query.origin, 0) + 1
            continue
        if placed.kind == KIND_GAP:
            live = []
            for gap in placed.gaps:
                tally = by_key.get(gap.key)
                if tally is not None and exhausted(tally, after):
                    continue
                if tally is not None and tally.carried and (
                    window(tally)[1] + admitted.get(gap.key, 0) >= after
                ):
                    continue
                live.append(gap)
            if not live:
                spent[query.origin] = spent.get(query.origin, 0) + 1
                continue
            for gap in live:
                admitted[gap.key] = admitted.get(gap.key, 0) + 1
        kept.append(query)
    return kept, held, spent, threads
