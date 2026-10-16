"""Rule 24/25, made structural: a refusal is never swallowed by a fallback.

Rule 24 says a provider refusal (`LlmConfigError`: billing, a rejected key, a
wait longer than the run) is authoritative and must not resolve toward keeping
or carrying on. Rule 25 says a guard added to one caller must be traced to every
caller that shares the failure — and records that this tracing was skipped twice.
It was skipped a third time: the judges, the selector and the note drain
re-raised refusals ahead of their catch-alls; the territory mapper, the library
assessment and the round planner did not, so a 402 during mapping was swallowed
three times and the run died one call later recorded as `incomplete`.

So this file does the tracing instead of a reviewer: every `try` whose body
makes a model call is found in the source, and any handler broad enough to catch
a refusal must be preceded by one that re-raises it. A new call site written
without the guard fails here, not in production.
"""

from __future__ import annotations

import ast
from pathlib import Path

import marginalia

SOURCE = Path(marginalia.__file__).resolve().parent

# Handlers that would catch `LlmConfigError` if it reached them.
_BROAD = {"Exception", "BaseException", "LlmError", "RuntimeError"}

# Call sites that deliberately convert a refusal into a *reported failure* and
# make no further call after it. Each entry says why; adding one needs the same.
#
# **The scan is lexical**: it sees `.complete(` written inside the `try` itself,
# not a `try` around a helper that calls the model (`ask.answer_question`'s try
# calls `_call`, for instance, and converts any `LlmError` — refusals included —
# into a reported failure with exit 1). Such sites are not policed here and need
# a reviewer's eye; the independent review found this limit.
_ALLOWED = {
    # `marg llm-check` exists to report what the provider says to one request.
    # Every `LlmError`, refusals included, is printed and exits non-zero; there
    # is no second call to protect.
    ("cli.py", "_cmd_llm_check"),
}


def _names(node: ast.expr | None) -> set[str]:
    if node is None:
        return {"<bare>"}
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    if isinstance(node, ast.Tuple):
        return set().union(*(_names(element) for element in node.elts))
    return set()


def _calls_the_model(body: list[ast.stmt]) -> bool:
    for statement in body:
        for node in ast.walk(statement):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "complete"
            ):
                return True
    return False


def _reraises(handler: ast.ExceptHandler) -> bool:
    return any(isinstance(node, ast.Raise) and node.exc is None for node in handler.body)


def _violations() -> list[str]:
    found: list[str] = []
    for path in sorted(SOURCE.rglob("*.py")):
        if path.name == "llm.py":
            continue  # the client itself: its try wraps the transport, not `.complete`
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if not isinstance(node, ast.Try) or not _calls_the_model(node.body):
                    continue
                guarded = False
                for handler in node.handlers:
                    names = _names(handler.type)
                    if "LlmConfigError" in names and _reraises(handler):
                        guarded = True
                        continue
                    broad = names & (_BROAD | {"<bare>"})
                    if broad and not guarded and (path.name, function.name) not in _ALLOWED:
                        found.append(
                            f"{path.relative_to(SOURCE)}:{handler.lineno} in {function.name}() "
                            f"catches {sorted(broad)} around a model call without first "
                            "re-raising LlmConfigError"
                        )
    return found


def test_no_fallback_swallows_a_provider_refusal() -> None:
    assert _violations() == []


def test_the_scan_finds_the_call_sites_it_is_meant_to_police() -> None:
    """A scan that finds nothing passes trivially. These are the known sites; if
    one moves or is renamed, this says the guard above stopped looking at it."""
    sites = set()
    for path in sorted(SOURCE.rglob("*.py")):
        if path.name == "llm.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                isinstance(node, ast.Try) and _calls_the_model(node.body)
                for node in ast.walk(function)
            ):
                sites.add(function.name)
    assert {"_judge_chunk", "judge_full", "_sample", "assess_library", "plan", "_one_batch"} <= sites


def test_the_scan_catches_a_missing_guard(tmp_path, monkeypatch) -> None:
    """The detector itself, on a synthetic module: broad catch, no re-raise."""
    package = tmp_path / "fake"
    package.mkdir()
    (package / "mod.py").write_text(
        "def f(client):\n"
        "    try:\n"
        "        client.complete('s', 'u')\n"
        "    except Exception:\n"
        "        return None\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(__import__(__name__), "SOURCE", package)
    assert len(_violations()) == 1


# -- the behaviour, at each site the scan polices -------------------------------

import pytest  # noqa: E402

from conftest import make_llm_config  # noqa: E402
from marginalia.contracts import Brief, RoundReport, TerritoryMap  # noqa: E402
from marginalia.llm import LlmBillingError, LlmClient  # noqa: E402


class _PaymentRequired(Exception):
    status_code = 402


def _refusing_client(calls: list[int]) -> LlmClient:
    def transport(**_):
        calls.append(1)
        raise _PaymentRequired()

    return LlmClient(make_llm_config(), transport=transport, sleep=lambda _: None)


BRIEF = Brief(
    topic="prompt caching",
    text="INTENT\nx\nREQUIRED OF EVERY VIDEO\n- x\nPARTS TO COVER\n- x\nEXCLUDED\n- y",
    version="1/1",
)


def test_a_refusal_during_mapping_stops_after_one_call() -> None:
    """It was swallowed three times (one per recency sample) and stored as a
    model failure against the map."""
    from marginalia.brain.territory import Mapper

    calls: list[int] = []
    with pytest.raises(LlmBillingError):
        Mapper(_refusing_client(calls)).map(BRIEF)
    assert len(calls) == 1


def test_a_refusal_during_the_library_assessment_is_raised() -> None:
    from marginalia.brain.planner import Planner

    with pytest.raises(LlmBillingError):
        Planner(_refusing_client([])).assess_library(BRIEF, "id | 20260101 | t | c", notes_read=1)


def test_a_refusal_during_planning_is_raised() -> None:
    from marginalia.brain.planner import Planner

    with pytest.raises(LlmBillingError):
        Planner(_refusing_client([])).plan(
            BRIEF, TerritoryMap(topic="prompt caching", version="1"), RoundReport(number=1, queries=[])
        )


def test_a_discovery_run_ended_by_a_refusal_says_so(tmp_path) -> None:
    """It was recorded as `incomplete`, the word for a crash."""
    from conftest import make_config
    from marginalia.brain.judge import Judge
    from marginalia.brain.planner import Planner
    from marginalia.brain.territory import Mapper
    from marginalia.core.ledger import Ledger
    from marginalia.core.library import Library
    from marginalia.flow.discover import discover

    config = make_config(tmp_path)
    library = Library(config.library_path)
    library.ensure()
    client = _refusing_client([])

    class Briefed(Judge):
        def write_brief(self, topic, *, max_tokens=None):
            return BRIEF

    with Ledger(library.ledger_path) as ledger:
        with pytest.raises(LlmBillingError):
            discover(
                config, ledger, library, client, "prompt caching",
                judge=Briefed(client), mapper=Mapper(client), planner=Planner(client),
                provider=object(),
            )
        (run,) = ledger.list_runs()
    assert run.stopped_because == "provider_refused"
    assert run.finished_at is not None


def test_the_note_drain_exits_non_zero_when_refused(tmp_path, monkeypatch) -> None:
    """R3 probe S7: `stopped: provider_refused` printed and exit 0, which a
    script reads as every note written."""
    from marginalia.cli import EXIT_FAILED, main
    from marginalia.contracts import NoteRun, STOP_REFUSED

    config = tmp_path / "config.toml"
    config.write_text(
        f'[library]\npath = "{(tmp_path / "library").as_posix()}"\n'
        '[llm]\nbase_url = "https://example.invalid/v1"\nmodel = "test-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MARGINALIA_CONFIG", str(config))
    monkeypatch.setattr(
        "marginalia.cli.write_notes", lambda *a, **k: NoteRun(version="9", model="m", stopped_because=STOP_REFUSED)
    )

    assert main(["note"]) == EXIT_FAILED
