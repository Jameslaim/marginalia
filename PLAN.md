# marginalia — Build Plan

A Claude Code plugin that finds relevant YouTube videos, extracts their transcripts,
understands them, and answers questions about them with timestamps.

**Harness: Claude Code, not Cowork.** Cowork's shell is an isolated sandbox; this project
needs local CPU for Whisper, persistent installs of yt-dlp and faster-whisper, a library
folder that survives sessions, and background jobs running 10–15 minutes per video.
Claude Code runs on the real machine. Cowork remains a viable *query-only* surface later,
once the library exists, since `/ask` only reads text files.

**Paths on this machine:**

```
C:\youtube-agent\projects\marginalia\      code, git repository
C:\youtube-agent\marginalia-library\       data, never in git
```

---

## 1. What it is

Two commands inside Claude Code:

- `/marginalia:ingest <topic or url>` — finds videos, extracts transcripts, builds understanding
- `/marginalia:ask <question>` — answers from the library, with citations and timestamps

Everything heavy lives on disk. Claude only ever sees short reports and notes.

---

## 2. Core principles

These five rules decide almost every downstream question.

**1. Heavy data never enters the context window.**
Scripts write transcripts to disk and return a one-line report. This is what
keeps cost flat as the library grows.

**2. Transcripts are immutable.**
Fetched once, stored verbatim, never modified, never deleted. Acquisition is the
fragile, rate-limited, blockable step — do it once and never depend on it again.

**3. Everything else is regenerable.**
Notes can be rewritten with a better prompt at any time, with zero network calls.
The library gets smarter without re-fetching.

**4. Understanding is computed once, at ingest.**
The model reads each transcript whole, while comprehension is best, and saves what
it understood. Query time reads notes, not raw transcripts.

**5. Cheap checks before expensive ones.**
Reading a title is free. Fetching a transcript costs money. Transcribing audio costs
a lot. Discard candidates in that order.

---

## 3. How ingest works

1. **Claude plans.** Turns the topic into 8–10 varied search queries — technical
   phrasing, plain phrasing, adjacent subtopics.
2. **Search** via the official YouTube Data API. Collects candidate video IDs.
3. **Gate 1 — known ID?** Drop anything already in the ledger.
4. **Gate 2 — judged before?** Drop anything previously rejected for this topic.
5. **Cheap model sorts** the survivors, one call per video, fresh context each time.
   "Relevant to this brief — yes, no, or unsure."
6. **Fetch transcripts** for the keepers.
7. **Gate 3 — same content?** Fingerprint the normalised text. Catches reuploads
   that carry a different video ID.
8. **Normalise** — strip scraper UI artifacts, preserve timing gaps, capture
   YouTube chapter markers if present.
9. **Store** transcript + metadata. Mark `needs_note`.
10. **Report to Claude**: "23 found, 8 already had, 13 stored, 2 failed."
11. **Claude decides what's next** — if something unexplored surfaced, search again.

Step 11 looping back to step 1 is the intelligence. Everything else is plumbing.

### Background workers

Two queues, both non-blocking:

| Queue | Feeds on | Slow because |
|-------|----------|--------------|
| ASR | videos marked `needs_asr` | 10–15 min per hour of video on CPU |
| Notes | transcripts marked `needs_note` | one strong-model pass per video |

Ingest returns as soon as transcripts are stored. Both queues drain behind you and can
be paused, batched, or run overnight.

---

## 4. How ask works

Three tiers, cheapest first.

1. **Load all notes.** Under ~100 videos this is the whole library at once —
   no search, no ranking, no embeddings needed.
2. **Answer from notes**, citing which videos.
3. **Escalate on follow-up.** When the question needs exact wording or precise timing,
   load that video's full transcript. The agent states which transcript it is opening
   and why. Transcripts drop out of context after the turn.

### Scaling threshold

| Videos | All notes combined | Approach |
|--------|-------------------|----------|
| 50     | ~75k tokens       | Load everything |
| 100    | ~150k tokens      | Load everything |
| 200    | ~300k tokens      | Getting tight |
| 260+   | 400k+ tokens      | Now add note selection |

Retrieval machinery is only needed past ~100 videos. Build for tier one.

---

## 5. What gets stored

```
C:\youtube-agent\marginalia-library\
  ledger.db                  every video ID ever seen, and its verdict
  videos/
    <video_id>/
      transcript.txt         full, timestamped, verbatim, never modified
      note.md                the understanding
      meta.json              see fields below
```

`meta.json` holds exactly: `video_id`, `title`, `channel`, `duration_s`, `language`,
`upload_date`, `source`, `caption_kind`, `provider`, `fetched_at`, `chapters`,
`original_url`, `line_count`.

`caption_kind` records manual-vs-auto. M3's transcript-upgrade pass depends on it —
an auto-captioned video is a candidate for replacement when manual captions appear.

One folder per video. Human-readable, not just machine-readable.

### What a note contains

Not a summary. A summary loses everything useful.

- Video ID and title (first line — required for citation and escalation)
- What it **claims** — actual assertions, not topic labels
- What it **gets wrong** — errors, with timestamps
- Where it **hedges or contradicts itself**
- What it **assumes you already know**
- What it **leaves out** — unaddressed objections
- **Key moments** with timestamps
- **Who is talking and what they are selling**
- **Verbatim block** — quotable prompts, commands, config, exact phrasings

"What it leaves out" matters most. Someone asking "when is X *not* worth it?" should
find the video that fails to answer it. A topic label never surfaces that.

The verbatim block exists because notes were observed to lose exact wording,
worked examples, and concrete numbers — the things most worth quoting.

---

## 6. Deduplication — full specification

### The three gates

| Gate | Catches | When |
|------|---------|------|
| 1. Video ID | Same video requested twice | Before spending |
| 2. Rejection ledger | Videos already judged irrelevant | Before spending |
| 3. Content fingerprint | Reuploads and mirrors with different IDs | After fetching |

Gates 1 and 2 run before any money is spent. Gate 3 runs after fetching, because two
different uploads cannot be compared until both have been read. You pay once, never again.

### Canonical ID extraction

Every URL form collapses to the same 11-character ID:

```
youtube.com/watch?v=ABC123      →  ABC123
youtu.be/ABC123                 →  ABC123
youtube.com/embed/ABC123        →  ABC123
youtube.com/shorts/ABC123       →  ABC123
youtube.com/live/ABC123         →  ABC123
watch?v=ABC123&t=90&list=PL1    →  ABC123
```

**Video IDs are case-sensitive.** `abc123` and `ABC123` are different videos. Never
lowercase them — this breaks people who normalise IDs the way they normalise text.

Store the ID. Never store the URL as a key.

### Ledger schema

```sql
videos(
  video_id       TEXT PRIMARY KEY,
  status         TEXT NOT NULL,   -- pending|needs_asr|stored|rejected|failed|duplicate
  source         TEXT,            -- captions | whisper
  caption_kind   TEXT,            -- manual | auto
  transcribed_at TEXT,
  content_hash   TEXT,            -- indexed
  duplicate_of   TEXT,            -- canonical id, if duplicate
  first_seen     TEXT NOT NULL,
  last_updated   TEXT NOT NULL,
  last_accessed  TEXT,            -- required for future deletion policy
  attempts       INTEGER NOT NULL DEFAULT 0,
  fail_kind      TEXT,            -- permanent | transient
  provider       TEXT
)

rejections(
  video_id     TEXT NOT NULL,
  topic        TEXT NOT NULL,
  reason       TEXT,
  rejected_at  TEXT NOT NULL
)
```

**Timestamps are `TEXT`, not `TIMESTAMP`.** SQLite gives `TIMESTAMP` NUMERIC affinity —
it contains none of `INT`/`CHAR`/`TEXT`/`BLOB`/`REAL`/`FLOA`/`DOUB`. ISO-8601 strings are
stored as text either way, but `TEXT` declares the intent honestly.

### Rejection is topic-relative — not global

A global rejection list permanently blinds the agent. A video rejected while searching
"prompt caching" because it is mostly about agent architecture must not be invisible
when the user later searches "agent architecture."

Rejections are therefore stored per topic. Gate 2 skips only when the new search is for
the same or a closely related topic. For a different topic the video is re-judged — one
cheap model call is far better than a permanent blind spot.

The ingest report should surface this: "3 previously-rejected videos matched — re-evaluating."

### Write ordering — makes crashes safe

```
1. Insert video_id with status = pending
2. Fetch the transcript
3. Update status = stored
```

Write the ledger row **before** fetching. A crash between 1 and 3 leaves status `pending`,
so the job is known to have been in flight and can resume. Writing only on success leaves
no trace of a crash and causes blind re-fetching.

### Missing captions is a route, not a failure

A video without captions has not failed. It is marked `needs_asr` and picked up by the
transcription queue. It reaches `stored` like any other video, with `source: whisper`.
Relevance is judged on content, never on caption availability.

### Real failures

- **Permanent** — deleted, private, region-blocked, or audio contains no speech at all.
  Do not retry.
- **Transient** — network error, rate limit, audio download failure, ASR crash or OOM.
  Retry with backoff, tracking `attempts` and enforcing a cooldown.

### Optional later: transcript upgrade

If a video was transcribed by Whisper and real captions appear later, those captions are
likely better. `source` and `transcribed_at` are recorded so a re-fetch-and-replace pass
is possible. Not built now.

### Content fingerprint (Gate 3)

After fetching: take text only (drop timestamps), lowercase, strip punctuation, collapse
whitespace, SHA-256. Exact match means identical content — store one transcript, mark the
second row `duplicate` with `duplicate_of` set.

**Near-duplicates are out of scope.** Clips cut from longer videos, or the same talk given
at two events, will not hash-match. Exact hashing catches reuploads and mirrors, which is
the overwhelming majority of the real problem. Revisit only if it actually bites.

### Idempotency

Each stage records completion. Re-running ingest on a `stored` video is a no-op and
says so. Re-running after a crash resumes from the last completed stage.

### The ledger outlives the data

When transcripts are eventually deleted, their ledger rows are **kept**. Otherwise the
agent re-downloads tomorrow exactly what was deleted today. Data is disposable; the record
of having seen it is permanent.

---

## 7. Relevance judging

The part that silently determines library quality. Failures here are invisible.

### The relevance brief

A bare topic is underspecified. "Prompt caching" could mean tutorials, provider
comparisons, or cost analysis — each producing a different library.

So before searching, a model writes an explicit brief: intent, must-contain,
also-wanted, explicitly-excluded, and edge cases. All judging happens against the
brief, never against the raw keyword.

A second, stronger model reviews the brief for gaps before searching begins. No human
approval step — but the brief is logged and surfaced in the ingest report so the user
can see what was searched for and why results look the way they do.

The brief is stored per ingest run. Rejections point at the brief that produced them,
which is what makes rejection topic-relative rather than global.

### Three stages, increasing cost

| Stage | Sees | Verdict |
|-------|------|---------|
| 1 | Title, channel, description, duration, date | YES / NO / MAYBE |
| 2 | First ~1000 words + chapter titles | YES / NO |
| 3 | Whole transcript (during note pass) | Confirms or flags |

**Three-way verdicts at stage 1.** Forcing yes/no on metadata alone makes the model
guess on ambiguous cases. MAYBE routes to stage 2, which is the correct response to
uncertainty.

Stage 3 is effectively free — the note pass reads the transcript regardless. A video
found irrelevant there is marked and becomes signal for improving the judge prompt.

### Bias toward inclusion

The two error types are wildly asymmetric:

- **False positive** — kept something useless. Costs one fetch plus one note. Visible.
- **False negative** — rejected something valuable. Costs a video you will never know
  existed. Invisible and permanent.

Tune the judge to keep when uncertain. A cluttered library is a far smaller problem
than one with holes that cannot be seen.

### Keyword matching is insufficient

A video can be highly relevant while never using the search term. Judging must happen
on content, not on title matching — which is the entire reason stage 2 exists.

### Following threads

Kept videos mention concepts they do not cover. Those become new search queries. The
agent extracts "mentioned but unexplored" terms and proposes follow-up rounds.

**Stopping condition:** two or three consecutive rounds returning no new videos above
threshold, or a configured budget cap.

### Model tiering

| Task | Frequency | Tier |
|------|-----------|------|
| Write brief | once per topic | Strong |
| Review brief | once per topic | Strong |
| Expand queries | once per topic | Cheap |
| Judge metadata | ~50× per topic | Cheap |
| Judge opening | ~10× per topic | Cheap |
| Write note | once per video | Strong |

Rule: anything that runs many times gets a cheap model; anything that runs once gets
a strong one.

### Decision logging — required from day one

Every judgment is logged with its reason:

```
ABC123  NO     "product review, no implementation detail"
XYZ789  MAYBE  "title ambiguous, checking opening"
DEF456  YES    "covers cache expiry and provider differences"
```

Costs nothing. Makes it possible to diagnose a badly-behaving judge months later by
reading back actual decisions rather than guessing. Cannot be reconstructed
retroactively.

### Deferred

Tuning the judge, building a gold test set, and measuring false-negative rate are
deferred until real failures are observed. The decision log is what makes that
diagnosis possible when the time comes.

---

## 8. The stack

| Piece | Choice | Why |
|-------|--------|-----|
| Search | Official YouTube Data API, yt-dlp search as overflow | Sanctioned, keyed, free (~100/day) |
| Transcripts | yt-dlp, second provider as fallback | Never depend on one method |
| No captions | faster-whisper `small`, int8, CPU, local | No usable GPU; ASR is a minority path |
| Sorting | Cheap model, one call per video | Easy question, high volume, flat cost |
| Understanding | Strong model, one pass per video | Hard question, done once |
| Storage | Files + SQLite ledger | Zero setup, portable, inspectable |
| Browser automation | **None** | Slower, costlier, more fragile than APIs |

### Provider abstraction

Transcript acquisition sits behind a `TranscriptProvider` interface with at least
two implementations. YouTube will eventually break one of them. This is the single
most important structural decision in the codebase.

Search sits behind a `SearchProvider` interface for the same reason: official API
primary, yt-dlp search as automatic overflow when the daily quota runs out.

---

## 9. Build order

| # | Milestone | Ships |
|---|-----------|-------|
| **M1** | Foundation | Plugin skeleton, ledger, file layout, ingest one URL |
| **M2** | Discovery | Brief, query expansion, cheap-model sorting, the loop-back |
| **M3** | Extraction hardening | Provider fallbacks, artifact cleaning, chapter capture |
| **M4** | ASR fallback | Videos without captions stop failing |
| **M5** | Understanding | Background note worker, note schema, regeneration tooling |
| **M6** | Query + eval | Ask with escalation, gold test set, cost tracking |

M1 and M2 alone produce something genuinely useful.

---

## 10. Deliberately rejected

Recording these so they don't get reintroduced later.

**Chunking transcripts into segments.** Tested directly: reading a transcript whole
caught a self-contradiction spanning four minutes, a repeated conceptual error, and
an unstated omission. Chunk retrieval would have surfaced passages *about* the topic
and missed all three. Chunking is lossy; whole-context reading is not.

**Embeddings and a vector database.** Storing one note per video instead of ~50 chunks
cuts the vector count by roughly 50x. At that scale plain text search over notes is
instant. Revisit past ~100 videos, not before.

**Browser automation.** The data is available through APIs more cheaply, faster, and
without breaking every time YouTube changes its layout.

**Invidious.** Public instances are unreliable in 2026 and depend on a stranger's server.
yt-dlp search covers the same need with a dependency already in the project.

**youtubei.js.** Capable, but JavaScript — a second runtime to reach the same InnerTube
API yt-dlp already uses. Legitimate future provider if yt-dlp breaks; not worth it now.

**Running the pipeline inside a Claude Code session.** Transcripts would accumulate in
context and be resent every turn — the exact quadratic cost curve that makes long sessions
expensive. The brain belongs in the built agent, not in the conversation driving it.

---

## 11. Risks

1. **YouTube breaks extraction.** Most likely failure. Mitigated by swappable providers
   and by never re-fetching what is already stored.
2. **Discovery finds the wrong videos.** Search quality determines library quality.
   Needs a real test set, not assumption.
3. **Search quota.** 100 free searches/day; each ingest uses several.
4. **ASR quality on real audio.** Published word-error rates use clean audio. Real
   YouTube audio — accents, music beds, crosstalk — routinely doubles or triples it.
   `small` degrades further outside English.
5. **Notes are opinionated and lossy.** They capture what the reading model thought
   mattered. Mitigated by keeping transcripts forever and escalating on demand.

---

## 12. Definition of done

- Ingesting a topic finds and stores relevant videos without supervision
- Running it twice re-processes nothing
- A video with no captions transcribes automatically
- Ask answers with a clickable timestamp
- "What does this video get wrong?" returns a real answer, not a summary
- All notes regenerate from stored transcripts with zero network calls

---

## 13. Deferred — deletion policy

Not built now. Revisit once the library is large and real usage patterns are visible.

**The only thing required today:** record `last_accessed` on every video and update it
whenever a note or transcript is read. Without it there is no way to later identify
"unused" data, and the clock cannot be started retroactively.

When it is eventually built, the expected shape:

- Notes delete freely — they regenerate from transcripts at no network cost
- Transcripts archive rather than delete — re-acquisition is the fragile step
- Ledger rows are never deleted, under any policy

---

## 14. Repository structure

```
marginalia/
├── .claude-plugin/
│   └── plugin.json              name, description, version
├── CLAUDE.md                    rules Claude always reads (anti-drift)
├── PLAN.md                      this file
├── config.example.toml
├── pyproject.toml
│
├── bin/
│   ├── marg                     CLI entrypoint — added to Bash PATH automatically
│   └── marg.cmd                 Windows shim
│
├── skills/
│   ├── ingest/SKILL.md          /marginalia:ingest
│   └── ask/SKILL.md             /marginalia:ask
│
├── agents/
│   ├── judge-relevance.md       model: haiku
│   └── write-note.md            model: opus
│
├── hooks/
│   └── hooks.json               PreToolUse guard: no direct writes to the library
│
├── prompts/
│   ├── write_brief.md
│   ├── expand_queries.md
│   ├── judge_metadata.md
│   ├── judge_opening.md
│   └── write_note.md
│
├── src/marginalia/
│   ├── cli.py                   single entry point
│   ├── config.py
│   ├── ids.py                   canonical video ID extraction
│   ├── ledger.py                the three gates
│   ├── library.py               folder layout read/write
│   ├── discover.py              brief → queries → judge → loop
│   ├── providers/
│   │   ├── base.py              TranscriptProvider + SearchProvider interfaces
│   │   ├── ytdlp.py
│   │   ├── youtube_api.py
│   │   └── whisper_local.py     faster-whisper small, int8, CPU
│   ├── normalize.py             artifact stripping, chapters, gap preservation
│   ├── note.py                  background note worker
│   ├── ask.py                   load notes, escalate to transcript
│   └── llm.py                   all model calls, prompt loading
│
└── tests/
```

### Structural rules

- `providers/` is a folder because it is the part guaranteed to break. Adding a source
  means adding one file, never a refactor. Nothing outside `providers/` imports yt-dlp.
- `llm.py` is the only place model names appear. Switching tiers is one file.
- `cli.py` is the only entry point. Skills are thin — they call the CLI and read the
  report. Logic never lives in a SKILL.md.
- `library.py` builds every library path. No other module constructs one.
- `CLAUDE.md` states the non-negotiables: never print transcript contents into the
  conversation, never bypass the ledger, never modify a stored transcript, never
  lowercase a video ID, always write the ledger row before fetching.
- `hooks/hooks.json` enforces those rules programmatically rather than relying on
  instruction-following.

### Milestone 1 subset

Only these exist after M1: `plugin.json`, `CLAUDE.md`, `config.example.toml`,
`skills/ingest/SKILL.md`, `bin/marg`, and `cli.py`, `config.py`, `ids.py`, `ledger.py`,
`library.py`, `providers/{base,ytdlp}.py`, plus `tests/test_ids.py` and
`tests/test_ledger.py`.

Everything else arrives with the milestone that needs it. Split a file when it has two
different reasons to change — not before.

### Development flow

```bash
claude --plugin-dir ./marginalia   # load the local plugin directory
/reload-plugins                    # pick up changes without restarting
```

Start standalone for fast iteration; convert to a distributable plugin only when sharing.

---

## 15. Claude Code capabilities to use

### Model switching — native, per task

Subagents accept `model` (`haiku`, `sonnet`, `opus`, `fable`, a full model ID, or
`inherit`) and `effort` (`low` → `max`) in frontmatter. Model tiering becomes
configuration rather than code.

```yaml
# agents/judge-relevance.md
---
description: Decides if one video matches the relevance brief
model: haiku
effort: low
maxTurns: 1
tools: Read
---
```

```yaml
# agents/write-note.md
---
description: Reads a full transcript and writes the understanding note
model: opus
effort: high
---
```

Resolution order: `CLAUDE_CODE_SUBAGENT_MODEL` → per-invocation parameter → frontmatter
→ inherit. `maxTurns` caps runaway subagents — the judge should decide in one turn.

### Two ways to make model calls

| | Python → API (`llm.py`) | Claude Code subagents |
|---|---|---|
| Billing | API key, per-token | Uses the session |
| Model tiering | Code | Frontmatter |
| Unattended batches | **Yes** | No — session only |
| Setup cost | Keys, billing | None |

The plan depends on background ASR and note queues running unattended, so `llm.py` is
the destination. But prototype M1–M2 with subagents to avoid billing setup, then move
judging into `llm.py` when unattended runs are needed. That switch touches one file,
which is the reason `llm.py` exists.

### Monitors

Plugins can declare background shell commands whose stdout lines become notifications
to Claude automatically. Useful for queue visibility:

```json
[
  { "name": "asr-queue",
    "command": "tail -F /c/youtube-agent/marginalia-library/logs/asr.log",
    "description": "Transcription progress" }
]
```

Experimental. Interactive CLI sessions only, unsandboxed, and stops when the session
ends. Visibility aid, not a substitute for a real daemon.

### Other

- **`hooks/`** — `PreToolUse` blocks direct writes to the library. Enforcement, not
  instruction. Worth doing early.
- **`bin/`** — CLI added to the Bash PATH while the plugin is active.
- **Python LSP plugin** — code intelligence during the build phase. Install from the
  official marketplace.
- **`isolation: worktree`** — isolated checkouts if two agents ever work the codebase
  concurrently.

---

## 16. Operating notes

- **YouTube chapters are free segmentation.** When present, use the author's own
  structure instead of inferring it.
- **Timing gaps are signal, not noise.** Silence between timestamps means an edit.
  Preserve gap information rather than smoothing it.
- **Transcripts arrive dirty.** Scraper UI text leaks into output. Artifact stripping
  must be a tested component, not an afterthought.
- **Store the note prompt version** alongside each note, so stale ones are identifiable
  when the prompt improves.
