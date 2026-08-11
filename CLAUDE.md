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
   the files must stay in agreement.

3. **Never modify a stored `transcript.txt`.** It is written once and is immutable.
   Acquisition is the fragile, blockable step; everything else in the system is
   regenerable from the transcript.

4. **All transcript sources go behind `TranscriptProvider`.** Never import `yt_dlp`
   outside `src/marginalia/providers/`. YouTube will eventually break it; adding a
   replacement must mean adding one file, never a refactor.

5. **Never lowercase or otherwise normalise a YouTube video ID.** IDs are
   case-sensitive: `dQw4w9WgXcQ` and `dqw4w9wgxcq` are different videos.

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

Milestone 1 is ingest-one-URL only. Do not add discovery, LLM calls, ASR,
understanding notes, `/ask`, embeddings, or content fingerprinting — each belongs to
a later milestone in `PLAN.md`. Do not add dependencies beyond `yt-dlp` and `pytest`.

## Running it

```
marg ingest <url>       ingest one video
marg status <url|id>    show ledger state for one video
marg list               list stored videos
```

Config resolution: `MARGINALIA_CONFIG` → `./config.toml` → `~/.marginalia/config.toml`.
