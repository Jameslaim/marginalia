"""The structural rules, enforced rather than stated.

Every rule in `RULES.md` that has actually held in this project held because a
test enforced it. The ones written down and left unchecked drifted. So each rule
that describes the *shape* of the package gets a test here, and the tests read
the source with `ast` rather than importing it — an import-time check can only
see what a module happened to import, while an AST scan sees every import
statement including the lazy ones inside functions.

There are no allowlisted violations. There were two — `brain/judge.py`
reaching `core/ledger.py` for the verdict words, and `config.py` holding a
default model name — and both are closed rather than documented. Where an
exemption is genuinely needed it should be a named constant with the reason
beside it and a test pinning its exact contents, so it stays one line long and
cannot quietly grow a second entry.
"""

from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "marginalia"


def _modules() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def _imports(path: Path) -> list[str]:
    """Every module named by an import in this file, absolute where possible.

    Relative imports are resolved against the file's own package so that
    ``from ..core.ledger import X`` inside `brain/judge.py` reads as
    ``marginalia.core.ledger`` — otherwise a boundary check would have to match
    on dots and would miss the same import written from a different depth.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parts = ["marginalia", *path.relative_to(SRC).with_suffix("").parts]
    if parts[-1] == "__init__":
        parts.pop()
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                # level 1 is this module's package, level 2 its parent, ...
                prefix = parts[: len(parts) - node.level]
                base = ".".join([*prefix, node.module] if node.module else prefix)
            names.append(base)
            names.extend(f"{base}.{alias.name}" for alias in node.names)
    return names


def _module_paths() -> list[str]:
    return [_rel(p) for p in _modules()]


# -- rule: only providers/ imports a YouTube library -------------------------

_TRANSCRIPT_LIBRARIES = ("yt_dlp", "youtube_transcript_api", "pytube", "youtubei")


@pytest.mark.parametrize("relative", _module_paths())
def test_only_providers_imports_a_youtube_library(relative: str) -> None:
    """Rule 4. YouTube will break one of these; replacing it must mean adding
    one file under `providers/`, never a refactor that reaches upward."""
    if relative.startswith("providers/"):
        return
    for name in _imports(SRC / relative):
        root = name.split(".")[0]
        assert root not in _TRANSCRIPT_LIBRARIES, (
            f"{relative} imports {name!r}; only providers/ may know which source ran"
        )


def test_a_provider_does_import_one() -> None:
    """Guard on the guard: if nothing imports yt-dlp any more the scan above
    passes vacuously and stops meaning anything."""
    found = any(
        any(name.split(".")[0] in _TRANSCRIPT_LIBRARIES for name in _imports(p))
        for p in _modules()
        if _rel(p).startswith("providers/")
    )
    assert found, "no provider imports a transcript library — the scan is now vacuous"


# -- rule: print appears only in cli.py --------------------------------------


@pytest.mark.parametrize("relative", _module_paths())
def test_only_cli_prints(relative: str) -> None:
    """Rule: `print` appears only in `cli.py`; everything else uses `logging`.

    This is the enforcement arm of the hard rule about transcript contents. A
    `print` in `note.py` is one debugging session away from putting a transcript
    in the conversation, and the cost curve that avoids is the whole reason
    marginalia is a CLI.
    """
    if relative == "cli.py":
        return
    tree = ast.parse((SRC / relative).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]
    assert not calls, (
        f"{relative} calls print() at line {calls[0].lineno}; use logging outside cli.py"
    )


# -- rule: only llm.py names a model -----------------------------------------

_MODEL_FAMILIES = (
    "claude",
    "gpt-",
    "chatgpt",
    "gemini",
    "llama",
    "glm",
    "kimi",
    "deepseek",
    "mistral",
    "qwen",
    "anthropic",
    "openai",
    "moonshot",
)

# One file. `config.py` used to be here too, because it held
# DEFAULT_LLM_BASE_URL and DEFAULT_LLM_MODEL — a defensible exception that was
# still an exception, and the default it protected named a provider that had
# stopped completing requests. Both keys are required now, so rule 7 reads
# literally: `llm.py` is the only place a model is named.
_MAY_NAME_A_MODEL = frozenset({"llm.py"})


def _code_only(source: str) -> str:
    """The file with every docstring and comment removed.

    Prose is not a dependency. `timestamps.py` records how two named models did
    on the same prompt, and `ask.py` explains why it runs in Python rather than in a chat session;
    deleting either to satisfy a grep would delete the evidence and the reason.
    What rule 7 is actually about is a model name reaching the *request*, and
    that only happens in code. `ast.unparse` drops comments for free.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body[0].value.value = ""
    return ast.unparse(tree)


@pytest.mark.parametrize("relative", _module_paths())
def test_only_llm_names_a_model(relative: str) -> None:
    if relative in _MAY_NAME_A_MODEL:
        return
    lowered = _code_only((SRC / relative).read_text(encoding="utf-8")).lower()
    for family in _MODEL_FAMILIES:
        assert family not in lowered, f"{relative} names a model family: {family!r}"


@pytest.mark.parametrize("relative", _module_paths())
def test_no_module_binds_a_base_url_to_a_literal(relative: str) -> None:
    """The other half of rule 7: where a request is *sent* is as much a provider
    choice as which model answers it.

    Reading `config.llm.base_url` — cli.py does, to name the endpoint in an
    error — is fine and is the point. What must not exist anywhere, `config.py`
    now included, is a `base_url` bound to a literal: that is a provider chosen
    in code rather than in the file the user edits.
    """
    if relative in _MAY_NAME_A_MODEL:
        return
    tree = ast.parse((SRC / relative).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.keyword) and node.arg == "base_url":
            target = node.value
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "base_url" for t in node.targets
        ):
            target = node.value
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            raise AssertionError(
                f"{relative}:{node.lineno} binds base_url to a literal "
                f"{target.value!r}; the endpoint comes from config"
            )


def test_the_model_name_allowlist_is_one_file() -> None:
    """If a second file starts naming a model, that is the drift rule 7 exists
    to catch, and it should fail here rather than pass because the list grew."""
    assert _MAY_NAME_A_MODEL == {"llm.py"}


# -- rule: only core/library.py builds a library path ------------------------

# `cli.py` reads `config.library_path` once per command to construct a
# `Library`. That is the sanctioned handoff, not path arithmetic: the root goes
# in, and every path under it is built by `core/library.py`.
_MAY_READ_THE_LIBRARY_ROOT = frozenset({"cli.py", "config.py"})


@pytest.mark.parametrize("relative", _module_paths())
def test_only_library_builds_a_library_path(relative: str) -> None:
    if relative in _MAY_READ_THE_LIBRARY_ROOT:
        return
    source = (SRC / relative).read_text(encoding="utf-8")
    assert "library_path" not in source, (
        f"{relative} reads the configured library root; take a Library instead "
        f"— core/library.py builds every path under it"
    )


def test_cli_never_joins_onto_the_library_root() -> None:
    """cli.py may *read* the root — it constructs the `Library`, and the
    `guard-write` hook needs it to test containment — but it must never join a
    name onto it. The moment it does, a second module knows the folder layout.
    """
    tree = ast.parse((SRC / "cli.py").read_text(encoding="utf-8"))

    def names_the_root(node: ast.AST) -> bool:
        return any(
            isinstance(child, ast.Attribute) and child.attr == "library_path"
            for child in ast.walk(node)
        )

    reads = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "library_path"]
    assert reads, "cli.py no longer reads the library root — this scan is vacuous"

    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and names_the_root(node):
            raise AssertionError(f"cli.py:{node.lineno} joins a path onto the library root")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"joinpath", "join"}
            and names_the_root(node)
        ):
            raise AssertionError(f"cli.py:{node.lineno} joins a path onto the library root")


# -- rule: brain/ performs no infrastructure I/O -----------------------------

_INFRASTRUCTURE = (
    "pathlib",
    "sqlite3",
    "socket",
    "urllib",
    "http",
    "os",
    "shutil",
    "subprocess",
    "marginalia.providers",
    "marginalia.core",
    "marginalia.flow",
    "marginalia.enrich",
)


def _brain_modules() -> list[str]:
    return [r for r in _module_paths() if r.startswith("brain/")]


@pytest.mark.parametrize("relative", _brain_modules())
def test_brain_performs_no_infrastructure_io(relative: str) -> None:
    """`brain/` reasons and returns decisions; `flow/` executes them.

    This is the new boundary and the one that matters most. `territory.py` and
    `planner.py` arrive here later, and the value of a reasoning layer is that
    it can be tested without a machine — no database, no disk, no network. That
    property is worth exactly as much as it is enforced.

    **No allowlist.** There was one: `judge.py` reached `core/ledger.py` for the
    three VERDICT_* strings, which pulled sqlite3 into every process that
    imported the judge. The constants now live in `contracts.py`, where the
    vocabulary brain, flow and core all speak belongs. Nothing here is exempt,
    and nothing here should become exempt — an allowlist on this rule is the
    rule ending.
    """
    offences = set()
    for name in _imports(SRC / relative):
        for forbidden in _INFRASTRUCTURE:
            if name == forbidden or name.startswith(forbidden + "."):
                offences.add((relative, forbidden))
    assert not offences, (
        f"{relative} reaches infrastructure: {sorted(offences)}. brain/ may import "
        f"llm and contracts, and nothing else that touches the outside world."
    )


def test_brain_may_import_llm_and_contracts() -> None:
    """The positive half: the rule is a permission, not only a prohibition. If
    `judge.py` stopped importing `llm` this would be a different package."""
    names = _imports(SRC / "brain" / "judge.py")
    assert "marginalia.llm" in names
    assert "marginalia.contracts" in names


# -- rule: no bare dicts across a boundary -----------------------------------


def test_every_shared_structure_is_in_contracts() -> None:
    """The types named as crossing a boundary live in `contracts.py`.

    Not a scan — an explicit list, because "does this cross a boundary?" is a
    judgement and a scan would have to guess at it. Adding a shared type means
    adding a line here, which is the prompt to ask whether it belongs.
    """
    from marginalia import contracts

    expected = {
        # asset state
        "STATUS_SEEN",
        "STATUS_PENDING",
        "STATUS_HAS_TRANSCRIPT",
        "STATUS_NEEDS_ASR",
        "STATUS_FAILED",
        "STATUS_DUPLICATE",
        "VIDEO_STATUSES",
        # judgement evidence
        "EVIDENCE_CLEAR",
        "EVIDENCE_DEGRADED",
        "RELEVANT_NONE",
        "RELEVANT_UNAVAILABLE",
        # query provenance
        "ORIGIN_BRIEF",
        "ORIGIN_TERRITORY",
        "ORIGIN_PLANNER",
        "ORIGIN_THREAD",
        # search filters — the vocabulary the model writes, the validator
        # reads and the provider sends. Shared three ways, so it is here.
        "FILTER_PUBLISHED_AFTER",
        "FILTER_ORDER",
        "FILTER_RELEVANCE_LANGUAGE",
        "FILTER_VIDEO_DURATION",
        "FILTER_FIELDS",
        "HARD_FILTERS",
        "ORDER_VALUES",
        "VIDEO_DURATION_VALUES",
        "SearchFilters",
        # territory
        "Recency",
        "PlannedQuery",
        "FILTER_FIELD_QUERY",
        "FILTER_FIELD_RECENCY_WAIVER",
        "RECENCY_DERIVED_WHY",
        "FilterRejection",
        "MapSnapshot",
        "TerritoryRevision",
        "TerritoryMap",
        # planning
        "RECOMMEND_CONTINUE",
        "RECOMMEND_STOP",
        "REVISE_ADD",
        "REVISE_REMOVE",
        "REVISE_CONCEPT",
        "REVISE_VOCABULARY",
        "JudgedVideo",
        "CoverageItem",
        "LibraryCoverage",
        "RoundPlan",
        # verdict vocabulary
        "VERDICT_YES",
        "VERDICT_NO",
        "VERDICT_MAYBE",
        # transcripts and search
        "TranscriptLine",
        "TranscriptResult",
        "SearchResult",
        # judgement
        "Brief",
        "MetadataVerdict",
        "FullVerdict",
        # ledger rows
        "VideoRow",
        # discovery runs
        "RoundReport",
        "DiscoverRun",
        # search runs
        "NEW",
        "KNOWN",
        "REJECTED",
        "STORED_THIS_RUN",
        "Candidate",
        "SearchRun",
        # note runs
        "NOTED",
        "SKIPPED",
        "FAILED",
        "STOP_DONE",
        "STOP_MAX_COST",
        "STOP_LIMIT",
        "STOP_REPEATED_FAILURES",
        "STOP_REFUSED",
        # refetch: four outcomes, because "it failed again", "it has no
        # captions" and "it is gone" are three different findings and only one
        # means try again tomorrow.
        "RECOVERED",
        "STILL_FAILING",
        "NO_CAPTIONS",
        "GONE",
        "STOP_THROTTLED",
        # M7. The selector's vocabulary: `all` fit, `selected` was chosen,
        # `fallback` means the selector failed and the budget was filled anyway.
        "SELECTION_ALL",
        "SELECTION_SELECTED",
        "SELECTION_FALLBACK",
        "NoteResult",
        "NoteRun",
        # answers
        "AnswerCitation",
        "CitationScan",
        "RefetchResult",
        "RefetchRun",
        "LoadedNote",
        # M7. One line per note, built in `ask.py` and read by `brain/selector.py`.
        "NoteSummary",
        "Selection",
        "Opened",
        "Answer",
        "Grounding",
        # timestamp snapping
        "TranscriptIndex",
        "NoteCitation",
        "Change",
        "SnapResult",
        # model calls
        "Usage",
        # run exports
        "ExportedRun",
        # doctor: seven checks, seven counters, and the report they land in
        "CHECK_UNTRACKED_DIR",
        "CHECK_TRANSCRIPT_MISSING",
        "CHECK_NOTE_MISSING",
        "CHECK_NOTE_UNRECORDED",
        "CHECK_NOTE_INVALID",
        "CHECK_TRANSCRIPT_MODIFIED",
        "CHECK_RUN_UNFINISHED",
        "DOCTOR_CHECKS",
        "DOCTOR_PROBLEMS",
        "DoctorFinding",
        "DoctorReport",
    }
    assert set(contracts.__all__) == expected
    for name in expected:
        assert hasattr(contracts, name)


def test_the_two_citation_types_are_distinguishable() -> None:
    """They were both called `Citation`, in two files, and nothing said which
    one a function returned. Names now carry the difference, and the fields
    confirm it is a real one rather than a naming accident."""
    from marginalia.contracts import AnswerCitation, NoteCitation

    answer = {f.name for f in fields(AnswerCitation)}
    note = {f.name for f in fields(NoteCitation)}
    assert answer != note
    assert "video_id" in answer and "video_id" not in note
    assert "start" in note and "start" not in answer


def test_no_shared_structure_is_defined_twice() -> None:
    """A type in `contracts.py` must not also be class-defined elsewhere — that
    is how two `Citation`s happened here, and it was invisible until something
    compared them with `isinstance`."""
    from marginalia import contracts

    shared = set(contracts.__all__)
    for path in _modules():
        if _rel(path) == "contracts.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        clash = defined & shared
        assert not clash, f"{_rel(path)} redefines a contract type: {sorted(clash)}"


def test_protocols_and_errors_stayed_in_providers() -> None:
    """Deliberately *not* moved. A Protocol is the shape of an implementation,
    not a structure in flight, and it belongs beside the implementations it
    constrains."""
    from marginalia import contracts
    from marginalia.providers import base

    for name in ("TranscriptProvider", "SearchProvider", "PermanentError", "TransientError"):
        assert hasattr(base, name)
        assert not hasattr(contracts, name), f"{name} is a protocol or error; it stays in providers/"
