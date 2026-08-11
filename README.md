# marginalia

A Claude Code plugin that finds relevant YouTube videos, extracts their transcripts,
writes an understanding note for each, and answers questions about them with
timestamps.

**This is Milestone 1: the foundation.** It ingests one URL end to end. There is no
search, no relevance judging, no ASR, no notes, and no `/ask` yet — see `PLAN.md` for
the build order.

## The architectural rule

Heavy data never enters the context window. Scripts write transcripts to disk and
return a short report. Skills are thin wrappers that call the CLI and read back that
report. `CLAUDE.md` states the rules that keep it that way.

## Install

Requires Python 3.11+.

```bash
pip install -e ".[dev]"
```

Or run from the source tree without installing — `bin/marg` puts `src/` on
`PYTHONPATH` itself. It looks for an interpreter in this order: `MARGINALIA_PYTHON`,
a `.venv/` at the project root, then `python3`/`python` on PATH. Each candidate is
tested by running it, so Windows' Microsoft Store alias stubs — which answer
`command -v` but are not interpreters — are skipped rather than used.

## Configure

```bash
cp config.example.toml config.toml
```

Resolution order: `MARGINALIA_CONFIG` → `./config.toml` → `~/.marginalia/config.toml`.
If none exists, one is created from `config.example.toml` on first run and the CLI
says where.

```toml
[library]
path = "C:/youtube-agent/marginalia-library"

[providers]
transcript_order = ["ytdlp"]
```

The library lives outside the repository and is never committed.

## Use

```bash
marg ingest https://www.youtube.com/watch?v=dQw4w9WgXcQ
marg status dQw4w9WgXcQ
marg list
```

Output is one line per video, meant to be read by a skill:

```
stored    dQw4w9WgXcQ  "Prompt caching explained"  412 lines  captions/en
skipped   dQw4w9WgXcQ  already stored
needs-asr dQw4w9WgXcQ  "Some video"  no captions available
failed    dQw4w9WgXcQ  transient: connection timeout (attempt 2)
```

Exit codes: `0` success, skip, or needs-asr; `1` fetch failure; `2` bad input or
unusable config.

Inside Claude Code, `/marginalia:ingest <url>` does the same thing.

## What gets stored

```
<library_root>/
├── ledger.db
└── videos/
    └── <video_id>/
        ├── transcript.txt     immutable, one line per caption
        └── meta.json
```

```
[00:00:01.500 --> 00:00:18.200] So let's suppose that you're working with your coding agent
[00:00:18.200 --> 00:00:35.400] Are you charged full price for those tokens?
```

## Ledger

SQLite. Every video ID ever seen and its verdict: `pending`, `needs_asr`, `stored`,
`rejected`, `failed`, `duplicate`. Rows are never deleted, even when data is —
otherwise the agent re-downloads tomorrow what it discarded today.

The row is written **before** the network call, so a crash leaves a visible
`pending` rather than no trace at all. Re-running on a `pending` row re-attempts it;
re-running on a `stored` row does nothing and says so.

A video with no captions is not a failure. It is marked `needs_asr` and waits for the
transcription queue (M4).

## Tests

```bash
pytest
```

`test_ids.py` covers every URL form and the case-sensitivity rule. `test_ledger.py`
covers state transitions and write ordering. `test_parsers.py` pins all five caption
serialisation formats to static fixtures in `tests/fixtures/`, since those parsers
otherwise run for the first time on the day YouTube changes format.

No test touches the network, the real library, or yt-dlp.
