# marginalia

A command-line agent that finds relevant YouTube videos, stores their transcripts on
disk, writes an understanding note for each, and answers questions from those notes
with a timestamped link per claim.

**Where it is.** Discovery, notes and answering all work. `marg discover` maps a
subject, searches it, judges candidates in two stages and plans each round from the
last one's results; `marg note` turns a stored transcript into a note; `marg ask`
answers from every note at once. There is no ASR yet, so a video with no caption
track is recorded as `needs_asr` and left. `PLAN.md` has the full design and the
build order; `RULES.md` has the rules that must not drift.

## The architectural rule

**Heavy data never enters the context window.** The CLI writes transcripts, notes,
answers and logs to disk and returns one line. Skills are thin wrappers that call
`marg` and print that line. This is the entire reason marginalia is a command-line
tool rather than something the model drives directly — it is what keeps the cost of
a question flat as the library grows.

The one exception is `marg ask`, which prints the answer itself. That is prose
written *from* the notes, not the notes.

## Install

Requires Python 3.11+.

```bash
pip install -e ".[dev]"
```

Or run from the source tree without installing: `bin/marg` puts `src/` on
`PYTHONPATH` itself.

## Configure

Resolution order: `MARGINALIA_CONFIG` → `./config.toml` → `~/.marginalia/config.toml`.
If none exists, one is created from `config.example.toml` on first run and the CLI
says where.

**Three values are needed to run.** Everything else has a working default.

```toml
[library]
path = "C:/youtube-agent/marginalia-library"   # 1. where everything is stored

[llm]
base_url = "https://api.example.com"           # 2. no default, on purpose
model = "some-model"                           # 3. no default, on purpose
```

The API key is read from `llm.api_key` or, preferably, the `MARGINALIA_LLM_API_KEY`
environment variable.

`base_url` and `model` deliberately have no defaults. Any default is a vendor chosen
on the user's behalf, silently — and that is not hypothetical here: a default once
pointed at a provider this project had already abandoned for not completing
requests. A missing key now names itself, its section and the file.

A YouTube Data API key is optional. Set `providers.youtube_api_key` or
`YOUTUBE_API_KEY` for the official search; without one the chain falls through to
`ytdlp_search`, and the report says so every time.

## Where things live

The library lives outside the repository and is never committed:

```
<library>/
├── ledger.db                  every video, decision, run and query
├── videos/<video_id>/
│   ├── transcript.txt         immutable; written once, never modified
│   ├── meta.json
│   └── note.md                overwritten freely by a better prompt
├── answers/                   one file per question `marg ask` answered
├── exports/<run_id>/          a readable view of one discovery run
└── logs/                      one file per day, at DEBUG
```

**The library is CLI-owned.** A `PreToolUse` hook runs `marg guard-write` and blocks
any direct `Write` or `Edit` aimed inside it, because the ledger and the files on
disk have to stay in agreement. Add a command or a flag instead.

Nothing under `answers/`, `exports/` or `logs/` ever contains transcript or note
text. An answer names the notes it was written from; an export renders the ledger; a
log says which transcript was opened and never what was in it.

## Commands

```
marg ingest <url>                  ingest one video
marg search "<q>" [--limit N]      find candidates; --topic T, --json
marg discover "<topic>"            the loop; --dry-run, --max-rounds/videos/requests
marg note [<video_id>...]          write notes; --limit N, --regenerate [--all], --status
marg snap-timestamps [<id>...]     align cited timestamps to the transcript; --dry-run
marg ask "<question>"              answer from every note; --deep <id>, --json,
                                   --no-save (saving to answers/ is the default)
marg run export <run_id>           write exports/<run_id>/ from the ledger
marg llm-check                     one request to the configured provider
marg status <url|id>               show ledger state for one video
marg list [--status S]             list videos by asset state (default has_transcript)
marg guard-write                   internal: the PreToolUse hook, reads stdin
```

Two skills wrap these: `/ingest` (a URL or a topic) and `/ask` (a question).

### Diagnosing a run

`marg run export <run_id>` writes a folder you can read yourself or hand to a model:

```
exports/<run_id>/
├── report.md          what happened, round by round
├── brief.md           what it decided the topic meant
├── territory.md       the map, its revisions, with diffs and reasons
├── queries.tsv        query, origin, filters, results, quota
└── decisions.tsv      video, stage, verdict, evidence, reason
```

Generated on demand only. The ledger stays the single source of truth and this is a
view of it — safe to delete, safe to regenerate. `marg run export` with an unknown ID
lists the recent runs.

## Swapping the model provider

**Config only, no code.** Change `base_url`, `model` and the API key and nothing
else. The provider has been replaced twice already, and only the second swap was
genuinely config-only — the first was broken by a single hardcoded `temperature=0`,
because the incoming model refused any temperature but its own default.

That is why `llm.py` sends only the portable baseline: `chat/completions`, a system
message, a user message, plain text back. No JSON mode, no structured outputs, no
tool calling. Anything a specific provider wants goes in `[llm] extra` or
`[llm.tasks.<name>] extra` and is passed through without the code knowing what it
means:

```toml
[llm]
extra = { temperature = 0.2 }

[llm.tasks.judge_full]
extra = { reasoning_effort = "high" }
```

Rates for cost estimates live in `[llm.pricing]`, because the next provider's will
differ. One model everywhere: what varies per task is thinking effort, and that is
config.

`marg llm-check` sends one request and reports what came back.

## Tests

```bash
pytest -q
```

`tests/test_boundaries.py` is the one to read first. The structural rules in
`RULES.md` are enforced there by scanning the source rather than stated and hoped
for — only `providers/` may import a YouTube library, only `llm.py` may name a model,
only `core/library.py` may build a library path, `brain/` may touch no I/O at all,
and `print` appears only in `cli.py`.
