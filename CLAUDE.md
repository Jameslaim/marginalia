# marginalia — rules

A Claude Code plugin that stores YouTube transcripts on disk and answers questions
about them. `PLAN.md` holds the full design. This file holds the rules that must
not drift.

## Hard rules

1. **Never print, echo, or paste transcript contents into the conversation.**
   Not in output, not in error messages, not under `--verbose`. Scripts write to
   disk and return a one-line report. This is what keeps cost flat as the library
   grows, and it is the entire reason marginalia is a CLI.
   **Notes are held to the same rule.** A note is roughly a tenth of a transcript
   and thirty-six of them still fill a context window, so a skill that printed one
   back would rebuild the exact cost curve this avoids. Reading notes is M6's job,
   deliberately, and it happens once per question rather than once per command.

2. **Never write into the library directory directly — always go through the CLI.**
   No `Write`, no `Edit`, no shell redirection into the library path. The ledger and
   the files must stay in agreement. Enforced, not merely stated: `hooks/hooks.json`
   runs `marg guard-write` as a `PreToolUse` hook and blocks the tool call.

3. **Never modify a stored `transcript.txt`.** It is written once and is immutable.
   Acquisition is the fragile, blockable step; everything else in the system is
   regenerable from the transcript. `note.md` is the opposite and deliberately so:
   overwritten freely, rewritten whole by a better prompt, and never a reason to
   fetch anything. `marg note --regenerate --all` must complete with YouTube
   unreachable, and that is checked by running it that way, not by assuming it.

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
   precedes every network call, and `start_run()` precedes the first judgement. A
   crash then leaves `pending`, or a run with no `finished_at`, which tells the next
   run the job was in flight. The ledger enforces this: `mark_stored` and friends
   raise if no row exists.

7. **`llm.py` is the only place a model is named, a request is built, or a prompt is
   loaded.** The provider is temporary and has already been replaced once, so swapping
   it must mean editing `config.toml` and nothing else. Use only the OpenAI-compatible
   baseline: `chat/completions`, a system message, a user message, plain text back.
   **Never** JSON mode, structured outputs, tool calling, prefills, or a
   provider-specific parameter; each is something some future provider will lack.
   Prompts stay model-agnostic — a test asserts none of them names a model family.

   **`_send` may name only `model`, `messages`, `max_tokens` and `extra`.** Everything
   else a provider wants comes from `[llm] extra` or `[llm.tasks.<name>] extra`, merged
   in and passed through without `llm.py` knowing what any of it means. This is not
   theoretical: a hardcoded `temperature=0` was the single thing that broke the first
   real provider swap, because `kimi-k3` refuses any temperature but its own default.
   A test reads `_send`'s source to keep it honest.

8. **Anything a judge cannot parse becomes MAYBE, never NO.** Keeping a useless video
   costs one download and is visible in the library. Rejecting a good one costs a
   video nobody will ever know existed — invisible and permanent. A parser bug, a
   refused batch, a network blip mid-judgement: all resolve toward keeping. The two
   mistakes are not symmetric and the code must not treat them as if they were.

## Structure

- `cli.py` is the only entry point. Skills are thin: they call `marg` and report the
  line it returns. Logic never lives in a `SKILL.md` — the ingest skill routes on
  whether the argument parses as a video ID, and does nothing else.
- `library.py` builds every library path. No other module constructs one.
- `llm.py` makes calls and knows nothing about verdicts. `judge.py` turns replies into
  verdicts and knows nothing about the loop. `discover.py` runs the loop and parses
  nothing. Keep that split; it is why the parsers are testable without a model.
  `note.py` holds the same split internally: pure section parsing at the top, the
  drain loop at the bottom, and nothing in between that needs a model to test.
- `print` appears only in `cli.py`. Everything else uses `logging`.
- Reading a transcript must be followed by `Ledger.touch_accessed(video_id)`. The
  deletion policy that arrives later depends on it, and it cannot be reconstructed
  retroactively.

## Scope

Milestone 5 adds **understanding notes**: `note.py`, `prompts/write_note.md`, the
`marg note` command, and `note_version`/`note_model`/`noted_at` on `videos`. A stored
transcript goes in, a note comes out, and the note is what every later question is
answered from.

Do not add `/ask` or `ask.py`, ASR or Whisper, embeddings, retrieval or ranking over
notes, or content fingerprinting (Gate 3) — each belongs to another milestone in
`PLAN.md`. Under ~100 videos the answer to "which notes are relevant?" is *all of
them*, and there are 36. Do not build a gold evaluation set or tune the judge; both
wait until real failures are observed, which is what the `decisions` log exists to
make possible.

**A note is not a summary.** A summary says what a video is about; a note says what it
claims, what it gets wrong, what it assumes, and — most valuable — what it leaves out.
If a section of a note could be replaced by a topic label, the prompt has drifted.

**Everything in a note that did not come from the transcript carries a `[note: ...]`
marker.** Arithmetic, extrapolation, outside knowledge of the subject or the speaker,
comparison to anything outside the video. Inference is not banned — it is often the
most valuable thing in a note — but it must never be indistinguishable from what the
speaker said. M6 answers from notes, so an unmarked inference becomes an asserted fact
two layers away from any transcript that could contradict it. `grep -c '\[note:'`
across the library says how much of the understanding layer is inference rather than
record. Audited at v2 and found systematic: ~44 outside-knowledge assertions across
five notes, two of them marked.

Do not add model switching. One model everywhere; what varies per task is thinking
effort, and that is config. A second model is a second variable while behaviour is
still stabilising.

Dependencies are `yt-dlp`, `openai`, and `pytest`. Nothing else. The YouTube Data API
is reached with stdlib `urllib` — it is two GETs returning JSON and an HTTP library
would buy nothing. `openai` earns its place because hand-rolling retries and
rate-limit handling is how you get silent failures.

## Running it

```
marg ingest <url>                  ingest one video
marg search "<q>" [--limit N]      find candidates; --topic T, --json
marg discover "<topic>"            the loop; --dry-run, --max-rounds/videos/requests
marg note [<video_id>...]          write notes; --limit N, --regenerate [--all], --status
marg snap-timestamps [<id>...]     align cited timestamps to the transcript; --dry-run
marg llm-check                     one request to the configured provider
marg status <url|id>               show ledger state for one video
marg list                          list stored videos
marg guard-write                   internal: the PreToolUse hook, reads stdin
```

Config resolution: `MARGINALIA_CONFIG` → `./config.toml` → `~/.marginalia/config.toml`.
