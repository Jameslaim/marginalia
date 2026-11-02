"""Several baseline replies on one state, resolved into one coverage.

The baseline is one model reading the library's evidence against the brief, and
on unchanged notes it does not always answer the same way: a part held in one
call is partly held in the next, and a listed thing held in one is missing in
another. The two errors do not cost the same. A thing wrongly marked missing
costs one query; a thing wrongly marked held is never searched for. So a
disagreement resolves toward missing, and every one resolved is counted.

Calls name the same thing in different words ("text split into tokens", "how
text is split into tokens — <evidence>"), so items and things are matched
through the brief: each is placed on the brief item, and the listed thing of
that item, its content words agree with most.

Pure: no I/O, and nothing here calls a model.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Sequence

from ..contracts import CoverageItem, LibraryCoverage
from .judge import aim_words, brief_items, brief_parts

__all__ = [
    "FLIP_HELD", "FLIP_STATE", "FLIP_UNLISTED", "BriefAnchor", "disagree", "names_only",
    "resolve_coverage", "thing_name",
]

_RANK = {"no": 0, "partly": 1, "yes": 2}

# The three kinds of disagreement, each resolved toward missing and counted.
FLIP_STATE = "item state"            # YES in one call, PARTLY or NO in another
FLIP_HELD = "held vs missing"        # a listed thing held in one, missing in another
FLIP_UNLISTED = "missing vs unlisted"  # missing in one, not named by another


def disagree(calls: Sequence[LibraryCoverage], brief_text: str = "") -> bool:
    """Did these calls disagree on anything `resolve_coverage` would resolve?"""
    return bool(resolve_coverage(calls, brief_text)[1])
# A thing is placed on a brief thing only when at least this share of the
# brief thing's content words appear in it.
_THING_SHARE = 0.5
# Two unplaced lines are one thing when this share of the shorter's content
# words appear in the other.
_LOOSE_SHARE = 0.6


def thing_name(text: str) -> str:
    """A listed thing without the evidence a call writes after it."""
    for mark in (" — ", " – ", " -- "):
        if mark in text:
            text = text.split(mark, 1)[0]
    return " ".join(text.split())


def names_only(coverage: LibraryCoverage) -> LibraryCoverage:
    """The coverage with every missing thing cut to its name.

    A missing thing names what to search for and carries no evidence: measured,
    a baseline wrote video ids and a paraphrase after the dash, and they went
    on into the plan's items and every query's origin. Applied where a reply
    is parsed and where a stored baseline is read back, so one written before
    this is read the same way and needs no new key.
    """
    return replace(coverage, items=[
        replace(item, missing=tuple(dict.fromkeys(thing_name(thing) for thing in item.missing)))
        for item in coverage.items
    ])


class BriefAnchor:
    """The brief's items and their listed things, to place a call's lines on."""

    def __init__(self, brief_text: str) -> None:
        self.items: list[tuple[str, list[str]]] = [
            (name, things) for name, things in brief_parts(brief_text)
        ] + [(item, []) for item in brief_items(brief_text, "ALSO WANTED")]
        self._words = [aim_words(name) | set().union(*map(aim_words, things)) if things
                       else aim_words(name) for name, things in self.items]
        # Lines no brief thing matches, clustered per item as they are seen, so
        # two calls' wordings of one gap are one gap and not two.
        self._loose: dict[object, list[set[str]]] = {}

    def item(self, part: str) -> int | None:
        words = aim_words(part)
        if not words:
            return None
        scored = [(len(words & item) / len(words), -index) for index, item in enumerate(self._words)]
        share, index = max(scored, default=(0.0, 0))
        return -index if share >= 0.5 else None

    def thing(self, item: int | None, text: str) -> tuple | str:
        """``(item, n)`` for the brief thing this names, else its own words."""
        words = aim_words(thing_name(text))
        if item is not None:
            best = max(
                ((len(words & aim_words(thing)) / max(1, len(aim_words(thing))), -n)
                 for n, thing in enumerate(self.items[item][1])),
                default=(0.0, 0),
            )
            if best[0] >= _THING_SHARE:
                return (item, -best[1])
        if not words:
            return thing_name(text).lower()
        clusters = self._loose.setdefault(item, [])
        for number, cluster in enumerate(clusters):
            if len(words & cluster) / min(len(words), len(cluster)) >= _LOOSE_SHARE:
                return ("loose", item, number)
        clusters.append(set(words))
        return ("loose", item, len(clusters) - 1)

    def label(self, key: tuple | str) -> str:
        """The brief's own words for an anchored thing; unanchored ones unnamed."""
        if isinstance(key, tuple) and key[0] != "loose":
            return self.items[key[0]][1][key[1]]
        return ""


def resolve_coverage(
    calls: Sequence[LibraryCoverage], brief_text: str = ""
) -> tuple[LibraryCoverage, list[dict]]:
    """``(coverage, flips)`` from one or more usable baselines on one state.

    Per item, the least-held state any call gave it (NO, then PARTLY, then
    YES). Per listed thing, missing when any call listed it missing, and held
    only when no call did; in a part resolved NO nothing is held. A part left
    with a missing thing is never YES. The detail is the least-held call's.
    The first call's order is kept, and an item only a later call assessed is
    added after.

    ``flips`` lists every item whose state differed between calls and every
    listed thing one call held and another listed missing, each with its
    states and what it was resolved to, named in the brief's words (rule 1:
    never the evidence a call wrote). One call comes back as it was.
    """
    if not calls:
        return LibraryCoverage(), []
    if len(calls) == 1:
        return calls[0], []
    anchor = BriefAnchor(brief_text)
    groups: list[tuple[object, list[CoverageItem | None]]] = []
    for position, call in enumerate(calls):
        for item in call.items:
            index = anchor.item(item.part)
            key = ("item", index) if index is not None else ("part", " ".join(sorted(aim_words(item.part))))
            group = next((g for k, g in groups if k == key and g[position] is None), None)
            if group is None:
                group = [None] * len(calls)
                groups.append((key, group))
            group[position] = item
    items: list[CoverageItem] = []
    flips: list[dict] = []
    for key, group in groups:
        index = key[1] if key[0] == "item" else None
        present = [item for item in group if item is not None]
        marks = [item.mark if item is not None else "" for item in group]
        least = min(present, key=lambda item: _RANK[item.mark])
        held_in: dict = {}
        missing_in: dict = {}
        names: dict = {}
        missing_names: dict = {}
        for position, item in enumerate(group):
            if item is None:
                continue
            for thing in item.held:
                k = anchor.thing(index, thing)
                names.setdefault(k, thing)
                held_in.setdefault(k, set()).add(position)
            for thing in item.missing:
                k = anchor.thing(index, thing)
                names.setdefault(k, thing)
                missing_names.setdefault(k, thing_name(thing))
                missing_in.setdefault(k, set()).add(position)
        missing = [missing_names[k] for k in names if k in missing_in]
        held = [names[k] for k in names if k not in missing_in]
        mark = least.mark
        if mark == "yes" and missing:
            mark = "partly"
        if mark == "no":
            # Nothing is held in a part one call found nothing of: what another
            # call held there is searched for, the cheap direction.
            missing, held = missing + held, []
        part = present[0].part
        brief_part = anchor.items[index][0] if index is not None else ""
        if len({m for m in marks if m}) > 1:
            flips.append({"kind": FLIP_STATE, "part": brief_part or part, "states": marks,
                          "resolved": mark})
        assessed = {p for p, item in enumerate(group) if item is not None}
        for k in names:
            if k not in missing_in:
                continue
            if k in held_in:
                kind = FLIP_HELD
            elif missing_in[k] != assessed:
                kind = FLIP_UNLISTED
            else:
                continue
            states = [
                "missing" if p in missing_in[k] else "held" if p in held_in.get(k, ()) else
                "unlisted" if p in assessed else "" for p in range(len(group))
            ]
            # Named in the brief's words, or a missing thing's own name: a
            # missing thing carries no evidence, a held one may (rule 1).
            flips.append({"kind": kind, "part": brief_part or part,
                          "thing": anchor.label(k) or missing_names[k],
                          "states": states, "resolved": "missing"})
        items.append(CoverageItem(
            part=part,
            satisfied=mark == "yes",
            detail=least.detail,
            partly=mark == "partly",
            held=tuple(held),
            missing=tuple(missing),
        ))
    resolved = replace(
        calls[0],
        items=items,
        input_tokens=sum(call.input_tokens for call in calls),
        output_tokens=sum(call.output_tokens for call in calls),
        latency_s=sum(call.latency_s for call in calls),
    )
    return resolved, flips
