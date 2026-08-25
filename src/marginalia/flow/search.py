"""Find candidate videos, and decide which of them are *known*.

This module answers exactly one question per candidate: have we seen this
before? It never decides whether a video is any good — relevance judging is
M2b, needs a model, and lives elsewhere. If you find yourself wanting to add a
verdict that means "irrelevant" here, you are in the wrong file.

Two gates run, both free, both before anything is spent:

============  =========================================  ================
Gate          Drops                                      Ledger evidence
============  =========================================  ================
1. known ID   videos already in the library, and          ``in_library()``
              videos this run stored in an earlier        ``stored_this_run``
              round
2. rejection  videos with a ``no`` verdict *for this      ``decisions``
              exact topic*
============  =========================================  ================

Gate 1 asks two questions, not one, and they have different answers for a few
minutes at a time. ``in_library`` is about notes, and notes are written by a
worker that runs *after* discovery finishes — so a video stored in round one is
not in the library during round two, and every later round used to re-judge it.
The fetch-step disk check meant no re-download, so this cost model calls and
inflated the round counters rather than bandwidth — but the planner reads those
counters, and a video counted as a fresh candidate three times over is three
pieces of evidence that do not exist. ``in_library`` is *not* weakened to fix
this; the run-scoped question is passed in separately, because "is this in the
library?" and "did I store this ten seconds ago?" are two questions and B1's
whole point is not to conflate them.

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
from typing import Collection, Sequence

from ..config import Config
from ..contracts import (
    KNOWN,
    NEW,
    REJECTED,
    STATUS_DUPLICATE,
    STORED_THIS_RUN,
    VERDICT_MAYBE,
    VERDICT_NO,
    Candidate,
    SearchFilters,
    SearchResult,
    SearchRun,
)
from ..core.ledger import STAGE_SEARCH, Ledger, in_library, normalise_topic
from ..providers import (
    PermanentError,
    ProviderUnavailable,
    QuotaExhausted,
    SearchResult,
    TransientError,
    get_search_provider,
)

log = logging.getLogger(__name__)

__all__ = ["run_search", "quota_headroom"]


def run_search(
    config: Config,
    ledger: Ledger,
    query: str,
    *,
    limit: int = 25,
    topic: str | None = None,
    log_decisions: bool = True,
    run_id: str | None = None,
    filters: SearchFilters | None = None,
    stored_this_run: Collection[str] = (),
) -> SearchRun:
    """Search, gate, log. No network call is made for a gated-out video.

    ``log_decisions=False`` gates without recording anything, which is what
    ``discover --dry-run`` needs: a dry run that wrote `no` rows would poison
    Gate 2 for the real run that follows, so tuning the judge would silently
    shrink the library it was meant to improve.

    ``stored_this_run`` is the second half of Gate 1 — the videos the caller
    has already stored and will not want judged again. Empty by default, and
    empty is the honest default: a single search has no run around it, and a
    dry run stores nothing.

    ``filters`` have already been validated by `brain/territory.py` — nothing
    here re-decides whether one is allowed. They are handed to the provider,
    which applies what it can.
    """
    normalised = normalise_topic(topic if topic is not None else query)
    results, provider, fallbacks, units, applied = _search(
        config, ledger, query, limit, filters
    )

    candidates = [
        _gate(
            ledger,
            result,
            normalised,
            log=log_decisions,
            run_id=run_id,
            stored_this_run=stored_this_run,
        )
        for result in _deduplicate(results)
    ]
    return SearchRun(
        query=query,
        topic=normalised,
        provider=provider,
        candidates=candidates,
        fallbacks=fallbacks,
        # What was *asked for*, and what the provider actually applied. They
        # differ whenever the chain falls through to a provider that cannot
        # filter, and B5 needs the difference recorded rather than assumed: the
        # planner reads query history to choose what to try next, and a row
        # claiming `order=date` ran when the search was plain would tell it an
        # experiment happened that did not.
        filters=filters or SearchFilters(),
        filters_applied=applied,
        quota_units=units,
    )


# -- the gates -------------------------------------------------------------


def _gate(
    ledger: Ledger,
    result: SearchResult,
    topic: str,
    *,
    log: bool = True,
    run_id: str | None = None,
    stored_this_run: Collection[str] = (),
) -> Candidate:
    verdict, reason, logged = _verdict(ledger, result, topic, stored_this_run)
    if log:
        ledger.log_decision(
            result.video_id,
            topic,
            stage=STAGE_SEARCH,
            verdict=logged,
            reason=reason,
            run_id=run_id,
        )
    return Candidate(result=result, verdict=verdict, reason=reason)


def _verdict(
    ledger: Ledger,
    result: SearchResult,
    topic: str,
    stored_this_run: Collection[str] = (),
) -> tuple[str, str, str]:
    """Return ``(report verdict, reason, logged verdict)``.

    The logged verdict is one of yes/no/maybe; a survivor is ``maybe``, not
    ``yes``, because this stage has judged nothing about it.
    """
    if result.video_id in stored_this_run:
        # Checked first: it is the more specific answer, and the more recent
        # one. Logged as `maybe`, not `no` — this run *kept* the video, and a
        # `no` row here would tell Gate 2, in every later run on this topic,
        # that the video had been turned down. That is the invisible error
        # rule 8 exists to avoid, arriving through a counter fix.
        return STORED_THIS_RUN, "stored earlier in this run", VERDICT_MAYBE

    row = ledger.get(result.video_id)
    if in_library(row):
        assert row is not None  # in_library() is False for None
        if row.status == STATUS_DUPLICATE:
            reason = f"duplicate of {row.duplicate_of}" if row.duplicate_of else "duplicate"
        else:
            reason = "already in the library"
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
    config: Config,
    ledger: Ledger,
    query: str,
    limit: int,
    filters: SearchFilters | None = None,
) -> tuple[list[SearchResult], str | None, list[tuple[str, str]], int, SearchFilters]:
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
            results = provider.search(query, limit=limit, filters=filters)
        except QuotaExhausted as exc:
            log.info("search provider %s is out of quota: %s", name, exc)
            fallbacks.append((name, str(exc)))
            continue
        except TransientError as exc:
            log.info("search provider %s failed transiently: %s", name, exc)
            fallbacks.append((name, str(exc)))
            continue

        log.info("search provider %s returned %d result(s)", name, len(results))
        # The provider's own accounting of what that cost, and of what of the
        # request it could honour. `getattr` on both because each arrived in a
        # later phase and a provider written against the older protocol is
        # still a valid provider — the honest fallback for the first is 0, and
        # for the second is "we do not know that any filter ran", which is the
        # same conservative answer an unfiltered search deserves.
        units = int(getattr(provider, "units_spent", lambda: 0)())
        applied = getattr(provider, "applied_filters", SearchFilters)()
        if filters and applied.as_dict() != filters.as_dict():
            dropped = sorted(set(filters.as_dict()) - set(applied.as_dict()))
            log.warning(
                "search: %s could not apply %s on %r; the query ran unfiltered",
                name,
                ", ".join(dropped),
                query,
            )
        return list(results), provider.name, fallbacks, units, applied

    raise TransientError(
        "no search provider could answer: "
        + "; ".join(f"{name}: {why}" for name, why in fallbacks)
    )


def quota_headroom(config: Config, ledger: Ledger) -> tuple[int | None, int]:
    """``(units left, units one search costs)`` for the chain as configured.

    ``None`` for the first value means **there is no ceiling to hit**, and that
    is the answer whenever any provider in the chain is unmetered. It is not a
    fudge: the overflow provider exists precisely so a run continues when the
    metered one runs dry, so stopping a run for want of quota while a free
    fallback sits in the chain would be inventing a limit that does not apply.

    Only the chain matters, not the run. This asks what the *next* round can
    afford before the round starts, because the alternative is discovering it
    mid-round and recording half a round's evidence — and half a round is
    exactly the input B5's planner must never be handed.
    """
    per_search = 0
    remaining: int | None = None

    for name in config.search_order:
        try:
            provider = get_search_provider(
                name,
                api_key=config.youtube_api_key,
                quota=ledger,
                daily_units=config.youtube_daily_units,
            )
        except (KeyError, ProviderUnavailable):
            # An unconfigured or unknown provider cannot answer, so it cannot be
            # the free fallback either. Skipped rather than treated as unmetered.
            continue

        left = provider.quota_remaining()
        if left is None:
            # An unmetered provider is in the chain and can answer. Nothing here
            # runs out.
            return None, 0
        cost = int(getattr(provider, "units_per_search", lambda: 0)())
        if remaining is None or left > remaining:
            remaining, per_search = left, cost

    return remaining, per_search
