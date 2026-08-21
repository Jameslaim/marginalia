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
from typing import Sequence

from ..contracts import (
    EVIDENCE_CLEAR,
    EVIDENCE_DEGRADED,
    RELEVANT_UNAVAILABLE,
    VERDICT_MAYBE,
    VERDICT_NO,
    VERDICT_YES,
    Brief,
    FullVerdict,
    MetadataVerdict,
    OpeningVerdict,
    SearchResult,
    TerritoryMap,
)
from ..llm import LlmClient, Prompt, Reply, batched, load_prompt

log = logging.getLogger(__name__)

__all__ = [
    "FULL_PROMPT",
    "Judge",
    "parse_batch",
    "parse_full",
    "parse_opening",
    "parse_queries",
    "territory_lines",
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

# Stage 2 gets more room than stage 1. The prompt asks for one specific
# sentence naming what the transcript established, and 120 characters cannot
# hold one — the reason is the only durable account of why a video is or is
# not in the library, read months later from the decision log.
_FULL_REASON = 400

_OPENING_WORDS = 1000

# Chapters shown per candidate at stage 1. The same cap stage 2 uses.
_CHAPTER_LIMIT = 12


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


# -- stage 2, whole transcript ----------------------------------------------

_FULL_LABEL_RE = re.compile(
    r"^\s*\**\s*(VERDICT|EVIDENCE|REASON|RELEVANT|UNEXPLAINED)\s*\**\s*[:\-]\s*(.*)$",
    re.IGNORECASE,
)

# `00:04:20-00:09:15`, and the shapes a model reaches for anyway: `4:20-9:15`,
# an en dash, spaces around the dash, a trailing comma instead of a semicolon.
_RANGE_RE = re.compile(
    r"(\d{1,2}:\d{2}(?::\d{2})?)\s*[-–—]\s*(\d{1,2}:\d{2}(?::\d{2})?)"
)

_FULL_LINES = ("VERDICT", "EVIDENCE", "REASON", "RELEVANT", "UNEXPLAINED")

# Words that say the evidence was *broken*, as opposed to merely mentioning it.
#
# The distinction is the whole rule and it took a real failure to find. The
# first version of this list included "transcript", which fires on almost every
# honest rejection — "the transcript only describes X conceptually" is a NO
# about content that happens to name its source. What matters is not whether
# the reason mentions the transcript but whether it says the transcript could
# not be read.
_DAMAGE_WORDS = (
    "garbled",
    "unintelligible",
    "illegible",
    "unreadable",
    "corrupt",
    "truncated",
    "gibberish",
    "nonsens",
    "incomprehensib",
    "mistranscri",
    "machine transl",
    "machine-transl",
    "auto-transl",
    "auto transl",
    "quality",
    "noise",
    "not legible",
    "could not be read",
    "cannot be read",
    "could not read",
    "no usable",
    "empty",
    "blank",
)

_MAX_UNEXPLAINED = 5

# In a subdirectory because judging is about to grow more prompts, and a flat
# `prompts/` where `judge_full.md` and `write_note.md` sit side by side stops
# saying which belong to the same decision.
FULL_PROMPT = "judge/full"


def _blames_damaged_evidence(reason: str) -> bool:
    """Does this reason say the evidence could not be read?

    A **proxy**. "DEGRADED is only for evidence quality, never ordinary
    uncertainty about relevance" is a rule about intent, and intent is not
    mechanically checkable from five lines. What is checkable is whether the
    reason claims damage — and the prompt requires it to, in as many words.

    Used in both directions, which is the point:

    * ``DEGRADED`` without a damage claim is DEGRADED standing in for "I am not
      sure", which is a judgement declining to be made.
    * ``NO`` **with** a damage claim is a rejection resting on evidence the
      judge has just said it could not read. That is the same violation as
      NO + DEGRADED, arriving through a mislabelled EVIDENCE line instead of an
      honest one — and it is the shape a real model produced: *"the transcript
      is garbled and contains no substantive content"*, verdict NO, evidence
      CLEAR. Catching only the honest version would enforce nothing, because
      the honest version is not the one that costs a video.

    A false positive costs a log line, a counter increment and one kept video.
    A false negative costs a video permanently, on evidence nobody could read.
    The proxy is tuned toward the first.
    """
    lowered = reason.lower()
    return any(word in lowered for word in _DAMAGE_WORDS)


def parse_full(text: str, video_id: str) -> FullVerdict:
    """Read a stage-2 reply into a :class:`FullVerdict`, enforcing the contract.

    The contract is five lines, and the rules below are guarantees rather than
    requests — a reply that breaks one is a **parse failure**: logged, counted,
    surfaced. It is not silently accepted, because a broken output format that
    nobody notices is how `/ask` came to report 2 citations out of 104.

    The rules, and why each is a rule:

    ``VERDICT: NO`` must carry ``EVIDENCE: CLEAR``
        You cannot confidently reject evidence you could not read. A NO on
        degraded evidence is a claim about captions wearing the costume of a
        claim about content — and NO is permanent, because Gate 2 treats every
        past NO as final.

    ``EVIDENCE: DEGRADED`` must blame the evidence
        Ordinary uncertainty about a readable video is a judgement stage 2 is
        equipped to make, and its answer is YES. Checked by proxy; see above.

    ``CLEAR`` + ``YES`` must name where the value is
        Keeping a video while pointing at nothing in it is the shape of a
        judgement that did not happen. ``UNAVAILABLE`` is a real answer here —
        that is what it is for — but ``NONE`` is not.

    Exactly the five lines, nothing else
        No preamble, no reasoning, no trailing commentary.

    **A violation never rejects.** Every resolution points at keeping, for the
    reason every parser here points that way: a parser bug that resolves to NO
    deletes videos from a library nobody will ever know were missing.
    """
    found: dict[str, str] = {}
    extra_lines = 0

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _FULL_LABEL_RE.match(line)
        if match is None:
            extra_lines += 1
            continue
        label = match.group(1).upper()
        # First wins. A model that restates a label has changed its mind out
        # loud, and the first answer is the one the format asked for.
        found.setdefault(label, match.group(2).strip())

    problems: list[str] = []

    missing = [label for label in _FULL_LINES if label not in found]
    if missing:
        problems.append(f"missing {'/'.join(missing)}")
    if extra_lines:
        problems.append(f"{extra_lines} line(s) outside the format")

    word = re.search(r"\b(YES|NO|MAYBE)\b", found.get("VERDICT", ""), re.IGNORECASE)
    if word is None:
        verdict = VERDICT_YES
        problems.append("no readable verdict")
    else:
        spoken = _VERDICT_WORDS[word.group(1).upper()]
        # There is no stage 3. A MAYBE has nowhere to go, so it resolves the
        # way the asymmetry points.
        verdict = VERDICT_YES if spoken == VERDICT_MAYBE else spoken

    evidence = (
        EVIDENCE_DEGRADED
        if re.search(r"\bDEGRADED\b", found.get("EVIDENCE", ""), re.IGNORECASE)
        else EVIDENCE_CLEAR
    )
    if "EVIDENCE" in found and not re.search(
        r"\b(CLEAR|DEGRADED)\b", found["EVIDENCE"], re.IGNORECASE
    ):
        problems.append("evidence is neither CLEAR nor DEGRADED")

    reason = _tidy_reason(found.get("REASON", ""), limit=_FULL_REASON)
    relevant, relevant_problem = _parse_relevant(found.get("RELEVANT", ""))
    if relevant_problem:
        problems.append(relevant_problem)
    concepts = _parse_concepts(found.get("UNEXPLAINED", ""))[:_MAX_UNEXPLAINED]

    # -- the consistency rules ---------------------------------------------

    blames_damage = _blames_damaged_evidence(reason)

    if verdict == VERDICT_NO and (evidence == EVIDENCE_DEGRADED or blames_damage):
        problems.append(
            "rejected on degraded evidence"
            if evidence == EVIDENCE_DEGRADED
            else "rejected while calling the transcript unreadable"
        )
        # Keep, and take the DEGRADED grade from the reason rather than from
        # the EVIDENCE line. The judge has said it could not read the
        # transcript; believing that costs a note, which is cheap and
        # reversible, while the alternative is a permanent rejection made on
        # evidence nobody could see. A damaged transcript is not evidence the
        # content is bad — it is evidence of nothing at all.
        verdict = VERDICT_YES
        evidence = EVIDENCE_DEGRADED

    if evidence == EVIDENCE_DEGRADED and not blames_damage:
        problems.append("degraded without naming an evidence problem")

    if (
        verdict == VERDICT_YES
        and evidence == EVIDENCE_CLEAR
        and not relevant
        and "RELEVANT" in found
    ):
        problems.append("kept on clear evidence but named no relevant range")

    if not problems:
        return FullVerdict(
            video_id=video_id,
            verdict=verdict,
            evidence=evidence,
            reason=reason or "no reason given",
            relevant=relevant,
            concepts=concepts,
            parsed=True,
        )

    problem = "; ".join(problems)
    log.warning("judge: stage-2 reply for %s broke the contract (%s) — kept", video_id, problem)
    log.debug("judge: unparsed stage-2 reply for %s:\n%s", video_id, text)
    return FullVerdict(
        video_id=video_id,
        verdict=verdict,
        evidence=evidence,
        reason=reason or f"reply broke the output contract ({problem})",
        relevant=relevant,
        concepts=concepts,
        parsed=False,
        problem=problem,
    )


def _parse_relevant(body: str) -> tuple[list[str], str]:
    """``(ranges, problem)``. ``UNAVAILABLE`` survives as itself.

    Two absences that must not collapse into one: NONE means nothing worth
    pointing at, UNAVAILABLE means there is and the clock cannot be trusted.
    """
    stripped = body.strip()
    if not stripped:
        return [], ""
    if re.fullmatch(r"\**\s*NONE\s*\**\.?", stripped, re.IGNORECASE):
        return [], ""
    if re.search(r"\bUNAVAILABLE\b", stripped, re.IGNORECASE):
        return [RELEVANT_UNAVAILABLE], ""

    ranges = [f"{start}-{end}" for start, end in _RANGE_RE.findall(stripped)]
    if not ranges:
        return [], "relevant ranges were neither timestamps, NONE nor UNAVAILABLE"
    return ranges, ""


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


def _tidy_reason(raw: str, *, limit: int = _MAX_REASON) -> str:
    cleaned = " ".join(raw.split()).lstrip("-–—:*").strip().strip("*")
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
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
        self,
        brief: Brief,
        candidates: Sequence[SearchResult],
        *,
        batch_size: int,
        territory: TerritoryMap | None = None,
    ) -> list[MetadataVerdict]:
        verdicts: list[MetadataVerdict] = []
        for chunk in batched(list(candidates), batch_size):
            verdicts.extend(self._judge_chunk(brief, chunk, territory))
        return verdicts

    def _judge_chunk(
        self,
        brief: Brief,
        chunk: Sequence[SearchResult],
        territory: TerritoryMap | None = None,
    ) -> list[MetadataVerdict]:
        prompt = self.prompt("judge_metadata")
        listing = "\n".join(
            _describe(position, candidate) for position, candidate in enumerate(chunk, start=1)
        )
        try:
            reply: Reply = self.client.complete(
                "You judge video relevance from metadata.",
                prompt.render(
                    brief=brief.text,
                    candidates=listing,
                    territory=territory_lines(territory),
                ),
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

    # -- stage 2: the whole transcript, one per call -----------------------

    def judge_full(
        self,
        brief: Brief,
        candidate: SearchResult,
        *,
        transcript: str,
        chapters: Sequence[str] = (),
        duration_s: int | None = None,
        territory: TerritoryMap | None = None,
    ) -> FullVerdict:
        """Judge one video on everything it says.

        The transcript arrives as text rather than being read from disk, for the
        same reason `opening_words` takes lines: this must never become a path
        that can read a stored transcript into a report. `brain/` cannot reach
        the filesystem at all, which makes that structural rather than careful.
        """
        prompt = self.prompt(FULL_PROMPT)
        chapter_text = (
            "CHAPTERS: " + "; ".join(chapters) if chapters else "CHAPTERS: none published"
        )
        try:
            reply = self.client.complete(
                "You judge whether one video satisfies a research brief, "
                "having read it in full.",
                prompt.render(
                    brief=brief.text,
                    title=candidate.title,
                    channel=candidate.channel,
                    duration=str(duration_s) if duration_s else "unknown",
                    chapters=chapter_text,
                    territory=territory_lines(territory),
                    transcript=transcript,
                ),
                task="judge_full",
            )
        except Exception as exc:  # noqa: BLE001 - a failed call must not reject
            log.warning("judge: stage-2 call failed for %s (%s) — keeping", candidate.video_id, exc)
            return FullVerdict(
                candidate.video_id,
                VERDICT_YES,
                reason="stage-2 judge failed, kept",
                parsed=False,
                problem="call failed",
            )

        if reply.finish_reason == "length":
            # Not a judgement — a fragment. Parsing it would read whichever of
            # the five lines happened to fit, and a half-read verdict is worse
            # than an unread one because it looks like an answer.
            log.warning(
                "judge: stage-2 reply for %s was truncated (%d output tokens) — keeping",
                candidate.video_id,
                reply.usage.output_tokens,
            )
            return FullVerdict(
                candidate.video_id,
                VERDICT_YES,
                reason="reply hit the token ceiling before finishing, kept",
                parsed=False,
                problem="truncated reply",
            )

        return parse_full(reply.text, candidate.video_id)


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


def territory_lines(territory: TerritoryMap | None) -> str:
    """The map as the judge sees it: a word list, and nothing more.

    **This is the risk B3 identified, made concrete.** That phase measured this
    judge confabulating from suggestive context — three videos kept on reasons
    describing numbers their transcripts do not contain, two of them short
    enough that the judge had read them in full and completed the pattern the
    title implied anyway. A territory map is a list of suggestive context, and
    handing it to that judge is the main hazard of this phase.

    So what crosses is deliberately impoverished. Concepts and vocabulary, flat,
    unlabelled as to which is which, with no queries, no filters and no reasons:
    the judge is being told what words this subject uses, not what the map
    concluded. The rule that a term is never a reason to keep a video is stated
    in the prompts themselves, as a rule with a worked example, because a note
    in a docstring enforces nothing at judging time.

    An absent map renders as a sentence saying so, rather than an empty block.
    ``Prompt.render`` refuses unfilled placeholders, and a blank section reads
    to a model like a subject with no vocabulary rather than a run without a
    map.
    """
    if territory is None or not territory.terms:
        return "TERRITORY: none mapped for this topic."
    return "TERRITORY (words this subject uses): " + ", ".join(territory.terms)


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
    # The creator's own table of contents, and the only structural view of a
    # video that exists before anything is downloaded. After the description
    # because that is the order a reader needs them in: what the video says it
    # is, then how it is laid out.
    #
    # Capped at the same twelve stage 2 uses, so a fifty-chapter workshop
    # cannot crowd the other candidates out of a batch.
    titles = [
        " ".join(str(chapter.get("title", "")).split())
        for chapter in (candidate.chapters or [])
    ]
    shown = [title for title in titles if title][:_CHAPTER_LIMIT]
    if shown:
        parts.append("   chapters: " + " | ".join(shown))
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
