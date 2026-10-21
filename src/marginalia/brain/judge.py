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
    RELEVANT_NONE,
    RELEVANT_UNAVAILABLE,
    OUTCOME_CALL_FAILED,
    OUTCOME_JUDGED,
    OUTCOME_NO_VERDICT,
    OUTCOME_TOO_LARGE,
    OUTCOME_TRUNCATED,
    UNJUDGED_CALL_FAILED,
    UNJUDGED_NO_VERDICT,
    UNJUDGED_TOO_LARGE,
    UNJUDGED_TRUNCATED,
    VERDICT_MAYBE,
    VERDICT_NO,
    VERDICT_YES,
    AimedQuery,
    Brief,
    CoverageItem,
    FullVerdict,
    MetadataVerdict,
    SearchResult,
    TerritoryMap,
)
from ..llm import (
    LlmClient,
    LlmConfigError,
    LlmRequestTooLarge,
    Prompt,
    Reply,
    batched,
    load_prompt,
    safe_reason,
)

log = logging.getLogger(__name__)

__all__ = [
    "BriefUnusable",
    "FULL_PROMPT",
    "METADATA_PROMPT",
    "Judge",
    "guesses",
    "parse_batch",
    "parse_full",
    "parse_queries",
    "parse_aimed_queries",
    "coverage_block",
    "resolve_guess",
    "resolve_stage1_no",
    "rests_on_absence",
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

# A labelled line, which query expansion must not mistake for a query.
_LABEL_RE = re.compile(r"^\s*\**\s*(VERDICT|REASON|CONCEPTS)\s*\**\s*[:\-]\s*(.*)$", re.IGNORECASE)

# Leading list markers a model adds to query lines despite being told not to.
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s*")

_MAX_REASON = 120

# Stage 2 gets more room than stage 1. The prompt asks for one specific
# sentence naming what the transcript established, and 120 characters cannot
# hold one — the reason is the only durable account of why a video is or is
# not in the library, read months later from the decision log.
_FULL_REASON = 400

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
#
# **It over-fired a second time, the other way round.** Bare `quality`, `noise`,
# `empty`, `blank`, `truncated` and `corrupt` describe subjects as readily as
# evidence — "a code quality tutorial", "data corruption recovery", "an empty
# sales pitch", "truncated backpropagation" — and each turned an ordinary content
# rejection into a DEGRADED keeper, which `wanted_for_a_note` then refuses a note
# for every topic. So the words split in two. These are damage on their own:
_DAMAGE_WORDS = (
    "garbled",
    "unintelligible",
    "illegible",
    "unreadable",
    "gibberish",
    "mistranscri",
    "machine transl",
    "machine-transl",
    "auto-transl",
    "auto transl",
    "not legible",
    "could not be read",
    "cannot be read",
    "could not read",
)

# These are damage only when the reason says them *of the evidence*: the
# transcript, the captions, the subtitles, the audio. Predicated of it
# ("the transcript is largely empty"), attached to it ("noisy auto captions",
# "no usable transcript") or as its quality ("transcript quality too poor").
# Merely appearing in a reason that also names the transcript is not enough —
# "the transcript covers data corruption recovery" is a NO about content.
#
# `text` only in a compound that means the transcript. Bare, it is a subject
# too — a Postgres column type, so "empty text values" and "long text is
# truncated at the column limit" are claims about content.
_EVIDENCE_NOUN = (
    r"(?:(?:stored|transcript|caption|subtitle)\s+text"
    r"|transcripts?|transcription|captions?|captioning|subtitles?|audio)"
)
_AMBIGUOUS_DAMAGE = (
    r"(?:empty|blank|truncated|truncation|corrupt\w*|noise|noisy|nonsens\w*"
    r"|incomprehensib\w*|quality|no usable)"
)
_COPULA = r"(?:is|are|was|were|seems?|seemed|appears?|appeared|looks?|looked|becomes?|became|gets?|got)"
# Adverbs only between the verb and the damage word, so "the transcript is about
# truncated backpropagation" stays a statement about subject matter.
_ADVERBS = r"(?:(?:\w+ly|too|so|very|just|almost|all|basically|essentially)\s+){0,2}"
_DAMAGE_OF_EVIDENCE_RE = re.compile(
    # predicated: "the transcript is largely empty", "the text is truncated"
    rf"\b{_EVIDENCE_NOUN}\s+{_COPULA}\s+{_ADVERBS}(?:of\s+)?(?:(?:poor|low|bad)[\s-]+)?"
    rf"{_AMBIGUOUS_DAMAGE}\b"
    # attached: "empty transcript", "noisy auto captions", "no usable transcript"
    rf"|\b{_AMBIGUOUS_DAMAGE}[\s-]+(?:(?:auto(?:matic)?|machine|auto-generated)[\s-]+)?"
    rf"{_EVIDENCE_NOUN}\b"
    # its quality: "transcript quality", "audio quality"
    rf"|\b{_EVIDENCE_NOUN}\s+quality\b",
    re.IGNORECASE,
)

_MAX_UNEXPLAINED = 5
# The same cap on `RELEVANT`, and for a measured reason. It was the one field of
# the five with no bound in the prompt *or* the parser, and on a 458-minute
# course (`mHxLXzYjQRE`, a 734k-character transcript) a verdict ran to 8,192
# output tokens — roughly 640 ranges — and was discarded as truncated. Stage 2
# then kept the video on rule 13's resolve-toward-keeping, so an unbounded list
# cost a real judgement.
#
# Five is what a reader can act on. A list long enough to need scrolling is not
# "where the value is", it is the transcript with different punctuation.
_MAX_RELEVANT = 5

# In a subdirectory because judging is about to grow more prompts, and a flat
# `prompts/` where `judge_full.md` and `write_note.md` sit side by side stops
# saying which belong to the same decision.
FULL_PROMPT = "judge/full"

# Stage 1. Named so the decision row can record which version judged it, as
# stage 2's does: 0 of 494 stage-1 rows over two runs carried a version, so no
# stage-1 verdict could be tied to the prompt that made it.
METADATA_PROMPT = "judge_metadata"


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

    A false positive is not free: on a NO it sets DEGRADED, and DEGRADED denies
    the video a note for every topic. So a word counts only when the reason says
    it of the evidence — see `_DAMAGE_OF_EVIDENCE_RE`.
    """
    lowered = reason.lower()
    return any(word in lowered for word in _DAMAGE_WORDS) or bool(
        _DAMAGE_OF_EVIDENCE_RE.search(reason)
    )


# -- the outside-knowledge marker, checked rather than requested ---------------
#
# Era is the one axis a transcript cannot speak on: no video states that its own
# import path has since been superseded, and none can. PLAN.md §7 allows that
# one exception to "use only the evidence supplied" and requires it to be
# marked, so that `grep '[outside knowledge'` says how often era judgement is
# carrying a verdict and whether it is being used to exclude only.
#
# It was asked for in the prompt and mostly not done: on the second `langchain`
# run, 4 of 50 stage-2 reasons rested on currency and 1 carried the marker.
# Same lesson as rule 13 — a rule living only in a prompt is a rule enforced by
# a model — so the code checks it here.
_CURRENCY_WORDS = (
    "deprecat",
    "supersed",
    "obsolete",
    "outdated",
    "out of date",
    "out-of-date",
    "no longer supported",
    "no longer maintained",
    "no longer the",
    "no longer current",
    "since been replaced",
    "has been replaced by",
    "pre-0.",
    "predates the current",
)

# The false positive this has to avoid, and it is a real reply rather than an
# imagined one. `Zbf5Hyv2DSY` was rejected as *"a conceptual lecture comparing
# legacy chains to LCEL"* — the video's own subject matter, described. A reason
# that reports what the video says about currency is not the judge asserting it,
# and marking it would be marking the transcript.
#
# So attribution suppresses. `legacy` is absent from the list above for the same
# reason: it describes an era far more often than it dates an API, and the trap
# this file already fell into once was `"transcript"` firing on every honest
# rejection that named its evidence.
_ATTRIBUTION_PHRASES = (
    "the speaker",
    "the presenter",
    "the instructor",
    "the narrator",
    "the video says",
    "the video notes",
    "the video calls",
    "the video acknowledges",
    "the transcript says",
    "the transcript notes",
    "the author says",
    "acknowledges",
    "admits",
    "warns that",
    "says it is",
    "calls it",
    "describes it as",
)

_MARKER = "[outside knowledge"


def _asserts_currency_unmarked(reason: str) -> bool:
    """Does this reason date an API without saying that is outside knowledge?

    A **proxy**, like `_blames_damaged_evidence`, and tuned the same way — but
    toward the opposite side, because the costs are different. This one changes
    no verdict and keeps no video: it moves a counter and writes a log line. So
    a false positive costs a slightly overstated rate, a false negative costs an
    unmarked claim going unmeasured, and neither costs a video. What it must not
    do is fire on the honest shape, because a counter that fires on everything
    measures nothing.
    """
    lowered = reason.lower()
    if _MARKER in lowered:
        return False
    if not any(word in lowered for word in _CURRENCY_WORDS):
        return False
    return not any(phrase in lowered for phrase in _ATTRIBUTION_PHRASES)


# A stage-1 reason that guesses at what the video contains. Stage 1 sees a
# title, a channel, a description and a chapter list, so a NO it can state only
# as a likelihood is uncertainty, and rule 8 says uncertainty is MAYBE.
#
# **Hedges only, and on purpose.** `seems`, `appears` and `suggests` are the
# ordinary register of reading metadata — "the title suggests a product promo"
# names what the metadata shows — and would turn honest rejections into
# fetches. `may` counts only before a word it hedges, so "May 2024" is a date.
# Over-firing is the cheap direction here: a guessed NO kept as MAYBE costs one
# fetch and a stage-2 read, and stage 2 then decides.
_GUESS_RE = re.compile(
    r"\b(?:likely|unlikely|probably|presumably|possibly|perhaps"
    r"|might|may\s+(?:be|not|have|only|just|well)|could\s+be)\b",
    re.IGNORECASE,
)


def guesses(reason: str) -> bool:
    """Does this stage-1 reason rest on a guess about the video's content?"""
    return _GUESS_RE.search(reason) is not None


def resolve_guess(verdict: str, reason: str) -> tuple[str, str | None]:
    """A NO that guesses becomes MAYBE. Returns ``(verdict, said)``.

    ``said`` is the judge's own word when it was overruled, ``None`` otherwise.
    Only a NO is resolved: a YES or a MAYBE that hedges has already sent the
    video to be read, which is what a hedge asks for. Stage 2 is not resolved
    this way — it read the transcript, and its error that hides is a wrong YES
    (rule 8), so a hedge there is not a reason to keep.
    """
    verdict, said, _ = resolve_stage1_no(verdict, reason)
    return verdict, said


# A stage-1 reason that rests on what the metadata did NOT show — "no evidence
# of a mechanism", "no explanation indicated", "too thin to explain" — rather
# than on anything it did. Checkpoint 4 measured about 10 of 51 NOs in this
# shape, nearly all on short clips; of the short clips stage 2 went on to read,
# 2 of 7 were kept. Metadata failing to show a video is on the brief is
# uncertainty, and rule 8 makes uncertainty MAYBE.
_ABSENT_RE = re.compile(
    r"\b(?:no|without|lacks?|lacking)\b[^,;.]{0,40}?"
    r"\b(?:evidence|evident|indicat(?:ed|ion|es)|visible|shown|apparent)\b"
    r"|\btoo\s+(?:short|thin|brief)\s+to\b"
    r"|\bnothing\s+(?:in\s+the\s+(?:title|metadata|description)\s+)?(?:shows|indicates|suggests)\b",
    re.IGNORECASE,
)
# The other shape of the same thing: the only description is a length or a
# format, followed by what is missing — "59-second short, no mechanism", "one-
# minute clip, no named source". A clip's length is not evidence about its
# content (the prompt's DURATION IS NOT DEPTH), so nothing was shown.
_LENGTH_ONLY_RE = re.compile(
    r"^\s*(?:an?\s+)?(?:(?:\d+|one|two|three|a)[- ]?(?:second|sec|s|minute|min)s?"
    r"|short(?:-form)?|brief)(?:[- ](?:long|language))?\s+(?:[\w-]+\s+){0,2}?"
    r"(?:clip|short|video|reel)\s*,\s*no\b",
    re.IGNORECASE,
)
# What the metadata *did* show that puts a video off the brief. A reason naming
# one of these keeps its NO even when it also says something is missing: "a
# meme clip, unrelated" and "product promo, no explanation" rest on what the
# title and description show. General categories only — none names a subject.
_SHOWN_OFF_BRIEF_RE = re.compile(
    r"\b(?:unrelated|different\s+(?:subject|topic|field)|off[- ]topic|not\s+about|promo\w*"
    r"|advert\w*|sponsor\w*|vendor|product\s+(?:launch|feature|demo)|launch|news|meme"
    r"|prank|reaction|opinion|excluded|excludes|giveaway|trailer|music|gaming"
    r"|entertainment|motivational|scam)\b",
    re.IGNORECASE,
)

RESOLVED_GUESS = "guess"
RESOLVED_UNSHOWN = "unshown"


def rests_on_absence(reason: str) -> bool:
    """Does this stage-1 NO rest only on what the metadata did not show?

    False when the reason also names what the metadata did show that is off the
    brief. Tuned on every stage-1 NO recorded across checkpoints 3 and 4 (522
    reasons): it fires on 31, and on none that names a different subject, a
    promotion, news, a meme or an EXCLUDED item. Over-firing is the cheap
    direction, as for `guesses`: the video is fetched and stage 2 decides.
    """
    if _SHOWN_OFF_BRIEF_RE.search(reason):
        return False
    return bool(_ABSENT_RE.search(reason) or _LENGTH_ONLY_RE.search(reason))


def resolve_stage1_no(verdict: str, reason: str) -> tuple[str, str | None, str]:
    """A stage-1 NO that guesses, or rests on absence, becomes MAYBE.

    Returns ``(verdict, said, resolved)``: ``said`` is the judge's own word
    when it was overruled, and ``resolved`` says on which ground — `guess` or
    `unshown` — so the two are counted apart (rule 14). Toward keeping only.
    """
    if verdict != VERDICT_NO:
        return verdict, None, ""
    if guesses(reason):
        return VERDICT_MAYBE, VERDICT_NO, RESOLVED_GUESS
    if rests_on_absence(reason):
        return VERDICT_MAYBE, VERDICT_NO, RESOLVED_UNSHOWN
    return verdict, None, ""


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
    said = _VERDICT_WORDS[word.group(1).upper()] if word is not None else None
    if word is None:
        verdict = VERDICT_MAYBE
        problems.append("no readable verdict")
    else:
        verdict = said
        if said == VERDICT_MAYBE:
            # The format allows YES or NO, so a MAYBE is the reply breaking it
            # and is counted in `unparsed` (rule 14). It is **kept as MAYBE**:
            # stored, noted, and not coverage.
            #
            # It used to be resolved to YES, on the reasoning that "there is no
            # stage 3, so a MAYBE has nowhere to go". A kept-but-not-coverage
            # state now exists for exactly this, and a YES is the one stage-2
            # error rule 8 says hides — the planner counted an unsure reading as
            # a satisfied part of the brief and could stop on it. Keeping the
            # video never needed the YES; claiming coverage did.
            problems.append("VERDICT MAYBE is not allowed at stage 2; kept as MAYBE")

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
    relevant_text = _relevant_text(found.get("RELEVANT"), relevant, relevant_problem)
    concepts = _parse_concepts(found.get("UNEXPLAINED", ""))[:_MAX_UNEXPLAINED]

    # -- the consistency rules ---------------------------------------------

    blames_damage = _blames_damaged_evidence(reason)
    # Checked here and deliberately kept out of `problems`. The reply is well
    # formed; what it broke is a convention about *attributing* a claim, and
    # counting that as a parse failure would report a marker nobody wrote as a
    # format nobody could read. Rule 14: three meanings never share a bucket.
    unmarked_currency = _asserts_currency_unmarked(reason)
    if unmarked_currency:
        log.warning(
            "judge: stage-2 reason for %s dates an API without the "
            "[outside knowledge: ...] marker — %s",
            video_id,
            reason,
        )

    # Read before the NO rule below rewrites `evidence`: whether the judge's own
    # DEGRADED label is backed by its own reason.
    unjustified_degraded = evidence == EVIDENCE_DEGRADED and not blames_damage

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
        #
        # **MAYBE, not YES.** Nobody read the content, so nothing can be said
        # about its relevance either way: not a rejection (Gate 2 must not
        # stop the topic), and not an acceptance. It was stored as `yes`, so
        # the ledger said the judge accepted a video it had refused; `said`
        # now keeps the judge's own word beside the resolution.
        verdict = VERDICT_MAYBE
        evidence = EVIDENCE_DEGRADED

    if unjustified_degraded:
        problems.append("degraded without naming an evidence problem")
        # **And resolved to CLEAR, toward keeping the video in the library.**
        # DEGRADED is the one label that takes a video out: `wanted_for_a_note`
        # refuses it a note for every topic, permanently. A DEGRADED whose reason
        # names no damage is the judge declining to decide, not a finding about
        # the text — so honouring it would let a format violation remove a
        # video, which rule 13 says no resolution may do. The damage-word fix
        # closed this path for a NO whose *reason* over-fired; this closes it
        # for a label the reason does not support.
        evidence = EVIDENCE_CLEAR

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
            said=said,
            unmarked_currency=unmarked_currency,
            relevant_text=relevant_text,
        )

    problem = "; ".join(problems)
    log.warning("judge: stage-2 reply for %s broke the contract (%s) — kept", video_id, problem)
    # The reply's shape, never the reply. It was logged whole here, and the
    # replies that land on this line are the ones that broke the five-line
    # contract — the likeliest to have echoed the transcript they were given.
    # Rule 1 holds `logs/` to the same line as stdout.
    log.debug(
        "judge: unparsed stage-2 reply for %s: %d line(s), %d character(s), labels %s",
        video_id,
        len(text.splitlines()),
        len(text),
        sorted(found) or "none",
    )
    if word is None:
        # No verdict at all is no judgement at all — the same case as a call
        # that failed, and recorded the same way. Every other contract break
        # above still carries a verdict the judge actually gave.
        return FullVerdict(
            video_id=video_id,
            verdict=VERDICT_MAYBE,
            evidence=evidence,
            reason=UNJUDGED_NO_VERDICT,
            relevant=relevant,
            concepts=concepts,
            parsed=False,
            outcome=OUTCOME_NO_VERDICT,
            problem=problem,
            unmarked_currency=unmarked_currency,
            relevant_text=relevant_text,
        )
    return FullVerdict(
        video_id=video_id,
        verdict=verdict,
        evidence=evidence,
        reason=reason or f"reply broke the output contract ({problem})",
        relevant=relevant,
        concepts=concepts,
        parsed=False,
        said=said,
        problem=problem,
        unmarked_currency=unmarked_currency,
        relevant_text=relevant_text,
    )


# `NONE`, as the judge writes it when nothing in the video is worth pointing at.
# One pattern for the parser and for what gets stored, so the two cannot disagree
# about which replies said NONE.
_RELEVANT_NONE_RE = re.compile(r"\**\s*NONE\s*\**\.?", re.IGNORECASE)


def _relevant_text(body: str | None, ranges: list[str], problem: str) -> str | None:
    """The RELEVANT line as stored on the decision row, or ``None``.

    Built from what the parser matched, never from ``body`` itself: the ranges
    are ``start-end`` strings `_RANGE_RE` extracted, so any prose the model wrote
    beside them is dropped here and cannot reach the ledger. ``NONE`` is stored
    only when the line said NONE — an empty list also results from a blank or
    missing line, and storing NONE for those would record a claim nobody made.
    """
    if body is None or problem:
        return None
    if ranges == [RELEVANT_UNAVAILABLE]:
        return RELEVANT_UNAVAILABLE
    if ranges:
        return "; ".join(ranges)
    if _RELEVANT_NONE_RE.fullmatch(body.strip()):
        return RELEVANT_NONE
    return None


def _parse_relevant(body: str) -> tuple[list[str], str]:
    """``(ranges, problem)``. ``UNAVAILABLE`` survives as itself.

    Two absences that must not collapse into one: NONE means nothing worth
    pointing at, UNAVAILABLE means there is and the clock cannot be trusted.
    """
    stripped = body.strip()
    if not stripped:
        return [], ""
    if _RELEVANT_NONE_RE.fullmatch(stripped):
        return [], ""
    if re.search(r"\bUNAVAILABLE\b", stripped, re.IGNORECASE):
        return [RELEVANT_UNAVAILABLE], ""

    ranges = [f"{start}-{end}" for start, end in _RANGE_RE.findall(stripped)]
    if not ranges:
        return [], "relevant ranges were neither timestamps, NONE nor UNAVAILABLE"
    if len(ranges) > _MAX_RELEVANT:
        # Truncated rather than refused: the first ranges are still true, and a
        # long list is an over-eager judge rather than a broken one. Said out
        # loud, because a silent cap is how "we only show five" becomes "there
        # were only five".
        log.info(
            "judge: %d relevant ranges returned; keeping the first %d",
            len(ranges),
            _MAX_RELEVANT,
        )
    return ranges[:_MAX_RELEVANT], ""


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


def parse_aimed_queries(text: str, limit: int) -> list[AimedQuery]:
    """Read ``query | aim`` lines. A line with no ``|`` is a query with no aim,
    kept — a query is still a query — and its empty aim is what gets counted."""
    out: list[AimedQuery] = []
    aims: dict[str, str] = {}
    lines = []
    for line in text.splitlines():
        query, bar, aim = line.partition("|")
        lines.append(query)
        if bar:
            key = " ".join(_LIST_MARKER_RE.sub("", query).strip().strip("\"'").lower().split())
            aims.setdefault(key, " ".join(aim.strip().strip("\"'").split())[:_MAX_AIM])
    for query in parse_queries("\n".join(lines), limit):
        out.append(AimedQuery(query, aims.get(" ".join(query.lower().split()), "")))
    return out


# An aim names a part or a missing thing; this is room for a long one.
_MAX_AIM = 160


def coverage_block(coverage: Sequence[CoverageItem]) -> str:
    """What is already held, for the expansion: each part as held, partly held
    with its missing things, or missing. Says so when nothing was assessed."""
    if not coverage:
        return (
            "COVERAGE SO FAR: nothing has been assessed yet. Spread the queries "
            "across every PART TO COVER."
        )
    lines = ["COVERAGE SO FAR — what the library and this run already hold:"]
    for item in coverage:
        if item.satisfied:
            lines.append(f"- HELD: {item.part}")
        elif item.partly:
            lines.append(
                f"- PARTLY HELD: {item.part} — missing: {'; '.join(item.missing) or 'not named'}"
            )
        else:
            missing = "; ".join(item.missing)
            lines.append(f"- MISSING: {item.part}" + (f" — {missing}" if missing else ""))
    return "\n".join(lines)


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


# The marker, whole, for the one place the cap must not cut. Requires the
# closing bracket: an unterminated marker cannot be preserved intact because
# there is no intact thing to preserve.
_MARKER_RE = re.compile(r"\[outside knowledge:[^\]]*\]", re.IGNORECASE)

# Below this, trimming the prose has stopped leaving a reason worth reading, so
# the marker is no longer worth protecting at its expense.
_MIN_PROSE = 80


def _tidy_reason(raw: str, *, limit: int = _MAX_REASON) -> str:
    """Normalise, then cap — but never cut an `[outside knowledge: ...]` marker.

    **The prose is what gets trimmed; the marker is kept whole.** A truncated
    marker still *counts*, because the detector matches the opening bracket, so
    the rate stays honest either way. What a cut marker loses is the only thing
    it was written to carry: which outside knowledge was used. Observed on a
    real reason — `[outside knowledge: `initialize_agent` and `AgentType` we…`
    — where the claim survived and the audit did not.

    Cutting the prose instead is the right trade because the prose degrades
    gracefully and the marker does not: half a sentence about a transcript is
    still evidence about the video, while half an attribution is a claim that
    something was assumed and no record of what.
    """
    cleaned = " ".join(raw.split()).lstrip("-–—:*").strip().strip("*")
    if len(cleaned) <= limit:
        return cleaned

    marker = _MARKER_RE.search(cleaned)
    if marker is not None:
        prose = (cleaned[: marker.start()] + cleaned[marker.end() :]).strip()
        budget = limit - len(marker.group(0)) - 1
        if budget >= _MIN_PROSE:
            if len(prose) > budget:
                prose = prose[: budget - 1].rstrip() + "…"
            return f"{prose} {marker.group(0)}"
        # The marker alone is most of the budget. Nothing is preserved whole
        # here, so say so rather than silently returning a reason that is all
        # attribution and no claim.
        log.info(
            "judge: an outside-knowledge marker of %d characters does not fit a "
            "%d-character reason; both are truncated",
            len(marker.group(0)),
            limit,
        )
    return cleaned[: limit - 1].rstrip() + "…"


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

    def write_brief(self, topic: str, *, max_tokens: int | None = None) -> Brief:
        """Two calls: write it, then close its gaps.

        The reviewer returns a revised brief rather than a critique, because a
        critique is a brief that never gets improved.

        ``max_tokens`` covers **both** calls. A brief is a long structured
        document — intent, required, parts, also-wanted, excluded, edge cases — and
        so is the revision of one, so the two carry the same risk. Only the
        review has been seen to truncate, but the draft succeeding at 8,192 was
        luck rather than a difference in shape.

        A truncated review is quiet in a way the map's was not: `usable` falls
        back to the draft, so the run continues with an *unreviewed* brief and
        one warning line. Everything judged in that run is then measured against
        a brief that never had its gaps closed. That fallback is now recorded on
        the brief (`review_problem`), and so on the run.

        **The draft is checked before it is reviewed.** It used to be checked
        only after the review had failed, so a draft that reasoned to the token
        ceiling and returned nothing went to the review as if it were a draft,
        and the review wrote a brief from the topic alone. Measured: 2 of 12
        drafts with the current prompt, recorded as ordinary reviewed briefs, no
        warning. A cut, empty or heading-less draft is now drawn again once —
        a fresh sample at the same ceiling — and if that one is unusable too,
        `BriefUnusable` stops the run. That happens before the run row exists,
        so stopping spends nothing but the calls already made, and it is the
        visible mistake; going on would spend search quota and store videos
        judged against a brief nobody wrote from a draft.
        """
        first = self.prompt("write_brief")
        problems: list[str] = []
        drafted: Reply | None = None
        for _ in range(_DRAFT_ATTEMPTS):
            candidate = self.client.complete(
                "You write precise research briefs.",
                first.render(topic=topic),
                task="write_brief",
                max_tokens=max_tokens,
            )
            problem = _draft_problem(candidate)
            if not problem:
                drafted = candidate
                break
            problems.append(problem)
            log.warning(
                "judge: brief draft %d of %d unusable (%s)",
                len(problems), _DRAFT_ATTEMPTS, problem,
            )
        if drafted is None:
            raise BriefUnusable(problems)

        second = self.prompt("review_brief")
        revised = self.client.complete(
            "You improve research briefs and return the improved brief itself.",
            second.render(topic=topic, brief=drafted.text),
            task="review_brief",
            max_tokens=max_tokens,
        )

        # The review replaces the draft only if it is a whole brief. It used to
        # be enough to be over eighty characters, so a review cut off by the
        # token ceiling, or a critique instead of a brief, became the authority
        # every verdict in the run was measured against.
        problem = _brief_problem(revised)
        if problem:
            log.warning("judge: brief review unusable (%s); keeping the draft", problem)
        drafted_problem = _brief_problem(drafted)
        if drafted_problem and problem:
            # Nothing better exists, so the run goes ahead on the draft — but
            # not silently: a brief missing EXCLUDED judges every near miss in.
            log.warning("judge: the draft brief is also incomplete (%s)", drafted_problem)
        return Brief(
            topic=topic,
            text=drafted.text if problem else revised.text,
            version=f"{first.version}/{second.version}",
            draft_attempts=len(problems) + 1,
            draft_problems=tuple(problems),
            review_problem=problem,
        )

    def expand_queries(
        self,
        brief: Brief,
        *,
        count: int = 9,
        concepts: Sequence[str] = (),
        coverage: Sequence[CoverageItem] = (),
    ) -> list[str]:
        """Queries from the brief, aimed at what is missing.

        ``coverage`` is what is already held — the library baseline, then each
        round's plan — so the queries go to the gaps rather than to parts the
        library has. Each returned query is an :class:`AimedQuery` carrying the
        aim its line named.
        """
        prompt = self.prompt("expand_queries")
        extra = coverage_block(coverage)
        if concepts:
            # Round two onward: concepts kept videos named and left unexplained.
            # Vocabulary the brief never typed — and, since they come from what
            # was kept, the vocabulary of what is already held. The prompt says
            # to use one only where it serves a gap.
            extra += (
                "\n\nTHREADS — concepts mentioned but left unexplained by videos "
                "already judged relevant. Words the brief never typed; use one where "
                "it serves a missing thing:\n"
                + "\n".join(f"- {concept}" for concept in concepts)
            )
        reply = self.client.complete(
            "You write search queries for a video search engine.",
            prompt.render(brief=brief.text, count=count, extra=extra),
            task="expand_queries",
        )
        text = reply.text
        if reply.finish_reason == "length":
            # The line being written when the ceiling hit may be half a query,
            # and a fragment is still a valid-looking search. Drop that line and
            # keep the finished ones: unlike a brief or a map, each query stands
            # alone, so the rest of the list is not made wrong by the cut.
            text = text.rstrip().rpartition("\n")[0]
            log.warning(
                "judge: query expansion was cut off at the token ceiling; "
                "its last line was dropped as a possible fragment"
            )
        queries = parse_aimed_queries(text, count)
        if not queries:
            log.warning("judge: query expansion returned nothing parseable; falling back to topic")
            return [AimedQuery(brief.topic, "")]
        return list(queries)

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
        prompt = self.prompt(METADATA_PROMPT)
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
        except LlmConfigError:
            # **A refusal is not a blip, and must not resolve toward keeping.**
            # Rule 8 sends an unanswered call toward MAYBE because the usual
            # cause is transient and the alternative deletes videos invisibly.
            # A 402 or a rejected key is the opposite case: every later call
            # fails identically, so "keep on failure" would walk the whole
            # candidate set through as MAYBE, fetch it, and store it on
            # judgements that never happened. Keeping on a blip is caution;
            # keeping on a refusal is storing an unjudged library.
            raise
        except Exception as exc:  # noqa: BLE001 - a failed batch must not reject
            # The asymmetry again: a batch that errored has judged nothing, and
            # treating "no answer" as NO would delete videos on a network blip.
            log.warning(
                "judge: stage-1 batch failed (%s) — all %d kept as MAYBE",
                safe_reason(exc),
                len(chunk),
            )
            return [
                MetadataVerdict(
                    c.video_id,
                    VERDICT_MAYBE,
                    "batch call failed",
                    parsed=False,
                    outcome=OUTCOME_CALL_FAILED,
                )
                for c in chunk
            ]

        parsed = parse_batch(reply.text, len(chunk))
        verdicts = []
        for candidate, (word, reason, was_parsed) in zip(chunk, parsed):
            # The prompt says a guessed NO, or one resting on what the metadata
            # does not show, is MAYBE; this is the half that does not depend on
            # the model having listened.
            verdict, said, resolved = resolve_stage1_no(word, reason)
            verdicts.append(
                MetadataVerdict(
                    candidate.video_id,
                    verdict,
                    reason,
                    was_parsed,
                    # `parse_batch` marks a position unparsed only when the reply
                    # gave it no verdict at all.
                    outcome=OUTCOME_JUDGED if was_parsed else OUTCOME_NO_VERDICT,
                    said=said,
                    resolved=resolved,
                )
            )
        guessed = sum(1 for verdict in verdicts if verdict.resolved == RESOLVED_GUESS)
        if guessed:
            log.info(
                "judge: %d stage-1 NO(s) rested on a guess — kept as MAYBE (rule 8)", guessed
            )
        unshown = sum(1 for verdict in verdicts if verdict.resolved == RESOLVED_UNSHOWN)
        if unshown:
            log.info(
                "judge: %d stage-1 NO(s) rested on what the metadata did not show — "
                "kept as MAYBE (rule 8)",
                unshown,
            )
        return verdicts

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

        The transcript arrives as text rather than being read from disk: this
        must never become a path that can read a stored transcript into a
        report. `brain/` cannot reach the filesystem at all, which makes that
        structural rather than careful.
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
        except LlmConfigError:
            raise  # a refusal, not a blip — see judge_metadata
        except LlmRequestTooLarge:
            # This transcript is past the model's window, so nothing read it.
            # Kept, like any call that judged nothing — and named, so the round
            # does not report a length problem as a provider blip.
            log.warning("judge: %s is too large for stage 2 — kept unread", candidate.video_id)
            return _unjudged(
                candidate.video_id,
                UNJUDGED_TOO_LARGE,
                "too large for the context window",
                OUTCOME_TOO_LARGE,
            )
        except Exception as exc:  # noqa: BLE001 - a failed call must not reject
            log.warning(
                "judge: stage-2 call failed for %s (%s) — keeping, unjudged",
                candidate.video_id,
                safe_reason(exc),
            )
            return _unjudged(
                candidate.video_id, UNJUDGED_CALL_FAILED, "call failed", OUTCOME_CALL_FAILED
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
            return _unjudged(
                candidate.video_id, UNJUDGED_TRUNCATED, "truncated reply", OUTCOME_TRUNCATED
            )

        return parse_full(reply.text, candidate.video_id)


def _unjudged(video_id: str, reason: str, problem: str, outcome: str) -> FullVerdict:
    """Stage 2 produced no verdict. Kept, and recorded as exactly that.

    These used to be YES — rule 8 resolving toward keeping, which is right for
    the video — and a YES is also what the planner counts as coverage and what
    discovery read as "already judged for this topic". So a provider blip both
    claimed a part of the brief was satisfied and stopped the same topic from
    ever asking again. MAYBE keeps the video, claims nothing about relevance,
    and leaves the next run free to judge it.
    """
    return FullVerdict(
        video_id, VERDICT_MAYBE, reason=reason, parsed=False, outcome=outcome, problem=problem
    )


# The sections every later judgement reads, as `write_brief` asks for them. A
# reply missing one of these is a fragment or a critique, not a brief. EDGE
# CASES and ALSO WANTED break ties and are not required.
#
# **Relevance needs both halves.** A video is relevant when it meets every
# REQUIRED OF EVERY VIDEO item, covers at least one of the PARTS TO COVER, and
# hits nothing EXCLUDED; the planner's coverage targets are the parts. A brief
# missing either section cannot be judged by that rule, so it is refused. The
# two replaced one heading that checkpoint 1 measured carrying both ideas: a
# topic made of parts got 6 to 9 items and three of six briefs called every
# part required, while a topic made of conditions was judged one-is-enough.
_BRIEF_HEADINGS = ("INTENT", "REQUIRED OF EVERY VIDEO", "PARTS TO COVER", "EXCLUDED")

# Every heading `write_brief` asks for, so a section's body ends where the next
# one begins. A heading is the name alone on its line, or the name followed by a
# separator and the body: `INTENT`, `## Intent`, `**EXCLUDED:**`, `Intent: ...`.
# **The name followed by a word is prose**, not a heading — `Intent is clear.`
# opens a critique, and matching it as a heading let one replace a brief.
_BRIEF_HEADING_RE = re.compile(
    r"^[ \t#*_>-]*(INTENT|REQUIRED[ \t]+OF[ \t]+EVERY[ \t]+VIDEO|PARTS[ \t]+TO[ \t]+COVER"
    r"|ALSO[ \t]+WANTED|EXCLUDED|EDGE[ \t]+CASES)\b"
    r"[ \t*_]*(?:\([^)\n]*\)[ \t*_]*)?(?:[:：—–-](?P<inline>.*))?$",
    re.IGNORECASE | re.MULTILINE,
)


def _brief_sections(text: str) -> dict[str, str]:
    """Each heading's body, by canonical name. The first occurrence wins."""
    matches = list(_BRIEF_HEADING_RE.finditer(text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        name = " ".join(match.group(1).upper().split())
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = (match.group("inline") or "") + "\n" + text[match.end() : end]
        sections.setdefault(name, body)
    return sections


def _brief_problem(reply: Reply) -> str:
    """Why this reply cannot serve as a brief, or ``""`` if it can.

    Three ways to fail: cut off, a required section missing, or a required
    section present and empty. The last is the one a heading check alone let
    through — three bare headings, twenty-eight characters under the brief's
    old headings, replaced a whole draft, where the eighty-character guard it superseded had
    refused it. Every verdict in a run is measured against this text, so a
    skeleton of one is worse than the unreviewed draft.
    """
    if reply.finish_reason == "length":
        return "cut off at the token ceiling"
    sections = _brief_sections(reply.text)
    missing = [heading for heading in _BRIEF_HEADINGS if heading not in sections]
    if missing:
        return "no " + ", ".join(missing) + " section"
    empty = [heading for heading in _BRIEF_HEADINGS if not re.search(r"\w", sections[heading])]
    if empty:
        return "empty " + ", ".join(empty) + " section"
    return ""


# Two draws of the draft: the first, and one fresh sample when it is unusable.
# A second draw is the cheap fix for a sampled overrun — the reasoning length
# varies from call to call, and the ceiling was hit by 2 of 12 drafts, not by a
# topic. A third would mostly be paying for a ceiling that is set too low or a
# prompt that does not finish, which is a configuration problem to report.
_DRAFT_ATTEMPTS = 2


def _draft_problem(reply: Reply) -> str:
    """Why this draft cannot go to the review, or ``""`` if it can.

    Narrower than `_brief_problem` on purpose. The review exists to close a
    draft's gaps — a missing EXCLUDED section is its most common repair — so a
    finished draft missing a section is still a draft. What is not a draft:

    - one cut off at the token ceiling, even with text: a fragment whose last
      section is half-written reads as a whole brief to the review;
    - an empty one, which is what a reply that spent its whole budget
      reasoning returns — the review then writes a brief from the topic alone;
    - one with no brief section at all (a refusal, a preamble, a critique).
    """
    if reply.finish_reason == "length":
        return (
            f"cut off at the token ceiling ({reply.usage.output_tokens} output tokens, "
            f"{len(reply.text.strip())} characters)"
        )
    if not reply.text.strip():
        return "empty"
    if not _brief_sections(reply.text):
        return "no brief section"
    return ""


class BriefUnusable(RuntimeError):
    """No usable draft after every attempt. The run stops before it starts.

    Raised before `discover` writes its run row, so nothing is recorded and no
    search quota is spent. The message says what each attempt returned and
    what to change, because the likely cause is a ceiling or an effort setting
    and both are the user's to fix.
    """

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        listed = "; ".join(f"attempt {n}: {p}" for n, p in enumerate(self.problems, start=1))
        super().__init__(
            f"no usable brief draft after {len(self.problems)} attempt(s) ({listed}). "
            "Nothing was searched or recorded. If every attempt was cut off, raise "
            "[discover] brief_max_tokens or lower [llm.tasks.write_brief] reasoning "
            "effort; otherwise run it again."
        )


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
