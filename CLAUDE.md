# marginalia — rules

A Claude Code plugin that stores YouTube transcripts on disk and answers questions
about them. `PLAN.md` holds the full design. This file holds the rules that must
not drift.

## Hard rules

1. **Never print, echo, or paste transcript contents into the conversation.**
   Not in output, not in error messages, not under `--verbose`. Scripts write to
   disk and return a one-line report. This is what keeps cost flat as the library
   grows, and it is the entire reason marginalia is a CLI.

2. **Never write into the library directory directly — always go through the CLI.**
   No `Write`, no `Edit`, no shell redirection into the library path. The ledger and
   the files must stay in agreement. Enforced, not merely stated: `hooks/hooks.json`
   runs `marg guard-write` as a `PreToolUse` hook and blocks the tool call.

3. **Never modify a stored `transcript.txt`.** It is written once and is immutable.
   Acquisition is the fragile, blockable step; everything else in the system is
   regenerable from the transcript.

4. **All sources go behind a provider interface** — `TranscriptProvider` for
   transcripts, `SearchProvider` for search. Never import `yt_dlp`, or any other
   search or transcript library, outside `src/marginalia/providers/`, and never let
   anything above that package know which provider ran. YouTube will eventually break
   one of them; adding a replacement must mean adding one file, never a refactor.
   The corollary that is easy to lose: providers must also *agree* on the shape of
   what they return. The API HTML-escapes titles and yt-dlp does not, so
   `youtube_api.py` unescapes — otherwise the same video carries a different title
   depending on which source answered.

5. **Never lowercase or otherwise normalise a YouTube video ID.** IDs are
   case-sensitive: `dQw4w9WgXcQ` and `dqw4w9wgxcq` are different videos.
   A **topic** is the opposite case: it is a search string, so it *is* lowercased and
   whitespace-collapsed by `normalise_topic`. That asymmetry is deliberate and
   `tests/test_topics.py` exists to defend it. Do not make the two consistent.

6. **Always write the ledger row before fetching, never after.** `mark_pending()`
   precedes every network call. A crash then leaves `pending`, which tells the next
   run the job was in flight. The ledger enforces this: `mark_stored` and friends
   raise if no row exists.

## Structure

- `cli.py` is the only entry point. Skills are thin: they call `marg` and report the
  line it returns. Logic never lives in a `SKILL.md`.
- `library.py` builds every library path. No other module constructs one.
- `print` appears only in `cli.py`. Everything else uses `logging`.
- Reading a transcript must be followed by `Ledger.touch_accessed(video_id)`. The
  deletion policy that arrives later depends on it, and it cannot be reconstructed
  retroactively.

## Scope

Milestone 2a is ingest-one-URL plus **search and the two free gates**. `marg search`
decides only whether a candidate is *known* — never whether it is any good.

Do not add LLM calls of any kind, `llm.py`, `prompts/`, `discover.py`, relevance
judging, the discovery loop, ASR, understanding notes, `/ask`, embeddings, or content
fingerprinting (Gate 3) — each belongs to a later milestone in `PLAN.md`.

If you find yourself wanting to decide whether a video is good, stop. That is M2b.

Do not add dependencies beyond `yt-dlp` and `pytest`. The YouTube Data API is reached
with stdlib `urllib`; it is two GETs returning JSON and an HTTP library would buy
nothing.

## Running it

```
marg ingest <url>                  ingest one video
marg search "<q>" [--limit N]      find candidates; --topic T, --json
marg status <url|id>               show ledger state for one video
marg list                          list stored videos
marg guard-write                   internal: the PreToolUse hook, reads stdin
```

Config resolution: `MARGINALIA_CONFIG` → `./config.toml` → `~/.marginalia/config.toml`.
