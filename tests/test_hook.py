"""The PreToolUse guard: no direct writes into the library.

`CLAUDE.md` rule 2 was instruction-only until now, and it is the rule most
likely to be broken *helpfully* — by an agent that decides editing a meta.json
by hand is simpler than adding a CLI flag. The ledger and the files on disk
would then disagree, silently, and `transcript.txt` is immutable.

Exit 2 blocks the tool call. Anything else lets it through, which is why the
fail-open cases below matter as much as the blocking ones: a guard that dies on
unexpected input must not take every edit in the repository with it.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from marginalia.cli import EXIT_BLOCKED, EXIT_OK, main


@pytest.fixture()
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A config pointing at a temporary library, found the way the CLI finds it."""
    root = tmp_path / "library"
    config = tmp_path / "config.toml"
    config.write_text(
        f'[library]\npath = "{root.as_posix()}"\n', encoding="utf-8"
    )
    monkeypatch.setenv("MARGINALIA_CONFIG", str(config))
    return root


def guard(event: dict, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    return main(["guard-write"])


def write_event(path: Path | str, tool: str = "Write", **extra) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {"file_path": str(path), **extra},
        "cwd": "C:/youtube-agent/projects/marginalia",
    }


# -- blocked ----------------------------------------------------------------


def test_a_write_into_the_library_is_blocked(
    library: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    code = guard(write_event(library / "videos" / "dQw4w9WgXcQ" / "meta.json"), monkeypatch)

    assert code == EXIT_BLOCKED
    message = capsys.readouterr().err
    assert "blocked" in message
    assert "marg" in message


def test_editing_a_stored_transcript_is_blocked(
    library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transcripts are immutable. Acquisition is the fragile step; everything
    else in the system is regenerable from what was fetched once."""
    event = write_event(library / "videos" / "dQw4w9WgXcQ" / "transcript.txt", tool="Edit")
    assert guard(event, monkeypatch) == EXIT_BLOCKED


def test_writing_the_ledger_is_blocked(library: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert guard(write_event(library / "ledger.db"), monkeypatch) == EXIT_BLOCKED


def test_the_library_root_itself_is_blocked(
    library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert guard(write_event(library), monkeypatch) == EXIT_BLOCKED


def test_a_relative_path_is_resolved_against_cwd(
    library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = write_event("videos/dQw4w9WgXcQ/meta.json")
    event["cwd"] = str(library)
    assert guard(event, monkeypatch) == EXIT_BLOCKED


def test_a_path_that_climbs_back_in_is_blocked(
    library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`..` is normalised before comparison, so the obvious dodge does not work."""
    event = write_event(library / "videos" / ".." / "ledger.db")
    assert guard(event, monkeypatch) == EXIT_BLOCKED


def test_a_notebook_write_is_blocked(library: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    event = {
        "tool_name": "NotebookEdit",
        "tool_input": {"notebook_path": str(library / "notes.ipynb")},
        "cwd": ".",
    }
    assert guard(event, monkeypatch) == EXIT_BLOCKED


@pytest.mark.skipif(
    os.path.normcase("A") != "a", reason="only meaningful where paths are case-insensitive"
)
def test_case_differences_do_not_get_past_it(
    library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows `C:\\Lib` and `c:/lib` are the same directory, so a guard
    that compares raw strings can be walked straight past."""
    shouted = str(library / "ledger.db").upper()
    assert guard(write_event(shouted), monkeypatch) == EXIT_BLOCKED


# -- allowed ----------------------------------------------------------------


def test_a_write_elsewhere_is_allowed(library: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert guard(write_event("C:/youtube-agent/projects/marginalia/PLAN.md"), monkeypatch) == EXIT_OK


def test_a_sibling_directory_with_a_shared_prefix_is_allowed(
    library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.../library-backup` is not inside `.../library`, and a prefix compare
    that misses this blocks writes nobody asked it to."""
    sibling = library.with_name(library.name + "-backup") / "notes.md"
    assert guard(write_event(sibling), monkeypatch) == EXIT_OK


# -- fails open -------------------------------------------------------------


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"tool_name": "Write"},
        {"tool_name": "Write", "tool_input": None},
        {"tool_name": "Bash", "tool_input": {"command": "ls"}},
        {"tool_name": "Write", "tool_input": {"file_path": ""}},
    ],
)
def test_input_it_cannot_evaluate_is_let_through(
    library: Path, monkeypatch: pytest.MonkeyPatch, event: dict
) -> None:
    """The narrow rule this enforces is worth less than the session. A guard
    that blocks every edit because it could not parse its own input is a worse
    failure than the one it prevents."""
    assert guard(event, monkeypatch) == EXIT_OK


def test_malformed_json_is_let_through(
    library: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("not json at all"))
    assert main(["guard-write"]) == EXIT_OK
    assert "guard-write" in capsys.readouterr().err


# -- the wiring the hook depends on ----------------------------------------


def test_hooks_json_is_valid_and_points_at_the_cli() -> None:
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "hooks" / "hooks.json").read_text(encoding="utf-8"))

    entries = config["hooks"]["PreToolUse"]
    assert entries, "no PreToolUse hook registered"
    matcher = entries[0]["matcher"]
    for tool in ("Write", "Edit"):
        assert tool in matcher

    command = entries[0]["hooks"][0]["command"]
    assert "guard-write" in command
    assert "bin/marg" in command
    assert (root / "bin" / "marg").is_file()
