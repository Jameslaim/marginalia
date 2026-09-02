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
from typing import Sequence

from ..contracts import (
    SELECTION_SELECTED,
    NoteSummary,
    Selection,
)
from ..llm import LlmClient, LlmConfigError, Prompt, load_prompt

log = logging.getLogger(__name__)

__all__ = ["SELECT_PROMPT", "Selector", "parse_selection"]

SELECT_PROMPT = "select_notes"

_DROPPED_RE = re.compile(r"^\s*\**\s*DROPPED\s*\**\s*:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
_WHY_RE = re.compile(r"^\s*\**\s*WHY\s*\**\s*:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")

_MAX_WHY = 300

# A list of ids needs nowhere near this. It is a ceiling on a runaway, not a
# budget for an answer: v1 of this prompt looped and spent 32,768 output tokens
# repeating ids, and the cost of that is what this number bounds.
SELECT_MAX_TOKENS = 4096


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
        """One call, returning the notes to keep. Failures never narrow.

        `keep` is how many notes fit the budget, so exactly `len(summaries) -
        keep` have to go and that number is what the reply is asked for. A quota
        rather than a ceiling, and the difference is measured: asked for *at
        most* 318 drops when 159 were needed, the model took the whole
        allowance, under-filled the budget by 60%, and produced a list long
        enough to truncate — which is what sent two of four questions down the
        fallback path.

        Failures return `parsed=False` rather than raising. The caller retries
        once and then refuses; it no longer fills the budget by position, for
        the reason recorded in PLAN.md.

        A refusal re-raises for rule 24's reason: every later call fails
        identically, so continuing would spend the answering call against a
        provider that has already said no.
        """
        prompt = self.prompt(SELECT_PROMPT)
        known = [summary.video_id for summary in summaries]
        drop = max(0, len(summaries) - keep)
        try:
            reply = self.client.complete(
                "You rank notes by how little they would contribute, so another "
                "model can read the rest in full.",
                prompt.render(
                    question=question,
                    drop=str(drop),
                    keep=str(keep),
                    count=str(len(summaries)),
                    summaries="\n".join(summary.line for summary in summaries),
                ),
                max_tokens=max_tokens or SELECT_MAX_TOKENS,
                task="select_notes",
            )
        except LlmConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed selection must not narrow
            log.warning("select: the selection call failed (%s)", exc)
            return Selection(
                mode=SELECTION_SELECTED,
                considered=len(known),
                parsed=False,
                problem=f"call failed: {exc}",
            )

        if reply.finish_reason == "length":
            # A fragment of a drop list is safe to *apply* — it drops less than
            # it meant to — but it is the signature of a reply that ran away,
            # and the quota exists partly to stop producing them.
            log.warning("select: the reply was truncated")
            return Selection(
                mode=SELECTION_SELECTED,
                considered=len(known),
                parsed=False,
                problem="reply hit the token ceiling before finishing",
                input_tokens=reply.usage.input_tokens,
                output_tokens=reply.usage.output_tokens,
            )

        parsed = parse_selection(reply.text, known=known, most=drop)
        return Selection(
            video_ids=parsed.video_ids,
            mode=parsed.mode,
            reason=parsed.reason,
            considered=parsed.considered,
            requested_drops=parsed.requested_drops,
            allowed_drops=parsed.allowed_drops,
            refused_drops=parsed.refused_drops,
            parsed=parsed.parsed,
            problem=parsed.problem,
            input_tokens=reply.usage.input_tokens,
            output_tokens=reply.usage.output_tokens,
        )
