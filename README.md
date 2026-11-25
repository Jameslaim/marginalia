# marginalia

A command-line agent that finds relevant YouTube videos, stores their transcripts on
disk, writes an understanding note for each, and answers questions from those notes
with a timestamped link per claim.

`marginalia` is a plain Python CLI that works with any OpenAI-compatible model and from
any agent or terminal; the Claude Code plugin files are optional.

> **Current status.** Discovery, notes, and cited answering all work end to end.
> `marg discover` builds a topic brief, searches YouTube, judges candidates in two
> stages, and plans each round from the previous round's gaps; `marg note` turns a
> stored transcript into a structured note; `marg ask` answers from the notes and
> checks every quoted phrase against the cited video's transcript. Speech recognition
> (ASR) is not yet implemented, so videos without usable caption tracks are recorded as
> `needs_asr`. See `PLAN.md` for the full design and `RULES.md` for the behavioral
> invariants enforced across the codebase.

---

## Features

- **Topic discovery loop (`marg discover`)** — Builds an owner-approved topic brief,
  identifies coverage gaps in your existing library, generates targeted search queries,
  screens candidates in two stages (metadata + full transcript), and stops cleanly when
  gaps are filled or exhausted.
- **Immutable transcript library** — Stores transcripts once (`transcript.txt`),
  tracks every video, run, query, and decision in SQLite (`ledger.db`), and keeps raw
  transcripts out of LLM conversation history.
- **Structured note generation (`marg note`)** — Summarizes transcripts into Markdown
  notes with timestamp alignment (`marg snap-timestamps`) and safe regeneration.
- **Cited Q&A with quote verification (`marg ask`)** — Produces answers with
  `[video_id mm:ss]` citations and deep links, then verifies every quoted passage
  against the original stored transcript.
- **Provider-agnostic LLM layer** — Works with any OpenAI-compatible
  `chat/completions` endpoint (DeepSeek, OpenAI, Groq, OpenRouter, Together, local
  Ollama/vLLM) via configuration only.
- **Dual search providers** — Uses the YouTube Data API v3 when a key is provided, and
  automatically falls back to `yt-dlp` search when no key is configured or daily quota
  is exhausted.
- **Run diagnostics (`marg run export`)** — Generates readable Markdown/TSV reports
  (`report.md`, `brief.md`, `territory.md`, `queries.tsv`, `decisions.tsv`) directly
  from the SQLite ledger.
- **Optional Claude Code integration** — Ships thin wrapper skills (`/ingest`, `/ask`)
  and a `PreToolUse` guard hook (`marg guard-write`) in `.claude-plugin/`, `skills/`,
  and `hooks/`.

---

## What to trust in an answer

Every claim carries a `[video_id mm:ss]` citation and a link that lands at that
moment. Quoted words are checked after the answer is written, against the stored
transcript of the cited video — the record, not the note — and marked where they
were not found:

- `[loosely quoted]` — the transcript holds the passage in almost these words
- `[not spoken]` — the words are in the note, not in the transcript
- `[unverified]` — no close match in either

The line under the answer reports how many quotes were checked and how many were found.
Unquoted prose is not automatically verified; the citation and link let you check the
source directly.

---

## Architecture

### The core architectural rule

**Heavy data never enters the context window.** The CLI writes transcripts, notes,
answers, and logs to disk and prints a concise summary line. Wrapper skills call
`marg` and surface only that summary, keeping per-question context cost flat as the
library grows. The one exception is `marg ask`, which prints the synthesized answer
prose (never the raw transcripts or full notes).

### Package layout

```text
src/marginalia/
├── cli.py                 CLI argument parsing and output formatting (only module that prints)
├── config.py              TOML + environment configuration loader and validator
├── contracts.py           Shared data types and domain contracts
├── llm.py                 Portable OpenAI-compatible chat/completions client & rate limiter
├── ask.py                 Question answering and note selection
├── quotes.py              Post-hoc quote verification against stored transcripts
├── brain/                 Pure decision logic (no I/O): brief, coverage, gaps, judge, planner,
│                          search_plan, selector, territory, web_context
├── core/                  Persistence & identity: ids, SQLite ledger, library paths
├── enrich/                Note generation, timestamp snapping, and marker utilities
├── flow/                  Orchestration workflows: acquire, brief, discover, doctor, export,
│                          refetch, repair, search, store, web_context
├── providers/             External integrations: youtube_api, ytdlp, ytdlp_search, chapters,
│                          host_search
└── prompts/               Bundled Markdown prompt templates (including nested judge/, plan/,
                           and territory/ prompts)
```

Structural boundaries are enforced automatically by `tests/test_boundaries.py` using
AST inspection:
- Only `providers/` may import a YouTube/transcript library.
- Only `llm.py` may name a model.
- Only `core/library.py` may construct library paths.
- `brain/` performs no I/O.
- `print` appears only in `cli.py`.

### On-disk library layout

The library lives **outside** this repository and is never committed:

```text
<library>/
├── ledger.db                  Every video, decision, run, and query
├── videos/<video_id>/
│   ├── transcript.txt         Immutable; written once, never modified
│   ├── meta.json              Video metadata
│   └── note.md                Generated note (regenerable)
├── answers/                   Saved answers from `marg ask`
├── briefs/                    Topic briefs (pending and approved versions)
├── exports/<run_id>/          On-demand readable view of a discovery run
└── logs/                      Daily DEBUG logs
```

Nothing under `answers/`, `exports/`, or `logs/` stores raw transcript or note text.

---

## Prerequisites & Dependencies

- **Python 3.11+**
- **Runtime dependencies** (installed automatically via `pyproject.toml`):
  - `yt-dlp >= 2024.1.1` (transcript fetching and fallback YouTube search)
  - `openai >= 1.40` (OpenAI-compatible LLM HTTP client)
- **Development dependencies** (`[dev]` extra):
  - `pytest >= 7.0`
- **External services**:
  - An API key and endpoint URL for any OpenAI-compatible LLM provider (required for
    discovery judging, note generation, and Q&A).
  - A YouTube Data API v3 key (optional; without one, search falls back to
    `ytdlp_search`).

---

## Installation

Clone the repository and install in editable mode inside a virtual environment:

```bash
git clone <repository-url>
cd marginalia
python -m venv .venv
```

Activate the virtual environment and install dependencies:

**Linux / macOS:**
```bash
source .venv/bin/activate
pip install -e ".[dev]"
```

**Windows (PowerShell):**
```powershell
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Verify the installation:

```bash
marg --help
```

Alternatively, you can run directly from a source checkout without installing via
`bin/marg` (POSIX) or `bin/marg.cmd` (Windows), which place `src/` on `PYTHONPATH`
automatically.

---

## Configuration

Configuration resolution order:

1. `MARGINALIA_CONFIG` environment variable
2. `./config.toml`
3. `~/.marginalia/config.toml`

If no config file exists, `marginalia` creates one from `config.example.toml` on first
run and prints its location. You can also copy `config.example.toml` manually:

```bash
cp config.example.toml config.toml
```

### Required settings

Three values must be configured to run. Everything else has a working default:

```toml
[library]
path = "~/.marginalia/library"         # 1. Default portable location (outside this repo)

[llm]
base_url = "https://api.example.com"   # 2. OpenAI-compatible API base URL (no default by design)
model = "your-model-name"              # 3. Model identifier (no default by design)
```

`base_url` and `model` intentionally have no defaults so the tool never silently sends
requests to a vendor you did not choose.

### Environment variables

See `.env.example` for all supported environment variables:

| Variable | Required | Description |
| --- | --- | --- |
| `MARGINALIA_LLM_API_KEY` | Yes (unless `llm.api_key` is set) | API key for the configured LLM provider |
| `YOUTUBE_API_KEY` | No | YouTube Data API v3 key; omit to use `ytdlp_search` |
| `MARGINALIA_CONFIG` | No | Explicit path to `config.toml` |
| `MARGINALIA_PYTHON` | No | Explicit Python 3.11+ path for `bin/marg` wrappers |

When both a config file key and an environment variable are set, the config file value
takes precedence. Setting secrets via environment variables is recommended.

### Swapping the model provider

**Config only, no code.** Change `base_url`, `model`, and your API key. `llm.py` sends
only the portable `chat/completions` baseline (system message, user message, plain text
response — no JSON mode, structured outputs, or tool calling). Provider-specific
parameters go in `[llm] extra` or `[llm.tasks.<name>] extra` and are passed through
untouched:

```toml
[llm]
extra = { temperature = 0.2 }

[llm.tasks.judge_full]
extra = { reasoning_effort = "high" }
```

Token pricing rates for cost estimates and `max_cost_usd` guards live in `[llm.pricing]`.
Verify your provider setup with:

```bash
marg llm-check
```

---

## Usage

### Quickstart workflow

```bash
# 1. Verify LLM connectivity
marg llm-check

# 2. Ingest a single video transcript
marg ingest "https://www.youtube.com/watch?v=<video_id>"

# 3. Or run a guided topic discovery loop
marg discover "how prompt caching works" --dry-run
marg discover "how prompt caching works"

# 4. Generate notes for stored videos
marg note

# 5. Ask a question grounded in your notes with timestamped citations
marg ask "What are the main trade-offs mentioned for prompt caching?"
```

*(marginalia is a pure command-line tool; there is no web or desktop GUI. Note that
speech recognition / ASR is not yet implemented: videos with no usable caption track
are recorded as `needs_asr` in the ledger and skipped until captions or ASR are
available.)*

### Example: cited answer & post-hoc quote verification (runnable offline)

You can reproduce the quote verifier offline with zero API keys using a **synthetic**
transcript/note (`demoVideo01`) and a **local mock OpenAI-compatible HTTP provider**
(`http.server` on `127.0.0.1`). Save the snippet below as `demo_quote_check.py` (or run
it via `python -c` after `pip install -e ".[dev]"`):

```python
import json, os, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from marginalia.contracts import TranscriptLine
from marginalia.core.library import Library

MOCK_REPLY = (
    'The speaker explains that "Storing transcripts on disk keeps context cost flat as the library grows" '
    '[demoVideo01 00:12]. The note also claims "verification eliminates all hallucinations completely" '
    '[demoVideo01 00:24].'
)

class MockProvider(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.dumps({
            "id": "chatcmpl-mock", "object": "chat.completion", "model": "mock-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": MOCK_REPLY}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args): pass

server = HTTPServer(("127.0.0.1", 0), MockProvider)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        lib = Library(root / "lib")
        lib.ensure()
        lib.write_transcript("demoVideo01", [
            TranscriptLine(start_ms=5_000, end_ms=11_000, text="Welcome to our short engineering note on local caching."),
            TranscriptLine(start_ms=12_000, end_ms=23_000, text="Storing transcripts on disk keeps context cost flat as the library grows."),
            TranscriptLine(start_ms=24_000, end_ms=34_000, text="Always verify quoted claims against the original transcript, not the summary."),
        ])
        lib.write_meta("demoVideo01", {"video_id": "demoVideo01", "title": "Local Caching for Research Agents", "channel": "Example Engineering", "duration_s": 34})
        lib.write_note("demoVideo01", (
            "---\nvideo_id: demoVideo01\ntitle: Local Caching for Research Agents\n"
            "channel: Example Engineering\nduration_s: 34\nprompt_version: v1\n---\n\n"
            "## Core thesis\n"
            "- Storing transcripts on disk keeps context cost flat as the library grows [demoVideo01 00:12].\n"
            "- The note paraphrases that verification eliminates all hallucinations completely.\n"
        ))
        cfg = root / "config.toml"
        cfg.write_text(
            f'[library]\npath = "{(root / "lib").as_posix()}"\n\n'
            f'[llm]\nbase_url = "http://127.0.0.1:{server.server_address[1]}/v1"\nmodel = "mock-model"\n',
            encoding="utf-8",
        )
        env = {**os.environ, "MARGINALIA_CONFIG": str(cfg), "MARGINALIA_LLM_API_KEY": "mock-key"}
        subprocess.run([sys.executable, "-m", "marginalia.cli", "ask", "Why store transcripts on disk?", "--no-save"], env=env, check=True)
finally:
    server.shutdown()
```

Run it with:

```bash
python demo_quote_check.py
```

**Observed output** (the `demoVideo01` URLs below are **synthetic** placeholder links
generated from the synthetic video ID, not real YouTube videos):

```text
The speaker explains that "Storing transcripts on disk keeps context cost flat as the library grows" [demoVideo01 00:12]. The note also claims "verification eliminates all hallucinations completely" [not spoken] [demoVideo01 00:24].

links:
  [demoVideo01 00:12]  00:12  Local Caching for Research Agents
    https://www.youtube.com/watch?v=demoVideo01&t=12s
  [demoVideo01 00:24]  00:24  Local Caching for Research Agents
    https://www.youtube.com/watch?v=demoVideo01&t=24s

ask:      1 notes, 2 citation-shaped, 2 parsed, 0 unparsed, 0 past the end of their video, 1 requests, $0.0010, 2s, v9; scope: the whole library (no --topic)
          grounding: 1 of 2 quote(s) found in the cited transcript; 0 sentence(s) and 0 phrase(s) not found, 1 the note's words not the speaker's, 0 a note's translation, 0 id(s) name no loaded video
          ~1,790 input tokens
```

*(Stderr also logs: `WARNING marginalia.ask: ask: 1 quoted string(s) are in the cited note and not in the transcript — the note's words, not a speaker's`.)*

Here, the first quote matches the stored transcript verbatim and passes verification,
while the second quote — present only in the note's prose — is flagged `[not spoken]`
and counted in the grounding summary. The check verifies **quoted wording** against the
stored transcript; unquoted prose is not checked, and the timestamped link lets you
verify any claim directly.

### Command reference

```text
marg ingest <url>                  Ingest one video transcript into the library
marg search "<q>" [--limit N]      Search for candidates (--topic T, --json)
marg discover "<topic>"            Run the discovery loop (--dry-run, --max-rounds/videos/requests)
marg brief "<topic>"               Write, review, approve, or inspect a topic brief
marg note [<video_id>...]          Generate notes (--limit N, --regenerate [--all], --missing-only, --status)
marg refetch                       Retry transient fetch failures (--limit N, --dry-run)
marg snap-timestamps [<id>...]     Align timestamps in older notes (--dry-run)
marg ask "<question>"              Answer from notes (--topic T, --deep <id>, --json, --no-save)
marg run export <run_id>           Export a readable view of a discovery run to exports/<run_id>/
marg doctor                        Compare disk state against ledger.db (read-only; --json)
marg reconcile [<id>...]           Record transcripts left on disk by an interrupted store
marg llm-check                     Send one test request to the configured LLM provider
marg status <url|id>               Show ledger state for one video
marg list [--status S]             List videos by asset state (default: has_transcript)
marg guard-write                   Internal PreToolUse hook (reads stdin)
```

### Diagnosing a discovery run

`marg run export <run_id>` generates an inspectable folder from `ledger.db`:

```text
exports/<run_id>/
├── report.md          Round-by-round summary
├── brief.md           Topic brief used for the run
├── territory.md       Topic map revisions, diffs, and reasons
├── queries.tsv        Executed queries, origins, filters, results, and quota
└── decisions.tsv      Per-video stage, verdict, evidence, and rationale
```

Run `marg run export` with an unknown ID to list recent runs.

### Claude Code plugin usage (optional)

When loaded as a Claude Code plugin, two skills wrap the CLI:
- `/ingest` — ingests a URL or runs discovery for a topic
- `/ask` — asks a question against your notes

`hooks/hooks.json` registers `marg guard-write` as a `PreToolUse` hook to prevent
direct `Write`/`Edit` operations inside the CLI-owned library directory.

---

## Testing

Run the full test suite (all tests are offline and require no API keys):

```bash
pytest -q
```

Start with `tests/test_boundaries.py` to see how architectural invariants from
`RULES.md` are automatically verified against the source tree.

---

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the full license text.
