# marginalia

A Claude Code plugin that finds relevant YouTube videos, extracts their transcripts,
writes an understanding note for each, and answers questions about them with
timestamps.

**This is Milestone 2b.** Give it a topic and it writes a brief, expands that into
search queries, judges the candidates in two stages, stores what survives, and uses
what the kept videos left unexplained to drive the next round. There is no ASR, no
understanding notes, and no `/ask` yet. See `PLAN.md` for the build order.

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
marg note --limit 3
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

## Discover

```bash
marg discover "prompt caching"
marg discover "agent cost control" --dry-run
```

```
brief:    Understand how prompt caching cuts cost in practice.
round 1:  5 queries, 63 candidates, 8 known, 41 no, 14 maybe → 9 stored
round 2:  4 queries from threads, 22 candidates, 19 known, 2 no, 1 maybe → 1 stored
run:      10 stored, 66 judged, 2 rounds, 24 requests, stopped: no_new_videos
```

A topic on its own is underspecified, so the run writes an explicit **brief** first —
intent, must-contain, also-wanted, excluded, edge cases — has a second pass close its
gaps, and judges everything against that rather than against the keyword. The brief is
stored on the run and printed first, because when a library comes out wrong the first
question is *what did it think I meant?*

Judging is two stages:

1. **Metadata**, batched. YES / NO / MAYBE per candidate. Three-way because forcing a
   binary choice on a title and a truncated description makes the model guess.
2. **Opening**, one call each, for the MAYBEs only. Fetches the transcript and judges
   the first ~1000 words. This exists because a video can be exactly right while never
   using the search term — titles lie, openings do not.

Stage 2 also returns the concepts a video *mentions but never explains*, in the same
response, at no extra request. Those become the next round's queries. That loop-back is
the part that turns one topic into a library.

Both judges are told to keep when genuinely uncertain. Keeping something useless costs
one download and is visible; rejecting something valuable costs a video you will never
learn existed. Anything the parser cannot read becomes MAYBE, never NO, for the same
reason.

`--dry-run` does the brief, queries, search and stage-1 judging, then stops before any
fetch or store — and writes nothing at all, not even decision rows. Tuning the judge
must not silently shrink the library it is meant to improve.

Stopping conditions, whichever fires first: `--max-rounds`, two consecutive rounds with
nothing new, `--max-videos`, `--max-requests`, or search going unavailable. Which one
fired is recorded in `runs.stopped_because` — a loop that stops for an unknown reason
cannot be tuned.

## Notes

```bash
marg note --limit 3                # start here, always
marg note --status
marg note dQw4w9WgXcQ
marg note --regenerate --all
```

```
noted     dQw4w9WgXcQ  1,123 tokens  v2
skipped   jNQXAC9IVRw  current note is v2
failed    xK2mPq9dRtY  transient: connection refused
run:      1 noted, 1 skipped, 1 failed, 3 requests, $0.17, 2m51s
```

One model reads one whole transcript, once, and writes down what it understood.
A transcript is ~4–15k tokens; its note is ~1.5k. That is the difference between
holding two videos in context and holding fifty, and it is the whole reason the
reading happens at ingest rather than at query time.

**A note is not a summary.** Seven fixed sections, free prose inside each:

| Section | What it holds |
|---------|---------------|
| Claims | the actual assertions, stated so they could be disagreed with |
| Wrong or shaky | errors with timestamps, self-corrections, contradictions, hedges |
| Assumes you know | the prerequisites never explained |
| Leaves out | objections never addressed, questions raised and dropped |
| Key moments | timestamps worth jumping to, and where the pacing goes wrong |
| Who's talking, what they want | speaker, affiliation, sponsor, share of runtime that is promotional |
| Verbatim | short high-value excerpts: commands, config, exact numbers |

"Leaves out" matters most. Someone asking *when is X not worth it?* should be led to
the video that fails to answer it, and no topic label will ever do that.

The headings are fixed so fifty notes can be read together; the prose inside is free
so the note can say something a schema could not anticipate. A section with nothing to
report must *say so* — "no errors found" is a claim, and a useful one — so a heading
with nothing under it is treated as a missing section and the note is rejected. A
rejected note is never written, and the video stays retryable.

The first two lines are built from `meta.json`, not asked for. The video ID is the one
field that must not be generated: escalating from a note back to its transcript is done
by that ID.

**`--limit` is not optional.** Generating thirty-six notes with an untuned prompt costs
real money and produces thirty-six mediocre notes. Generate three, read them, revise
the prompt, run again.

### Regeneration, and why it is the point

A video needs a note when it is `stored` and either has never been noted or carries a
note some *older* prompt wrote. That is a query, not a list somebody maintains, which
is what `note_version` on the video row buys. Bump the version in
`prompts/write_note.md` and every existing note is stale by definition.

Regeneration reads stored transcripts and calls the model. It never touches YouTube —
and that is verified by running it with YouTube unreachable, not by assuming it.
Acquisition is the fragile step; everything downstream of it being regenerable is what
makes that survivable.

Transcripts too long for the model's context are logged and skipped, never split.
A note assembled from chunks would look like a note and would not be one: the value is
in the reading that spans four minutes and notices the contradiction.

Note contents never reach stdout, for the same reason transcripts do not.

## Swapping the model provider

**Treat the provider as temporary.** Swapping is three config values, and it has been
done once in anger — NVIDIA NIM to Moonshot, when the former went from ~235s per
request to not completing at all.

```toml
[llm]
base_url = "https://api.moonshot.ai/v1"
model    = "kimi-k3"
api_key  = "..."                 # or set MARGINALIA_LLM_API_KEY

[llm.limits]
requests_per_minute = 20         # the next provider's ceiling, not ours
```

Then, in order:

```bash
marg llm-check
```

One request. Reports model, latency, and whether the reply parsed. Run it first — a
provider that answers but not usably otherwise shows up much later as an empty library.
**If a single request takes more than ~30s, stop and reconsider the model** rather than
starting a run; that was the entire problem with the previous provider and it is worth
catching in one request instead of an hour.

```bash
marg discover "prompt caching" --dry-run
```

Then compare against the previous decision log. Both `runs` and `decisions` record the
`model`, so:

```sql
SELECT model, verdict, count(*) FROM decisions GROUP BY model, verdict;
```

If judging quality shifted, that query is how you find out where.

Known OpenAI-compatible options: **Groq**, **OpenRouter**, **DeepSeek**, **Together**,
and local serving via **Ollama** or **vLLM**. All take the same three values.

Only the OpenAI-compatible baseline is used — `chat/completions`, a system message, a
user message, plain text back. No JSON mode, no structured outputs, no tool calling, no
prefills, no provider-specific parameters. Every one of those is something some
provider will lack. The parsers in `judge.py` are the price of that portability, and
they are cheaper than a migration.

### What the real swap actually cost

Three config values and **one code deletion**. `llm.py` had `temperature=0` hardcoded;
`kimi-k3` returns HTTP 400 for any temperature but its own default. Temperature looks
like part of the OpenAI baseline and is not — reasoning models routinely refuse it.

It is now gone from the code and lives in config as `[llm] extra` if a provider wants
it. `_send` names only the four fields every endpoint requires, and a test reads its
source to keep it that way. Nothing in the suite would have caught this: a mock
transport accepts whatever it is handed. An abstraction is only proven by a swap
nobody rehearsed.

## Thinking effort per task

One model everywhere — a second model is a second variable. What varies is effort:

```toml
[llm.tasks.judge_metadata]
extra = { reasoning_effort = "low" }

[llm.tasks.write_brief]
extra = { reasoning_effort = "high" }
```

`llm.py` does not know what any of that means. It merges the block into the request
body and sends it; `[llm] extra` applies to every request and a task block overrides it
key by key. A provider without the feature has no blocks, gets an empty dict, and every
request is the baseline — which is what keeps swapping config-only.

`judge_metadata` runs most often and reasons least, and reasoning tokens bill as
**output** at 5× the input rate, so effort on the highest-volume call is where cost and
latency go wrong at once. `write_brief` runs once and decides what every later
judgement is measured against. Low is the floor, never off: "off" is a different code
path on some providers, and consistency beats the marginal saving.

**Check the parameter is honoured before trusting it.** A silently ignored field looks
identical to a working one. Send a deliberately invalid value, and compare output-token
counts at low versus high. On `kimi-k3`: an invalid value is *accepted* without
complaint (so it is not validated — a typo degrades to the default rather than
erroring), but low produced 270–406 output tokens against high's 744–1395 on the same
prompt, with median latency 15.4s against 33.2s. Honoured, not validated.

## What a run costs

Billing is per token, so the request count is a weak guard — one batch of fifty
candidates and one of five cost the same requests and wildly different money.

```toml
[llm.pricing]
input_per_million  = 3.0
output_per_million = 15.0

[discover]
max_cost_usd = 1.50
```

Rates live in config because they change and the next provider's will differ. The
ceiling is computed from the token counts already recorded on every run, and firing it
is recorded as `stopped_because = max_cost`.

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
        ├── note.md            the understanding — rewritten freely
        └── meta.json
```

```
[00:00:01.500 --> 00:00:18.200] So let's suppose that you're working with your coding agent
[00:00:18.200 --> 00:00:35.400] Are you charged full price for those tokens?
```

## Ledger

SQLite, four tables: `videos`, `decisions`, `quota`, `runs`.

`videos` holds every video ID ever seen and its status: `pending`, `needs_asr`,
`stored`, `rejected`, `failed`, `duplicate`. Rows are never deleted, even when data is —
otherwise the agent re-downloads tomorrow what it discarded today. It also carries
`note_version` / `note_model` / `noted_at`, which is what makes "which notes are stale?"
a query. Those three arrived after the table shipped and are added in place: a library
of transcripts predating notes is exactly the library notes are for, and rebuilding it
to add three nullable columns would destroy the fragile artifact to add the regenerable
one.

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
| `test_config.py` | config parsing, and especially the provider-swap surface |
| `test_llm.py` | rate-limiter pacing, retry classification, prompt loading |
| `test_judge_parsing.py` | every way a model reply can be malformed |
| `test_discover.py` | loop control flow, stopping conditions, dry-run inertness |
| `test_cli_discover.py` | the discover report and `llm-check` |
| `test_note.py` | section validation, staleness, retryability, the no-YouTube promise |
| `test_cli_note.py` | the note report, and that no note content reaches stdout |

`test_parsers.py` and `test_search_providers.py` use static fixtures because those
parsers otherwise run for the first time on the day YouTube changes format.

No test touches the network, the real library, yt-dlp, or a YouTube API key.
Live-network checks belong to acceptance: a test that needs a key and an internet
connection stops being run, and a test that stops being run stops protecting anything.
