"""A stored or owner-set recency window is applied, exactly as a sampled one is.

Measured: an owner-set "last 24 months" reached none of a run's 37 queries,
because the topic said "what still works from classic SEO" and "classic" read
as the user asking for older material. The owner's window, set for the approved
brief, now outranks a phrase in the topic; a window the model sampled or
decided still yields to it. Subjects are invented.
"""

from __future__ import annotations

import json
from datetime import date

from marginalia.brain.judge import brief_sha256
from marginalia.brain.territory import validate
from marginalia.contracts import (
    ORIGIN_PLAN,
    RECENCY_DECIDED,
    RECENCY_OWNER,
    RECENCY_SAMPLED,
    PlannedQuery,
    Recency,
)
from marginalia.flow.brief import set_owner_recency
from test_discover import (  # noqa: F401 - fixtures are found by name
    FakeJudge,
    FakeMapper,
    client,
    config,
    ledger,
    library,
    run,
    searches,
)

TODAY = date(2026, 11, 3)
QUERIES = [PlannedQuery(f"coop build {n}", ORIGIN_PLAN, "aim: Coop design") for n in range(3)]


class _WindowMapper(FakeMapper):
    """The map, given the brief's stored window, uses it."""

    def map(self, brief, *, count=8, max_tokens=None, recency=None):
        self.calls += 1
        return self.territory if recency is None else self.territory.__class__(
            topic=self.territory.topic, version=self.territory.version, recency=recency)


def _filters(topic, recency):
    kept, _ = validate(QUERIES, recency=recency, topic=topic, today=TODAY)
    return [query.filters.as_dict() for query in kept]


def test_owner_set_stored_decided_and_sampled_windows_give_the_same_filters() -> None:
    topic = "keeping backyard hens through winter"
    windows = [Recency(months=24, source=source, samples=samples) for source, samples in
               ((RECENCY_OWNER, ()), (RECENCY_DECIDED, (24, 24, 24)), (RECENCY_SAMPLED, (24, 24, 24)))]
    filters = [_filters(topic, window) for window in windows]
    assert filters[0] == filters[1] == filters[2]
    assert all(f["publishedAfter"] == "2024-11-03" for f in filters[0])


def test_an_owner_set_window_outranks_a_timeless_word_in_the_topic() -> None:
    topic = "what still works from classic coop building"
    owner = _filters(topic, Recency(months=24, source=RECENCY_OWNER))
    assert all(f["publishedAfter"] == "2024-11-03" for f in owner)
    kept, _ = validate(QUERIES, recency=Recency(months=24, source=RECENCY_OWNER), topic=topic, today=TODAY)
    assert "outranks the topic's 'classic'" in kept[0].reasons["publishedAfter"]


def test_a_decided_or_sampled_window_still_yields_to_the_word() -> None:
    topic = "what still works from classic coop building"
    for source in (RECENCY_DECIDED, RECENCY_SAMPLED):
        assert _filters(topic, Recency(months=24, source=source, samples=(24,))) == [{}, {}, {}]


def test_the_run_applies_the_owners_window_to_every_main_query(
    config, ledger, library, client, searches
) -> None:
    topic = "what still works from classic coop building"
    text = "INTENT\nBuild a coop.\n\nPARTS TO COVER\n- Coop design: ventilation\n"
    ledger.approve_brief(topic, text, brief_sha256(text))
    assert set_owner_recency(ledger, topic, "24 months")[1] == ""

    outcome = run(config, ledger, library, client, FakeJudge(queries=[["coop vents", "coop roof"]]),
                  topic=topic, max_rounds=1, mapper=_WindowMapper(), search_source="map")

    rows = ledger.queries_for_run(outcome.run_id)
    assert rows and all("publishedAfter" in json.loads(row.filters_json or "{}") for row in rows)
