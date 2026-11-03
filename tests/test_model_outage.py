"""An outage that outlasts the retries ends the run cleanly; a refusal still stops at once.

Measured: a 503 for about 8 seconds raised out of discovery after the baseline
and the plan were paid for, and the run was recorded `incomplete`, read exactly
like a crash. The run now ends as `model_unavailable`: recorded, with what it
stored kept and the failure named. Rule 24 is unchanged. Subjects are invented.
"""

from __future__ import annotations

import pytest

from marginalia.cli import _DISCOVER_FAILED, _discover_json, _discover_report
from marginalia.flow.discover import STOP_MODEL_UNAVAILABLE, STOP_REASONS
from marginalia.llm import LlmBillingError, LlmError
from test_discover import (  # noqa: F401 - fixtures are found by name
    FakeJudge,
    client,
    config,
    ledger,
    library,
    result,
    run,
    searches,
)


class _DownInRoundTwo(FakeJudge):
    def __init__(self, error, **kwargs) -> None:
        super().__init__(**kwargs)
        self._error = error

    def expand_queries(self, brief, *, count=9, concepts=(), coverage=()):
        if self.query_calls:
            raise self._error
        return super().expand_queries(brief, count=count, concepts=concepts, coverage=coverage)


def test_an_outage_past_the_retries_ends_the_run_recorded_and_keeps_what_it_stored(
    config, ledger, library, client, searches
) -> None:
    searches["*"] = [result("storedvid01")]
    judge = _DownInRoundTwo(LlmError("model call failed after 10 attempts: transient HTTP 503"),
                            queries=[["coop heaters"]])

    outcome = run(config, ledger, library, client, judge, max_rounds=3)

    assert STOP_MODEL_UNAVAILABLE in STOP_REASONS
    assert outcome.stopped_because == STOP_MODEL_UNAVAILABLE
    assert "transient HTTP 503" in outcome.stop_detail
    assert outcome.stored_ids == ["storedvid01"], "what it stored is kept"
    row = ledger.get_run(outcome.run_id)
    assert row.finished_at and row.stopped_because == STOP_MODEL_UNAVAILABLE
    assert "stopped: model_unavailable — the model provider stayed unavailable" in "\n".join(
        _discover_report(outcome))
    assert _discover_json(outcome)["stop_detail"] == outcome.stop_detail
    assert STOP_MODEL_UNAVAILABLE in _DISCOVER_FAILED, "reported, then exit 1"


def test_a_refusal_still_stops_at_once(config, ledger, library, client, searches) -> None:
    searches["*"] = [result("storedvid01")]
    judge = _DownInRoundTwo(LlmBillingError("402: insufficient balance"), queries=[["coop heaters"]])

    with pytest.raises(LlmBillingError):
        run(config, ledger, library, client, judge, max_rounds=3)
