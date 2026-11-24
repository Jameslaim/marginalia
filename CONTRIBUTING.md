# Contributing to marginalia

Thank you for your interest in contributing to `marginalia`.

## Before you edit

Read `RULES.md` before changing behavior. Each rule documents a failure that was
measured on this system, and many structural rules are enforced automatically by
`tests/test_boundaries.py`.

Key architectural boundaries (enforced by AST tests):

- Only `src/marginalia/providers/` may import a YouTube/transcript library.
- Only `src/marginalia/llm.py` may name a model or talk to the LLM client.
- Only `src/marginalia/core/library.py` may construct library paths.
- `src/marginalia/brain/` must remain pure decision logic with no I/O.
- `print()` may appear only in `src/marginalia/cli.py`.

## Development setup

Requires Python 3.11+.

```bash
git clone <repository-url>
cd marginalia
python -m venv .venv
source .venv/bin/activate   # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

## Running tests

The test suite is fully offline and requires no API keys:

```bash
pytest -q
```

To run just the architectural boundary checks:

```bash
pytest -q tests/test_boundaries.py
```

## Pull request guidelines

- Keep changes focused and include offline regression tests for bug fixes or new behavior.
- Ensure `pytest -q` passes locally (including `tests/test_boundaries.py`).
- Never commit `config.toml`, `.env`, API keys, or local library data (`ledger.db`, transcripts, notes).
- If you add or edit a prompt under `src/marginalia/prompts/`, bump its `version:` frontmatter field.
