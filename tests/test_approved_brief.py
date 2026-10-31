"""A topic's brief is written once to review, approved by its owner, and reused.

Every `marg discover` wrote a fresh brief, so the PARTS and the things they
list changed between runs of one topic — one run's harness part listed routing
and retries, the next brief for the same topic named neither — and coverage
across runs was measured against whichever brief each run drew.

The topics below are invented and general; none is a test topic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from marginalia.brain.judge import brief_check, brief_parts, brief_sha256
from marginalia.cli import EXIT_FAILED, EXIT_OK, main
from marginalia.contracts import Brief
from marginalia.core.ledger import Ledger, normalise_topic
from marginalia.core.library import Library

TOPIC = "Home  Plumbing Repairs"
BRIEF = (
    "INTENT\nLearn how to diagnose and fix common household plumbing faults.\n\n"
    "REQUIRED OF EVERY VIDEO\n- explains why the fault happens, not only the fix\n\n"
    "PARTS TO COVER\n"
    "- Blocked drains: why they clog, clearing tools (plungers, augers), and when to call a plumber\n"
    "- Leaking taps: worn washers; cartridge failures\n\n"
    "ALSO WANTED\n- names the parts and tools by their trade names\n\n"
    "EXCLUDED\n- product adverts for drain cleaners\n\n"
    "EDGE CASES\n- trade-school lectures count if they explain a real repair\n"
)


@pytest.fixture()
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "library"
    config = tmp_path / "config.toml"
    config.write_text(
        f'[library]\npath = "{root.as_posix()}"\n\n'
        '[llm]\napi_key = "k"\nbase_url = "https://example.invalid/v1"\nmodel = "test-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MARGINALIA_CONFIG", str(config))
    return root


@pytest.fixture()
def writer(configured, monkeypatch):
    """A judge whose write_brief returns BRIEF, counting its calls."""
    calls: list[str] = []

    class FakeJudge:
        def __init__(self, client) -> None:
            pass

        def write_brief(self, topic, *, max_tokens=None):
            calls.append(topic)
            return Brief(topic=topic, text=BRIEF, version="5/5")

    monkeypatch.setattr("marginalia.cli.Judge", FakeJudge)
    return calls


def _pending(root: Path) -> Path:
    return Library(root).pending_brief_path(normalise_topic(TOPIC))


def _approved(root: Path, topic: str = TOPIC):
    with Ledger(Library(root).ledger_path) as ledger:
        return ledger.approved_briefs(topic)


# -- writing a brief to review -------------------------------------------------


def test_writing_saves_a_pending_file_and_prints_its_parts(configured, writer, capsys) -> None:
    assert main(["brief", TOPIC]) == EXIT_OK

    out = capsys.readouterr().out
    path = _pending(configured)
    assert path.read_text(encoding="utf-8").strip() == BRIEF.strip()
    assert f"file:     {path}" in out
    assert "parts:    2" in out
    assert "  - Blocked drains" in out
    assert "      . clearing tools (plungers, augers)" in out
    assert "      . when to call a plumber" in out
    assert "--approve" in out
    assert writer == [TOPIC], "one write and review, through the usual path"
    assert _approved(configured) == [], "writing approves nothing"


def test_a_pending_brief_is_never_overwritten_without_force(configured, writer, capsys) -> None:
    main(["brief", TOPIC])
    _pending(configured).write_text("the owner's edits", encoding="utf-8")

    assert main(["brief", TOPIC]) == EXIT_FAILED
    assert "--force" in capsys.readouterr().err
    assert _pending(configured).read_text(encoding="utf-8") == "the owner's edits"
    assert len(writer) == 1, "refused before any model call"

    assert main(["brief", TOPIC, "--force"]) == EXIT_OK
    assert _pending(configured).read_text(encoding="utf-8").strip() == BRIEF.strip()


# -- approval ------------------------------------------------------------------


def test_an_edited_brief_is_checked_and_stored_as_version_one(configured, writer, capsys) -> None:
    main(["brief", TOPIC])
    edited = BRIEF.replace("- Leaking taps", "- Low water pressure: causes and fixes\n- Leaking taps")
    _pending(configured).write_text(edited, encoding="utf-8")

    assert main(["brief", TOPIC, "--approve"]) == EXIT_OK

    out = capsys.readouterr().out
    rows = _approved(configured)
    assert [row.version for row in rows] == [1]
    assert rows[0].brief == edited.strip()
    assert rows[0].sha256 == brief_sha256(edited)
    assert rows[0].prompt_version == "5/5"
    assert "approved: brief v1" in out
    copy = Library(configured).approved_brief_path(normalise_topic(TOPIC), 1)
    assert copy.read_text(encoding="utf-8").strip() == edited.strip()


def test_an_invalid_edit_is_refused_and_the_pending_file_kept(configured, writer, capsys) -> None:
    main(["brief", TOPIC])
    broken = BRIEF.split("EXCLUDED")[0]
    _pending(configured).write_text(broken, encoding="utf-8")

    assert main(["brief", TOPIC, "--approve"]) == EXIT_FAILED

    err = capsys.readouterr().err
    assert "brief not approved: no EXCLUDED section" in err
    assert "the pending file is kept" in err
    assert _pending(configured).read_text(encoding="utf-8") == broken
    assert _approved(configured) == []


def test_re_approval_makes_a_new_version_only_when_the_text_changed(configured, writer, capsys) -> None:
    main(["brief", TOPIC])
    main(["brief", TOPIC, "--approve"])
    capsys.readouterr()

    # Unchanged, and only the line endings differ: no new version.
    _pending(configured).write_bytes(BRIEF.replace("\n", "\r\n").encode("utf-8"))
    assert main(["brief", TOPIC, "--approve"]) == EXIT_OK
    assert "already" in capsys.readouterr().out

    _pending(configured).write_text(BRIEF.replace("adverts", "advertisements"), encoding="utf-8")
    assert main(["brief", TOPIC, "--approve"]) == EXIT_OK

    rows = _approved(configured)
    assert [row.version for row in rows] == [1, 2]
    assert "advertisements" in rows[1].brief and "advertisements" not in rows[0].brief


def test_the_topic_is_keyed_as_the_ledger_keys_it(configured, writer) -> None:
    """Topics are search strings and normalised (rule 5's other half)."""
    main(["brief", TOPIC])

    assert main(["brief", "home plumbing   REPAIRS", "--approve"]) == EXIT_OK
    with Ledger(Library(configured).ledger_path) as ledger:
        assert ledger.approved_brief("HOME PLUMBING REPAIRS").version == 1
        assert ledger.approved_brief("home plumbing") is None, "a different topic"


def test_status_shows_pending_and_approved(configured, writer, capsys) -> None:
    main(["brief", TOPIC])
    capsys.readouterr()
    main(["brief", TOPIC, "--status"])
    assert "approved: none" in capsys.readouterr().out

    main(["brief", TOPIC, "--approve"])
    capsys.readouterr()
    main(["brief", TOPIC, "--status"])
    out = capsys.readouterr().out
    assert "same as the latest approval" in out
    assert "approved: v1 " in out


def test_approving_with_no_pending_brief_names_the_command(configured, capsys) -> None:
    assert main(["brief", TOPIC, "--approve"]) == EXIT_FAILED
    assert "write one with marg brief" in capsys.readouterr().err


# -- the check -----------------------------------------------------------------


@pytest.mark.parametrize(
    "edit, problem",
    [
        (lambda t: t.replace("INTENT\n", "Intent is clear.\n"), "no INTENT section"),
        (lambda t: t.replace("- explains why the fault happens, not only the fix", ""), "empty REQUIRED"),
        (lambda t: t.replace("- explains why", "- a\n- b\n- c\n- d\n- explains why"),
         "REQUIRED OF EVERY VIDEO has 5 item(s)"),
        (lambda t: t.replace("- Leaking taps: worn washers; cartridge failures\n", ""),
         "PARTS TO COVER has 1 item(s)"),
    ],
)
def test_the_check_names_what_failed(edit, problem) -> None:
    assert problem in brief_check(edit(BRIEF))
    assert brief_check(BRIEF) == ""


def test_parts_are_split_into_their_listed_things() -> None:
    assert brief_parts(BRIEF) == [
        ("Blocked drains", ["why they clog", "clearing tools (plungers, augers)", "when to call a plumber"]),
        ("Leaking taps", ["worn washers", "cartridge failures"]),
    ]
