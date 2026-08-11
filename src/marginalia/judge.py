"""Turning model output into verdicts.

Separate from ``llm.py`` (which only knows how to make a call) and from
``discover.py`` (which only knows the loop). This is where free text becomes a
decision, and it is the part most likely to break: the model is temporary, the
prompts change, and nothing here may assume either behaves.

**The parsing rule that everything else follows from.** A candidate whose
verdict cannot be read becomes MAYBE — never NO. The two mistakes are not
symmetric:

* keeping something useless costs one download, and it is visible
* rejecting something valuable costs a video nobody will ever know existed

A parser bug that resolves to NO silently deletes videos from a library that
was never built. A parser bug that resolves to MAYBE costs one extra request
and shows up in the logs. Only one of those is recoverable.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Sequence

from .ledger import VERDICT_MAYBE, VERDICT_NO, VERDICT_YES
from .llm import LlmClient, Prompt, Reply, batched, load_prompt
from .providers.base import SearchResult

log = logging.getLogger(__name__)

__all__ = [
    "Brief",
    "MetadataVerdict",
    "OpeningVerdict",
    "Judge",
    "parse_batch",
    "parse_opening",
    "parse_queries",
]

# Words the judge is told to use. Anything else on a line is not a verdict.
_VERDICT_WORDS = {"YES": VERDICT_YES, "NO": VERDICT_NO, "MAYBE": VERDICT_MAYBE}

# `1: YES - reason`, and the variations models reach for anyway: `1. YES`,
# `[1] YES`, `#1 - YES`, `1) **YES**`, `1 — MAYBE`. Keyed on the number, so
# order of appearance never matters.
_BATCH_LINE_RE = re.compile(
    r"""^\s*\**\s*
        [\[#(]?\s*(?P<number>\d{1,3})\s*[\]).:\-–—]*\s*
        (?:\*{0,2})\s*
        (?P<verdict>YES|NO|MAYBE)\b
        (?:\*{0,2})\s*
        (?P<reason>.*)$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_LABEL_RE = re.compile(r"^\s*\**\s*(VERDICT|REASON|CONCEPTS)\s*\**\s*[:\-]\s*(.*)$", re.IGNORECASE)

# Leading list markers a model adds to query lines despite being told not to.
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s*")

_MAX_REASON = 120

_OPENING_WORDS = 1000


@dataclass(frozen=True)
class Brief:
    """The explicit statement of what is wanted, and the prompts behind it."""

    topic: str
    text: str
    version: str

    @property
    def intent(self) -> str:
        """The INTENT section on one line, for the report."""
        lines = self.text.splitlines()
        for index, line in enumerate(lines):
            if line.strip().upper().startswith("INTENT"):
                body = line.partition(":")[2].strip()
                if body:
                    return " ".join(body.split())
                for follower in lines[index + 1 :]:
                    if follower.strip():
                        return " ".join(follower.split())
        first = next((line for line in lines if line.strip()), "")
        return " ".join(first.split())


@dataclass(frozen=True)
class MetadataVerdict:
    video_id: str
    verdict: str
    reason: str
    parsed: bool = True


@dataclass(frozen=True)
class OpeningVerdict:
    video_id: str
    verdict: str
    reason: str
    concepts: list[str] = field(default_factory=list)
    parsed: bool = True


# -- parsing ----------------------------------------------------------------


def parse_batch(text: str, count: int) -> list[tuple[str, str, bool]]:
    """Read a batched stage-1 reply into ``count`` (verdict, reason, parsed).

    Survives, by construction: entries missing, entries invented, entries out
    of order, prose instead of verdicts, markdown decoration, and a flat
    refusal. Position is taken from the number the model wrote, never from the
    order lines appear in, because a model that reorders its output would
    otherwise silently attach every verdict to the wrong video.

    Anything not confidently read comes back MAYBE.
    """
    found: dict[int, tuple[str, str]] = {}
    extra: list[int] = []

    for line in text.splitlines():
        match = _BATCH_LINE_RE.match(line)
        if match is None:
            continue
        number = int(match.group("number"))
        verdict = _VERDICT_WORDS[match.group("verdict").upper()]
        reason = _tidy_reason(match.group("reason"))
        if not 1 <= number <= count:
            extra.append(number)
            continue
        # First mention wins: a model that restates a verdict in a summary
        # must not be able to overturn its own answer.
        found.setdefault(number, (verdict, reason))

    if extra:
        log.warning("judge: reply named %d candidate(s) outside 1..%d: %s", len(extra), count, extra)

    results: list[tuple[str, str, bool]] = []
    missing: list[int] = []
    for position in range(1, count + 1):
        if position in found:
            verdict, reason = found[position]
            results.append((verdict, reason or "no reason given", True))
        else:
            missing.append(position)
            results.append((VERDICT_MAYBE, "verdict could not be parsed", False))

    if missing:
        log.warning(
            "judge: %d of %d verdicts unparseable (positions %s) — kept as MAYBE, never NO",
            len(missing),
            count,
            missing,
        )
    return results


def parse_opening(text: str) -> tuple[str, str, list[str], bool]:
    """Read a stage-2 reply into ``(verdict, reason, concepts, parsed)``.

    An unreadable reply keeps the video. There is no third stage to route a
    MAYBE to here, so the inclusion bias resolves the other way: a parser bug
    must not be able to reject.
    """
    verdict: str | None = None
    reason = ""
    concepts: list[str] = []

    for line in text.splitlines():
        match = _LABEL_RE.match(line)
        if match is None:
            continue
        label, body = match.group(1).upper(), match.group(2).strip()
        if label == "VERDICT" and verdict is None:
            word = re.search(r"\b(YES|NO|MAYBE)\b", body, re.IGNORECASE)
            if word is not None:
                found = _VERDICT_WORDS[word.group(1).upper()]
                # There is no stage 3. A MAYBE here has nowhere to go, so it
                # resolves the way the asymmetry points: keep it.
                verdict = VERDICT_YES if found == VERDICT_MAYBE else found
        elif label == "REASON" and not reason:
            reason = _tidy_reason(body)
        elif label == "CONCEPTS" and not concepts:
            concepts = _parse_concepts(body)

    if verdict is None:
        log.warning("judge: stage-2 verdict unparseable — keeping the video, never rejecting")
        return VERDICT_YES, "verdict could not be parsed, kept", concepts, False

    return verdict, reason or "no reason given", concepts, True


def parse_queries(text: str, limit: int) -> list[str]:
    """Read expanded search queries, one per line, tolerating list markers."""
    queries: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        cleaned = _LIST_MARKER_RE.sub("", line).strip().strip("\"'").strip()
        if not cleaned or len(cleaned.split()) > 12:
            continue
        # A model that adds a heading writes "Queries:" on its own line.
        if cleaned.endswith(":") or _LABEL_RE.match(cleaned):
            continue
        key = " ".join(cleaned.lower().split())
        if key in seen:
            continue
        seen.add(key)
        queries.append(cleaned)
        if len(queries) >= limit:
            break
    return queries


def _parse_concepts(body: str) -> list[str]:
    if not body or body.strip().upper() in {"NONE", "N/A", "-"}:
        return []
    concepts: list[str] = []
    seen: set[str] = set()
    for piece in re.split(r"[,;]", body):
        cleaned = " ".join(piece.strip().strip("\"'.*").split())
        if not cleaned or len(cleaned.split()) > 6:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        concepts.append(cleaned)
    return concepts[:6]


def _tidy_reason(raw: str) -> str:
    cleaned = " ".join(raw.split()).lstrip("-–—:*").strip().strip("*")
    if len(cleaned) > _MAX_REASON:
        cleaned = cleaned[: _MAX_REASON - 1].rstrip() + "…"
    return cleaned


# -- the judge ---------------------------------------------------------------


class Judge:
    """Owns the prompts and the model client. Knows nothing about the loop."""

    def __init__(self, client: LlmClient, *, prompts_directory=None) -> None:
        self.client = client
        self._directory = prompts_directory
        self._cache: dict[str, Prompt] = {}

    def prompt(self, name: str) -> Prompt:
        if name not in self._cache:
            self._cache[name] = load_prompt(name, directory=self._directory)
        return self._cache[name]

    # -- the brief ---------------------------------------------------------

    def write_brief(self, topic: str) -> Brief:
        """Two calls: write it, then close its gaps.

        The reviewer returns a revised brief rather than a critique, because a
        critique is a brief that never gets improved.
        """
        first = self.prompt("write_brief")
        drafted = self.client.complete(
            "You write precise research briefs.",
            first.render(topic=topic),
            task="write_brief",
        )

        second = self.prompt("review_brief")
        revised = self.client.complete(
            "You improve research briefs and return the improved brief itself.",
            second.render(topic=topic, brief=drafted.text),
            task="review_brief",
        )

        # A reviewer that returns a sentence has answered the wrong question.
        # The draft is a real brief; a critique of it is not.
        usable = len(revised.text) > 80
        if not usable:
            log.warning("judge: brief review returned too little; keeping the original draft")
        return Brief(
            topic=topic,
            text=revised.text if usable else drafted.text,
            version=f"{first.version}/{second.version}",
        )

    def expand_queries(
        self, brief: Brief, *, count: int = 9, concepts: Sequence[str] = ()
    ) -> list[str]:
        prompt = self.prompt("expand_queries")
        extra = ""
        if concepts:
            # Round two onward. These came from stage 2 reading real openings,
            # so they are the loop's own findings, not a rephrasing of the topic.
            extra = (
                "These concepts were mentioned but left unexplained by videos already "
                "judged relevant. Aim several queries at them:\n"
                + "\n".join(f"- {concept}" for concept in concepts)
            )
        reply = self.client.complete(
            "You write search queries for a video search engine.",
            prompt.render(brief=brief.text, count=count, extra=extra),
            task="expand_queries",
        )
        queries = parse_queries(reply.text, count)
        if not queries:
            log.warning("judge: query expansion returned nothing parseable; falling back to topic")
            return [brief.topic]
        return queries

    # -- stage 1: metadata, batched ---------------------------------------

    def judge_metadata(
        self, brief: Brief, candidates: Sequence[SearchResult], *, batch_size: int
    ) -> list[MetadataVerdict]:
        verdicts: list[MetadataVerdict] = []
        for chunk in batched(list(candidates), batch_size):
            verdicts.extend(self._judge_chunk(brief, chunk))
        return verdicts

    def _judge_chunk(
        self, brief: Brief, chunk: Sequence[SearchResult]
    ) -> list[MetadataVerdict]:
        prompt = self.prompt("judge_metadata")
        listing = "\n".join(
            _describe(position, candidate) for position, candidate in enumerate(chunk, start=1)
        )
        try:
            reply: Reply = self.client.complete(
                "You judge video relevance from metadata.",
                prompt.render(brief=brief.text, candidates=listing),
                task="judge_metadata",
            )
        except Exception as exc:  # noqa: BLE001 - a failed batch must not reject
            # The asymmetry again: a batch that errored has judged nothing, and
            # treating "no answer" as NO would delete videos on a network blip.
            log.warning("judge: stage-1 batch failed (%s) — all %d kept as MAYBE", exc, len(chunk))
            return [
                MetadataVerdict(c.video_id, VERDICT_MAYBE, "batch call failed", parsed=False)
                for c in chunk
            ]

        parsed = parse_batch(reply.text, len(chunk))
        return [
            MetadataVerdict(candidate.video_id, verdict, reason, was_parsed)
            for candidate, (verdict, reason, was_parsed) in zip(chunk, parsed)
        ]

    # -- stage 2: the opening, one per call --------------------------------

    def judge_opening(
        self,
        brief: Brief,
        candidate: SearchResult,
        *,
        opening: str,
        chapters: Sequence[str] = (),
    ) -> OpeningVerdict:
        prompt = self.prompt("judge_opening")
        chapter_text = (
            "CHAPTERS: " + "; ".join(chapters) if chapters else "CHAPTERS: none published"
        )
        try:
            reply = self.client.complete(
                "You judge video relevance from how the video opens.",
                prompt.render(
                    brief=brief.text,
                    title=candidate.title,
                    channel=candidate.channel,
                    chapters=chapter_text,
                    opening=opening,
                ),
                task="judge_opening",
            )
        except Exception as exc:  # noqa: BLE001 - see above
            log.warning("judge: stage-2 call failed for %s (%s) — keeping", candidate.video_id, exc)
            return OpeningVerdict(
                candidate.video_id, VERDICT_YES, "opening judge failed, kept", parsed=False
            )

        verdict, reason, concepts, parsed = parse_opening(reply.text)
        return OpeningVerdict(candidate.video_id, verdict, reason, concepts, parsed)


def _describe(position: int, candidate: SearchResult) -> str:
    """One candidate, as the judge sees it. Metadata only — never transcript."""
    parts = [f"{position}. {candidate.title or '(untitled)'}"]
    parts.append(f"   channel: {candidate.channel or 'unknown'}")
    if candidate.duration_s:
        # Seconds below a minute, because integer minutes render a 59-second
        # short as "0 min" — and a judge reading that writes "zero duration"
        # in its reason and rejects on it. Observed, not hypothetical.
        duration = (
            f"{candidate.duration_s} sec"
            if candidate.duration_s < 60
            else f"{candidate.duration_s // 60} min"
        )
        parts.append(f"   duration: {duration}")
    if candidate.published_at:
        parts.append(f"   published: {candidate.published_at[:10]}")
    description = " ".join((candidate.description or "").split())
    if description:
        parts.append(f"   description: {description[:400]}")
    return "\n".join(parts)


def opening_words(transcript_lines: Sequence[str], limit: int = _OPENING_WORDS) -> str:
    """The first ~1000 words of caption text, for stage 2.

    Takes already-extracted text, not the stored file: this must never become a
    path that reads a transcript into a report.
    """
    words: list[str] = []
    for line in transcript_lines:
        words.extend(line.split())
        if len(words) >= limit:
            break
    return " ".join(words[:limit])
