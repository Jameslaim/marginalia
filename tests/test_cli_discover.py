"""The `marg discover` report, and `marg llm-check`.

The report is a contract: a skill reads it, and the brief line is the first
thing anyone looks at when a library comes out wrong. `llm-check` is the first
thing anyone runs after swapping providers, so it has to cost exactly one
request and say plainly whether parsing worked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from marginalia.cli import EXIT_FAILED, EXIT_OK, EXIT_USAGE, main
from marginalia.flow.discover import DiscoverRun, RoundReport
from marginalia.brain.judge import Brief
from marginalia.llm import LlmConfigError

BRIEF = Brief(
    topic="prompt caching",
    text="INTENT\nUnderstand how prompt caching cuts cost in practice.\n\nMUST CONTAIN\n- x",
    version="1/1",
)


def fake_run(**overrides) -> DiscoverRun:
    run = DiscoverRun(
        run_id="abc123def456",
        topic="prompt caching",
        brief=BRIEF,
        model="test-model",
        stopped_because="no_new_videos",
        requests=24,
        input_tokens=8000,
        output_tokens=900,
        stored_ids=["aaaaaaaaaaa", "bbbbbbbbbbb"],
    )
    run.rounds = [
        RoundReport(
            number=1, queries=["a"] * 5, candidates=63, known=8,
            verdict_no=41, verdict_maybe=14, verdict_yes=8, stored=9,
        ),
        RoundReport(
            number=2, queries=["b"] * 4, candidates=22, known=19,
            verdict_no=2, verdict_maybe=1, verdict_yes=0, stored=1, from_threads=True,
        ),
    ]
    for key, value in overrides.items():
        setattr(run, key, value)
    return run


@pytest.fixture()
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "library"
    config = tmp_path / "config.toml"
    config.write_text(
        f'[library]\npath = "{root.as_posix()}"\n\n'
        '[llm]\napi_key = "k"\nmodel = "test-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MARGINALIA_CONFIG", str(config))
    return root


@pytest.fixture()
def stubbed(configured: Path, monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}

    def fake_discover(config, ledger, library, client, topic, **kwargs):
        captured.update(kwargs)
        captured["topic"] = topic
        return fake_run()

    monkeypatch.setattr("marginalia.cli.discover", fake_discover)
    return captured


# -- the report -------------------------------------------------------------


def test_the_report_leads_with_the_brief(stubbed, capsys) -> None:
    """The first question when results look wrong is what it thought the topic
    meant. That has to be on screen without opening the database."""
    assert main(["discover", "prompt caching"]) == EXIT_OK

    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == "brief:    Understand how prompt caching cuts cost in practice."


def test_the_report_has_one_line_per_round_then_a_summary(stubbed, capsys) -> None:
    main(["discover", "prompt caching"])
    lines = capsys.readouterr().out.strip().splitlines()

    assert len(lines) == 4
    assert lines[1].startswith("round 1:  5 queries, 63 candidates, 8 known, 41 no, 14 maybe")
    assert lines[1].endswith("9 stored")
    assert lines[3] == (
        "run:      2 stored, 66 judged, 2 rounds, 24 requests, stopped: no_new_videos"
    )


def test_a_thread_driven_round_says_so(stubbed, capsys) -> None:
    """Round two coming from the loop's own findings is the interesting part."""
    main(["discover", "prompt caching"])
    assert "queries from threads" in capsys.readouterr().out.splitlines()[2]


def test_a_dry_run_says_would_store(configured, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "marginalia.cli.discover",
        lambda *a, **k: fake_run(dry_run=True, stopped_because="dry_run", stored_ids=[]),
    )
    main(["discover", "prompt caching", "--dry-run"])

    summary = capsys.readouterr().out.strip().splitlines()[-1]
    assert "would store" in summary
    assert "stopped: dry_run" in summary


def test_flags_reach_the_loop(stubbed, capsys) -> None:
    main([
        "discover", "prompt caching",
        "--max-rounds", "2", "--max-videos", "5", "--max-requests", "30", "--dry-run",
    ])

    assert stubbed["max_rounds"] == 2
    assert stubbed["max_videos"] == 5
    assert stubbed["max_requests"] == 30
    assert stubbed["dry_run"] is True


def test_unset_flags_defer_to_config(stubbed, capsys) -> None:
    main(["discover", "prompt caching"])
    assert stubbed["max_rounds"] is None
    assert stubbed["max_videos"] is None


def test_no_transcript_text_can_reach_the_report(stubbed, capsys) -> None:
    """The report is built from counts and the brief. There is no field on
    DiscoverRun that carries caption text, and that is the point."""
    main(["discover", "prompt caching"])
    out = capsys.readouterr().out
    assert "transcript" not in out.lower()


# -- structured output ------------------------------------------------------


def test_json_is_valid_and_carries_the_run_id(stubbed, capsys) -> None:
    main(["discover", "prompt caching", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["run_id"] == "abc123def456"
    assert payload["model"] == "test-model"
    assert payload["brief_version"] == "1/1"
    assert payload["stopped_because"] == "no_new_videos"
    assert payload["requests"] == 24
    assert len(payload["rounds"]) == 2
    assert payload["rounds"][1]["from_threads"] is True


def test_json_carries_the_whole_brief_not_just_the_intent(stubbed, capsys) -> None:
    main(["discover", "prompt caching", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["brief"] == BRIEF.text
    assert payload["brief_intent"] in BRIEF.text


# -- llm-check --------------------------------------------------------------


def test_llm_check_reports_model_latency_and_parsing(configured, monkeypatch, capsys) -> None:
    from marginalia.llm import Reply, Usage

    monkeypatch.setattr(
        "marginalia.llm.LlmClient.complete",
        lambda self, s, u, **k: Reply("ready", "test-model", Usage(1, 12, 2), 0.4),
    )
    assert main(["llm-check"]) == EXIT_OK

    out = capsys.readouterr().out
    assert "test-model" in out
    assert "parse=ok" in out
    assert "in=12 out=2" in out


def test_llm_check_costs_exactly_one_request(configured, monkeypatch) -> None:
    from marginalia.llm import Reply, Usage

    calls: list[int] = []

    def once(self, system, user, **kwargs):
        calls.append(1)
        return Reply("ready", "test-model", Usage(1, 1, 1), 0.1)

    monkeypatch.setattr("marginalia.llm.LlmClient.complete", once)
    main(["llm-check"])
    assert len(calls) == 1


def test_llm_check_fails_loudly_when_parsing_breaks(configured, monkeypatch, capsys) -> None:
    """A provider that answers but not usably is the failure this catches —
    the one that would otherwise show up as an empty library."""
    from marginalia.llm import Reply, Usage

    monkeypatch.setattr(
        "marginalia.llm.LlmClient.complete",
        lambda self, s, u, **k: Reply("I cannot comply.", "test-model", Usage(1, 1, 1), 0.1),
    )
    assert main(["llm-check"]) == EXIT_FAILED
    assert "parse=FAILED" in capsys.readouterr().out


def test_llm_check_reports_a_config_error_as_usage(configured, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "marginalia.llm.LlmClient.complete",
        lambda self, s, u, **k: (_ for _ in ()).throw(LlmConfigError("bad key")),
    )
    assert main(["llm-check"]) == EXIT_USAGE
    assert "bad key" in capsys.readouterr().err


# -- the report cannot hide what a round actually did -----------------------


def test_fetch_failures_are_surfaced(configured, monkeypatch, capsys) -> None:
    """Observed in a real run: 13 of 13 fetches failed to YouTube throttling
    and the round line was indistinguishable from a round where the judge
    rejected everything. A degradation nobody sees is one nobody fixes."""
    run = fake_run()
    run.rounds[0].failed = 13
    monkeypatch.setattr("marginalia.cli.discover", lambda *a, **k: run)

    main(["discover", "prompt caching"])

    assert "(13 fetch failed)" in capsys.readouterr().out


def test_gate_2_drops_are_surfaced(configured, monkeypatch, capsys) -> None:
    run = fake_run()
    run.rounds[0].rejected_before = 3
    monkeypatch.setattr("marginalia.cli.discover", lambda *a, **k: run)

    main(["discover", "prompt caching"])

    assert "3 previously rejected" in capsys.readouterr().out


def test_a_clean_round_stays_terse(configured, monkeypatch, capsys) -> None:
    """The extra clauses appear only when they have something to report."""
    monkeypatch.setattr("marginalia.cli.discover", lambda *a, **k: fake_run())
    main(["discover", "prompt caching"])

    line = capsys.readouterr().out.splitlines()[1]
    assert "fetch failed" not in line
    assert "previously rejected" not in line
