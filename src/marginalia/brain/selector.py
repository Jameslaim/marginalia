"""M7 stage 1: which notes the answering call reads.

**The constraint that decides this module.** Both stages read *whole notes*,
never fragments. The standard approach — chunk each note, embed the chunks,
retrieve the chunks — destroys the whole-video understanding the note format
exists to produce, which is the same argument that rejected chunking transcripts
at the start of the project (PLAN.md §10). This file therefore contains no
embedding, no vector, no similarity score and no fragment. It reads one line per
note and returns identifiers.

**Rule 8 arrives here in a new place.** A note wrongly excluded is invisible:
the answer never sees it, cites around it, and nothing reports that it existed.
So every failure in this module resolves toward loading more — a refused call, a
truncated reply, an unparseable list and an empty list all fall through to the
caller's budget fill rather than to a narrow selection.

Rule 9: no filesystem, no database, no provider. Summary lines in, identifiers
out.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Sequence

from ..contracts import (
    SELECTION_SELECTED,
    NoteSummary,
    Selection,
)
from ..llm import LlmClient, LlmConfigError, Prompt, batched, load_prompt

log = logging.getLogger(__name__)

__all__ = ["BATCH_SIZE", "SELECT_PROMPT", "Selector", "parse_selection"]

SELECT_PROMPT = "select_notes"

_DROPPED_RE = re.compile(r"^\s*\**\s*DROPPED\s*\**\s*:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
_WHY_RE = re.compile(r"^\s*\**\s*WHY\s*\**\s*:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")

_MAX_WHY = 300

# A list of ids needs nowhere near this. It is a ceiling on a runaway, not a
# budget for an answer: v1 of this prompt looped and spent 32,768 output tokens
# repeating ids, and the cost of that is what this number bounds.
SELECT_MAX_TOKENS = 4096


# Notes shown to the selector in one call. **Sixty, and structural rather than
# another guess at wording.** Five measurements across four framings, and the
# model has never once handled a list of three hundred:
#
#   keep-list                 looped, 3,351 ids on one line
#   exclusion-list, uncapped  dropped 297 of 309
#   exclusion-list, capped    truncated at 318 ids, twice
#   forced quota              asked to drop 409 of 422
#   forced quota              asked to drop 371 of 422, truncated twice
#
# Two things follow from sixty. Truncation stops being reachable — a batch drops
# about twenty-three ids, a few hundred tokens against a ceiling of thousands —
# and *ranking* becomes a request a model can actually satisfy, which four
# hundred items is not.
BATCH_SIZE = 60


def _quotas(total: int, sizes: Sequence[int]) -> list[int]:
    """Split a drop quota across batches, proportionally, summing exactly.

    Largest-remainder, so the parts add up to the whole: handing each batch
    `round(share)` independently loses or gains notes against the budget, and
    the caller would then trim the difference by position — the arbitrary
    exclusion this whole design is avoiding.
    """
    if not sizes or total <= 0:
        return [0] * len(sizes)
    counted = sum(sizes)
    exact = [total * size / counted for size in sizes]
    quotas = [int(value) for value in exact]
    remainder = total - sum(quotas)
    # Give the leftovers to the batches with the largest fractional part.
    order = sorted(range(len(sizes)), key=lambda i: exact[i] - quotas[i], reverse=True)
    for i in order[:remainder]:
        quotas[i] += 1
    return [min(q, size) for q, size in zip(quotas, sizes)]


def parse_selection(text: str, *, known: Sequence[str], most: int | None = None) -> Selection:
    """Read a stage-1 reply into the set of notes to *keep*.

    **The reply names what to drop, and everything unnamed is kept.** Rule 8 is
    in the format rather than only in the prompt: a reply that names nothing
    keeps everything, one that names half of what it meant to drops half as
    much, and one that cannot be read at all drops nothing here — the caller
    fills the budget instead.

    **`most` is the quota the code enforces, not a request the prompt makes.**
    It is now exactly the number of notes that have to go, and excess drops are
    re-included from the end of the named list and counted. Measured twice on
    the way here: asked with no cap the model dropped 297 of 309, and asked for
    *at most* twice the requirement it took the whole allowance and under-filled
    the budget by 60%. Same shape as the search-filter validator — the model
    proposes, the code decides how much of it runs.

    Naming *fewer* than the quota is left alone. It is the safe direction: the
    keep set is then larger than the budget and the caller trims the remainder,
    so a short reply costs a little arbitrary trimming rather than a lot.

    Ids are checked against the notes that were offered, and case is never
    repaired. Rule 5: two ids differing by case are two videos, so a "repair"
    would drop a note the model never named. An unmatched id is ignored and
    counted, which is the safe direction — it drops nothing.
    """
    body = _DROPPED_RE.search(text or "")
    if body is None:
        return Selection(
            mode=SELECTION_SELECTED,
            considered=len(known),
            parsed=False,
            problem="no DROPPED line",
        )

    offered = set(known)
    named: list[str] = []
    for video_id in _ID_RE.findall(body.group(1)):
        if video_id in offered and video_id not in named:
            named.append(video_id)
    unknown = len({v for v in _ID_RE.findall(body.group(1))}) - len(named)

    why = _WHY_RE.search(text or "")
    reason = " ".join((why.group(1) if why else "").split())[:_MAX_WHY]

    problems: list[str] = []
    if unknown:
        problems.append(f"{unknown} id(s) not in the library")
        log.warning("select: %d dropped id(s) were not offered — ignored", unknown)

    requested = len(named)
    refused = 0
    if most is not None and len(named) > most:
        refused = len(named) - most
        # Kept from the front: a model listing its drops puts the ones it is
        # surest about first, and re-including from the tail gives back the
        # ones it was least sure of. Widening, either way.
        named = named[:most]
        problems.append(f"{refused} drop(s) over the cap of {most} were refused")
        log.warning(
            "select: the reply dropped %d notes against a cap of %d — %d re-included",
            len(named) + refused,
            most,
            refused,
        )

    dropped = set(named)
    kept = [video_id for video_id in known if video_id not in dropped]
    if not kept:
        return Selection(
            mode=SELECTION_SELECTED,
            considered=len(known),
            reason=reason,
            parsed=False,
            problem="the reply dropped every note",
        )

    return Selection(
        video_ids=kept,
        mode=SELECTION_SELECTED,
        reason=reason or "no reason given",
        considered=len(known),
        # Not `overflowed`: that one means "the budget trimmed a note the model
        # wanted", and these mean "the quota refused a drop the model asked
        # for". Two different losses, and the pair below is what says whether
        # the model ranked or thresholded.
        requested_drops=requested,
        allowed_drops=most if most is not None else 0,
        refused_drops=refused,
        parsed=True,
        problem="; ".join(problems),
    )


def _merge_reasons(reasons: Sequence[str]) -> str:
    """One sentence out of several batch sentences, without inventing one.

    The batches mostly say the same thing in different words, so the first is
    kept whole and the count of the rest is appended. Summarising them with
    another model call would put words in seven models' mouths at once, and
    concatenating seven sentences makes a paragraph nobody reads.
    """
    if not reasons:
        return "no reason given"
    if len(reasons) == 1:
        return reasons[0]
    return f"{reasons[0]} [and {len(reasons) - 1} more batches, similarly]"


class Selector:
    """Owns the selection prompt and the model client. Knows nothing about disk."""

    def __init__(self, client: LlmClient, *, prompts_directory=None) -> None:
        self.client = client
        self._directory = prompts_directory
        self._cache: dict[str, Prompt] = {}

    def prompt(self, name: str) -> Prompt:
        if name not in self._cache:
            self._cache[name] = load_prompt(name, directory=self._directory)
        return self._cache[name]

    def select(
        self,
        question: str,
        summaries: Sequence[NoteSummary],
        *,
        keep: int,
        max_tokens: int | None = None,
    ) -> Selection:
        """Choose which notes to keep, in batches of `BATCH_SIZE`.

        **One call per sixty notes, each with its proportional share of the
        quota.** See `BATCH_SIZE` for why this is structural rather than a sixth
        attempt at wording.

        **A failed batch keeps all of its notes.** That is rule 8 at the level
        the failure now happens: one unreadable reply costs its batch's share of
        the drops — about twenty-three notes' worth of budget, trimmed later by
        position — rather than costing the whole question. Only if *every* batch
        fails does this return `parsed=False`, and the caller then refuses.

        **What batching gives up, recorded rather than solved.** There is no
        global comparison: a note ranking tenth in its batch but fiftieth
        overall is dropped anyway, because each batch fills its own quota. The
        cost is bounded and the assumption behind it is the best available —
        proportional quotas assume quality is spread evenly across batches, and
        since video ids sort essentially at random, the batches *are* random
        samples of the library.
        """
        batches = list(batched(list(summaries), BATCH_SIZE))
        quotas = _quotas(max(0, len(summaries) - keep), [len(batch) for batch in batches])

        kept: list[str] = []
        reasons: list[str] = []
        usage = [0, 0]
        totals = {"requested": 0, "allowed": 0, "refused": 0}
        failures: list[str] = []

        for batch, quota in zip(batches, quotas):
            if quota <= 0:
                # Nothing has to go from this batch, so there is nothing to ask.
                kept.extend(summary.video_id for summary in batch)
                continue
            decision = self._one_batch(question, batch, quota, max_tokens)
            usage[0] += decision.input_tokens
            usage[1] += decision.output_tokens
            totals["requested"] += decision.requested_drops
            totals["allowed"] += decision.allowed_drops
            totals["refused"] += decision.refused_drops
            if not decision.parsed:
                # Keep the whole batch. It costs this batch's share of the
                # quota, not the question.
                failures.append(decision.problem or "unreadable")
                kept.extend(summary.video_id for summary in batch)
                continue
            kept.extend(decision.video_ids)
            if decision.reason:
                reasons.append(decision.reason)

        if failures and len(failures) == len([q for q in quotas if q > 0]):
            return Selection(
                mode=SELECTION_SELECTED,
                considered=len(summaries),
                parsed=False,
                problem=f"every batch failed ({failures[0]})",
                input_tokens=usage[0],
                output_tokens=usage[1],
            )

        problem = ""
        if failures:
            problem = f"{len(failures)} of {len(batches)} batches unreadable ({failures[0]})"
            log.warning(
                "select: %d of %d batches failed — their notes were all kept",
                len(failures),
                len(batches),
            )
        return Selection(
            video_ids=kept,
            mode=SELECTION_SELECTED,
            reason=_merge_reasons(reasons),
            considered=len(summaries),
            requested_drops=totals["requested"],
            allowed_drops=totals["allowed"],
            refused_drops=totals["refused"],
            parsed=True,
            problem=problem,
            input_tokens=usage[0],
            output_tokens=usage[1],
        )

    def _one_batch(
        self,
        question: str,
        batch: Sequence[NoteSummary],
        drop: int,
        max_tokens: int | None,
    ) -> Selection:
        """One call over one batch. Every failure returns `parsed=False`."""
        prompt = self.prompt(SELECT_PROMPT)
        known = [summary.video_id for summary in batch]
        try:
            reply = self.client.complete(
                "You rank notes by how little they would contribute, so another "
                "model can read the rest in full.",
                prompt.render(
                    question=question,
                    drop=str(drop),
                    keep=str(len(batch) - drop),
                    count=str(len(batch)),
                    summaries="\n".join(summary.line for summary in batch),
                ),
                max_tokens=max_tokens or SELECT_MAX_TOKENS,
                task="select_notes",
            )
        except LlmConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed batch keeps its notes
            log.warning("select: a batch failed (%s)", exc)
            return Selection(
                mode=SELECTION_SELECTED, considered=len(known),
                parsed=False, problem=f"call failed: {exc}",
            )

        if reply.finish_reason == "length":
            log.warning("select: a batch reply was truncated")
            return Selection(
                mode=SELECTION_SELECTED, considered=len(known), parsed=False,
                problem="reply hit the token ceiling before finishing",
                input_tokens=reply.usage.input_tokens,
                output_tokens=reply.usage.output_tokens,
            )

        parsed = parse_selection(reply.text, known=known, most=drop)
        return replace(
            parsed,
            input_tokens=reply.usage.input_tokens,
            output_tokens=reply.usage.output_tokens,
        )
