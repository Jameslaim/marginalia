# marginalia

A Claude Code plugin that finds relevant YouTube videos, extracts their transcripts,
writes an understanding note for each, and answers questions about them with
timestamps.

**This is Milestone 2a.** It ingests one URL end to end, and it searches for candidate
videos and says which ones it already knows about. It does **not** yet judge whether a
video is any *good* — that needs a model and arrives in M2b, along with the relevance
brief and the discovery loop. There is no ASR, no notes, and no `/ask` yet. See
`PLAN.md` for the build order.

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
search_order = ["youtube_api", "ytdlp_search"]
# youtube_api_key = ""        # or set YOUTUBE_API_KEY; the file wins
# youtube_daily_units = 10000 # set to 0 to force the overflow provider
```

The library lives outside the repository and is never committed.

A YouTube Data API key is optional. Without one, `youtube_api` reports itself
unavailable and the chain falls through to `ytdlp_search` — saying so in the report,
every time.

## Use

```bash
marg ingest https://www.youtube.com/watch?v=dQw4w9WgXcQ
marg search "prompt caching" --limit 25
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

## Search

```
new       dQw4w9WgXcQ  "Prompt caching explained"      18m  Some Channel
new       xK2mPq9dRtY  "Agent cost control"            42m  Another Channel
known     9fLmZz4kWpQ  already stored
rejected  aB3cD4eF5gH  no for this topic (2026-08-09)
24 found, 3 known, 1 previously rejected, 20 new — youtube_api
```

The verdict is always the first token. Two gates run, both free, both before anything
is spent:

1. **known ID** — already `stored` or `duplicate` in the ledger
2. **rejection** — a `no` verdict recorded for *this exact topic*

`--topic T` separates the search string from the judgement key, so the same query can
be run against a different enquiry and re-evaluate everything. It defaults to the
query. Topics are lowercased and whitespace-collapsed; video IDs never are.

Every candidate, kept or dropped, gets a row in the `decisions` table. `--json` emits
the same data structured, including the descriptions the terse form omits.

Searching is metered. One `search.list` call costs 100 units against a 10,000/day
default; spend is tracked locally per UTC date and checked *before* the request, so a
would-be overspend falls through to `ytdlp_search` rather than collecting an opaque
403.

## The library is CLI-owned

`hooks/hooks.json` registers a `PreToolUse` hook that blocks `Write` and `Edit` aimed
anywhere inside the configured library path. The ledger and the files on disk have to
stay in agreement, and `transcript.txt` is immutable — so the rule is enforced rather
than merely written down. The guard fails **open**: input it cannot parse is let
through, because blocking every edit in the repository is a worse failure than the one
it prevents.

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

SQLite, three tables: `videos`, `decisions`, `quota`.

`videos` holds every video ID ever seen and its status: `pending`, `needs_asr`,
`stored`, `rejected`, `failed`, `duplicate`. Rows are never deleted, even when data is —
otherwise the agent re-downloads tomorrow what it discarded today.

`decisions` logs every verdict with the stage that produced it — not only the negative
ones, because a rejection log alone cannot answer "why did the judge keep this?" months
later, and none of it can be reconstructed retroactively.

The row is written **before** the network call, so a crash leaves a visible
`pending` rather than no trace at all. Re-running on a `pending` row re-attempts it;
re-running on a `stored` row does nothing and says so.

A video with no captions is not a failure. It is marked `needs_asr` and waits for the
transcription queue (M4).

## Tests

```bash
pytest
```

| File | Covers |
|------|--------|
| `test_ids.py` | every URL form, and the case-sensitivity rule |
| `test_ledger.py` | state transitions, write ordering, decisions, quota, schema drift |
| `test_parsers.py` | all five caption formats, pinned to static fixtures |
| `test_errors.py` | provider error classification — permanent vs transient |
| `test_topics.py` | topics are lowercased, video IDs are not |
| `test_search.py` | the two gates, the provider chain, decision logging |
| `test_search_providers.py` | both search providers against recorded payloads |
| `test_cli_search.py` | the report format, which is a contract a skill parses |
| `test_hook.py` | the library-write guard, blocking and failing open |

`test_parsers.py` and `test_search_providers.py` use static fixtures because those
parsers otherwise run for the first time on the day YouTube changes format.

No test touches the network, the real library, yt-dlp, or a YouTube API key.
Live-network checks belong to acceptance: a test that needs a key and an internet
connection stops being run, and a test that stops being run stops protecting anything.
