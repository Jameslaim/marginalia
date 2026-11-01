"""The start-of-run web lookup (step 4): a few searches, digested, for the plan only.

Runs after the library baseline and before the search plan, when `[discover]
web_context` is on and the source is the plan. Its queries are aimed by the
coverage, as the plan's are; its results are digested into short tagged lines
for the plan's CURRENT CONTEXT slot. **Never a reason to stop a run**: a
refused account, a timeout, an empty or unreadable result, or a failed digest
leaves the plan with its context `absent` or `empty`, which the plan line says,
and the failure is counted. Writes nothing; `discover` records what it did.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Sequence

from ..brain.search_plan import render_context
from ..brain.web_context import ContextWriter, gaps_for
from ..config import Config
from ..contracts import (
    WEB_CONTEXT_EMPTY,
    WEB_CONTEXT_FAILED,
    WEB_CONTEXT_OFF,
    WEB_CONTEXT_SUPPLIED,
    Brief,
    CoverageItem,
    WebContext,
    WebLookup,
)
from ..llm import LlmClient
from ..providers.base import ProviderError, WebSearchProvider, WebSearchRefused

log = logging.getLogger(__name__)

__all__ = ["gather_context", "web_search_provider"]


def web_search_provider(config: Config) -> WebSearchProvider:
    """The configured provider: the model host's own search, on the model account's key."""
    from ..providers import get_web_search_provider

    return get_web_search_provider("host_search", api_key=config.llm.api_key,
                                   base_url=config.llm.base_url)


def gather_context(
    config: Config,
    client: LlmClient,
    brief: Brief,
    coverage: Sequence[CoverageItem],
    *,
    enabled: bool,
    provider: WebSearchProvider | None = None,
    writer: ContextWriter | None = None,
) -> WebContext:
    """The lookup, start to end. Returns what it did; raises only a refusal of
    the model account itself (rule 24), never a lookup failure."""
    if not enabled:
        return WebContext(status=WEB_CONTEXT_OFF, reason="web_context is off")
    limits = config.discover
    writer = writer or ContextWriter(client)
    context = WebContext(status=WEB_CONTEXT_FAILED)

    try:
        provider = provider or web_search_provider(config)
    except WebSearchRefused as exc:
        context.reason = f"no web search provider: {exc}"
        return context
    context.provider = provider.name

    gaps = gaps_for(brief.text, coverage)
    queries, refused = writer.queries(brief.topic, brief.text, gaps, count=limits.web_context_queries)
    context.model_requests = writer.requests
    if queries is None:
        context.reason = refused
        return context
    context.queries_refused = refused

    for query in queries:
        try:
            results = provider.search(query.text, count=limits.web_context_results)
        except WebSearchRefused as exc:
            # Every later search fails the same way: stop searching, keep going.
            context.lookups.append(WebLookup(query.text, query.aim, error=str(exc)))
            log.warning("web context: the search provider refused the account (%s)", exc)
            break
        except ProviderError as exc:
            context.lookups.append(WebLookup(query.text, query.aim, error=str(exc)))
            continue
        context.lookups.append(WebLookup(query.text, query.aim, tuple(results)))
        if results:
            # Billed only when it returns a non-empty result.
            context.searches_billed += 1
            context.search_cost_usd += provider.cost_per_search()

    if not context.results:
        errors = [lookup.error for lookup in context.lookups if lookup.error]
        context.status = WEB_CONTEXT_FAILED if errors and len(errors) == len(context.lookups) else WEB_CONTEXT_EMPTY
        context.reason = errors[0] if errors else "the searches returned nothing"
        return context

    lines, dropped = writer.digest(brief.text, gaps, context.lookups)
    context.model_requests = writer.requests
    if lines is None:
        context.reason = dropped
        return context
    context.lines, context.dropped = list(lines), dropped
    if not lines:
        context.status, context.reason = WEB_CONTEXT_EMPTY, "no digest line survived the checks"
        return context
    context.status = WEB_CONTEXT_SUPPLIED
    block, _ = render_context([line.render() for line in lines])
    context.sha256 = hashlib.sha256(block.encode("utf-8")).hexdigest()
    return context


def context_lines(context: WebContext) -> list[str] | None:
    """What the plan's slot is given: the lines when supplied, an empty list
    when a lookup ran and nothing survived (the slot says a lookup returned
    nothing usable), ``None`` when no lookup ran or it failed (absent)."""
    if context.status == WEB_CONTEXT_SUPPLIED:
        return [line.render() for line in context.lines]
    if context.status == WEB_CONTEXT_EMPTY:
        return []
    return None
