"""Find candidate videos, and decide which of them are *known*.

This module answers exactly one question per candidate: have we seen this
before? It never decides whether a video is any good — relevance judging is
M2b, needs a model, and lives elsewhere. If you find yourself wanting to add a
verdict that means "irrelevant" here, you are in the wrong file.

Two gates run, both free, both before anything is spent:

============  =========================================  ================
Gate          Drops                                      Ledger evidence
============  =========================================  ================
1. known ID   videos already ``stored`` or ``duplicate``  ``videos.status``
2. rejection  videos with a ``no`` verdict *for this      ``decisions``
              exact topic*
============  =========================================  ================

Gate 2 matches on the normalised topic string and nothing else. Fuzzy topic
similarity is deferred deliberately: a wrong fuzzy match hides a video
permanently and invisibly, while a missed exact match costs one cheap re-judge.
See ``PLAN.md`` §6.

Every candidate — kept or dropped — gets a ``search``-stage row in
``decisions``. The log cannot be reconstructed retroactively, and a judge that
misbehaves in M2b is diagnosable only from what was actually recorded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Sequence

from .config import Config
from .ledger import (
    KNOWN_STATUSES,
    STAGE_SEARCH,
    STATUS_DUPLICATE,
    VERDICT_MAYBE,
    VERDICT_NO,
    Ledger,
    normalise_topic,
)
from .providers import (
    PermanentError,
    ProviderUnavailable,
    QuotaExhausted,
    SearchResult,
    TransientError,
    get_search_provider,
)

log = logging.getLogger(__name__)

__all__ = ["Candidate", "SearchRun", "run_search", "NEW", "KNOWN", "REJECTED"]

# What the report prints in its first column, and what `--json` carries.
NEW = "new"
KNOWN = "known"
REJECTED = "rejected"


@dataclass(frozen=True)
class Candidate:
    result: SearchResult
    verdict: str  # new | known | rejected
    reason: str  # short, printable, stable — a skill reads it

    @property
    def video_id(self) -> str:
        return self.result.video_id


@dataclass(frozen=True)
class SearchRun:
    query: str
    topic: str
    provider: str | None
    candidates: list[Candidate]
    # (provider, why) for each provider that was tried and fell through, in
    # order. The report names these: a silent fallback looks like a quota that
    # never runs out.
    fallbacks: list[tuple[str, str]] = field(default_factory=list)

    def of(self, verdict: str) -> list[Candidate]:
        return [candidate for candidate in self.candidates if candidate.verdict == verdict]

    @property
    def counts(self) -> dict[str, int]:
        return {
            "found": len(self.candidates),
            KNOWN: len(self.of(KNOWN)),
            REJECTED: len(self.of(REJECTED)),
            NEW: len(self.of(NEW)),
        }


def run_search(
    config: Config, ledger: Ledger, query: str, *, limit: int = 25, topic: str | None = None
) -> SearchRun:
    """Search, gate, log. No network call is made for a gated-out video."""
    normalised = normalise_topic(topic if topic is not None else query)
    results, provider, fallbacks = _search(config, ledger, query, limit)

    candidates = [
        _gate(ledger, result, normalised) for result in _deduplicate(results)
    ]
    return SearchRun(
        query=query,
        topic=normalised,
        provider=provider,
        candidates=candidates,
        fallbacks=fallbacks,
    )


# -- the gates -------------------------------------------------------------


def _gate(ledger: Ledger, result: SearchResult, topic: str) -> Candidate:
    verdict, reason, logged = _verdict(ledger, result, topic)
    ledger.log_decision(
        result.video_id,
        topic,
        stage=STAGE_SEARCH,
        verdict=logged,
        reason=reason,
    )
    return Candidate(result=result, verdict=verdict, reason=reason)


def _verdict(ledger: Ledger, result: SearchResult, topic: str) -> tuple[str, str, str]:
    """Return ``(report verdict, reason, logged verdict)``.

    The logged verdict is one of yes/no/maybe; a survivor is ``maybe``, not
    ``yes``, because this stage has judged nothing about it.
    """
    row = ledger.get(result.video_id)
    if row is not None and row.status in KNOWN_STATUSES:
        if row.status == STATUS_DUPLICATE:
            reason = f"duplicate of {row.duplicate_of}" if row.duplicate_of else "duplicate"
        else:
            reason = "already stored"
        return KNOWN, reason, VERDICT_NO

    previous = ledger.latest_decision(result.video_id, topic, verdict=VERDICT_NO)
    if previous is not None:
        return REJECTED, f"no for this topic ({previous.decided_on})", VERDICT_NO

    return NEW, "new candidate", VERDICT_MAYBE


def _deduplicate(results: Sequence[SearchResult]) -> list[SearchResult]:
    """One row per video ID, first occurrence wins — rank order is meaning."""
    seen: set[str] = set()
    unique: list[SearchResult] = []
    for result in results:
        if result.video_id in seen:
            continue
        seen.add(result.video_id)
        unique.append(result)
    dropped = len(results) - len(unique)
    if dropped:
        log.info("search: dropped %d repeated video id(s) from the result set", dropped)
    return unique


# -- the provider chain ----------------------------------------------------


def _search(
    config: Config, ledger: Ledger, query: str, limit: int
) -> tuple[list[SearchResult], str | None, list[tuple[str, str]]]:
    """Try each configured provider in order.

    Falls through on quota exhaustion, an unavailable provider, or a transient
    failure — all three mean "this source cannot answer right now", and an
    overflow provider exists precisely for that. Does **not** fall through on a
    bad query or a rejected key: those fail identically everywhere, and hiding
    them behind a fallback turns a fixable mistake into a mystery.
    """
    fallbacks: list[tuple[str, str]] = []

    for name in config.search_order:
        try:
            provider = get_search_provider(
                name,
                api_key=config.youtube_api_key,
                quota=ledger,
                daily_units=config.youtube_daily_units,
            )
        except KeyError:
            raise PermanentError(
                f"unknown search provider {name!r} in {config.path}"
            ) from None
        except ProviderUnavailable as exc:
            log.info("search provider %s unavailable: %s", name, exc)
            fallbacks.append((name, str(exc)))
            continue

        try:
            results = provider.search(query, limit=limit)
        except QuotaExhausted as exc:
            log.info("search provider %s is out of quota: %s", name, exc)
            fallbacks.append((name, str(exc)))
            continue
        except TransientError as exc:
            log.info("search provider %s failed transiently: %s", name, exc)
            fallbacks.append((name, str(exc)))
            continue

        log.info("search provider %s returned %d result(s)", name, len(results))
        return list(results), provider.name, fallbacks

    raise TransientError(
        "no search provider could answer: "
        + "; ".join(f"{name}: {why}" for name, why in fallbacks)
    )
