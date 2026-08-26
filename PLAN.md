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
      note.md                the understanding — overwritten freely
      meta.json              see fields below
```

The two files are governed by opposite rules, and that is the design rather than an
inconsistency. `transcript.txt` refuses to be overwritten because re-acquiring it may
be impossible. `note.md` is rewritten whole whenever the prompt improves, from the
transcript, with no network call to YouTube — verified by running the regeneration
with YouTube unreachable, not by assuming it.

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
  provider       TEXT,
  note_version   TEXT,            -- the write_note prompt version behind note.md
  note_model     TEXT,
  noted_at       TEXT
)

decisions(
  video_id   TEXT NOT NULL,
  topic      TEXT NOT NULL,
  stage      TEXT NOT NULL,   -- search | metadata | opening | note
  verdict    TEXT NOT NULL,   -- yes | no | maybe
  reason     TEXT,
  decided_at TEXT NOT NULL
)
CREATE INDEX idx_decisions_topic ON decisions(topic, verdict);
CREATE INDEX idx_decisions_video ON decisions(video_id);

quota(
  provider   TEXT NOT NULL,
  date       TEXT NOT NULL,   -- UTC calendar date, YYYY-MM-DD
  units_used INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (provider, date)
)

plans(
  run_id         TEXT NOT NULL,
  round          INTEGER NOT NULL,
  plan_json      TEXT NOT NULL,   -- RoundPlan.as_dict(), whole
  prompt_version TEXT,
  planned_at     TEXT NOT NULL
)
CREATE INDEX idx_plans_run ON plans(run_id, round);
```

`plans` stores what the planner *concluded*, where `queries` stores what it asked for.
One row per round rather than a column on `runs`, because a run has many rounds and a
plan is identified by the round it ended. Nothing queries inside `plan_json`: it is
written once and read back whole, by a person or by the export renderer.

`decisions` replaced an earlier `rejections` table in M2a. Logging only the
negative verdicts was too narrow in two ways: it cannot answer "why did the
judge *keep* this?", and it does not record which stage decided — the two
questions worth asking when the judge starts misbehaving. There is no migration;
the schema-drift guard in `ledger.py` refuses a ledger carrying the old table
and says to delete the library and re-ingest.

`decisions` is deliberately **not** foreign-keyed to `videos`. A search
candidate has not been touched yet, and inserting a `videos` row for all 25
hits of every query would claim work was in flight that never started.

`quota` is keyed by UTC date, so the daily reset is the key changing rather than
a job anyone has to remember to run. Yesterday's rows are kept: they cost
nothing and record what a day actually spent.

**Timestamps are `TEXT`, not `TIMESTAMP`.** SQLite gives `TIMESTAMP` NUMERIC affinity —
it contains none of `INT`/`CHAR`/`TEXT`/`BLOB`/`REAL`/`FLOA`/`DOUB`. ISO-8601 strings are
stored as text either way, but `TEXT` declares the intent honestly.

### Rejection is topic-relative — not global

A global rejection list permanently blinds the agent. A video rejected while searching
"prompt caching" because it is mostly about agent architecture must not be invisible
when the user later searches "agent architecture."

Verdicts are therefore stored per topic. Gate 2 drops a candidate only when a `no`
exists for that video *on this topic*. For a different topic the video is re-judged —
one cheap model call is far better than a permanent blind spot.

The report surfaces it whenever it fires: `3 previously rejected` in the summary line,
and one `rejected <id> no for this topic (<date>)` line per video.

**Topic matching is exact, on the normalised string — nothing more.** The topic is
lowercased and its whitespace collapsed before it is stored or compared, so
"Prompt Caching" and "prompt  caching" are one topic. Beyond that there is no
similarity, no stemming, no embedding.

Fuzzy topic similarity is **deferred, and the asymmetry is the reason**. A wrong fuzzy
match hides a video permanently and invisibly — the failure nobody can see. A missed
exact match costs one cheap re-judge — the failure that shows up on a bill. Exact
matching only ever makes the cheap error. Revisit only if re-judging measurably hurts.

**Topics are lowercased; video IDs are never.** They look alike and the temptation to
normalise both the same way is real, but a topic is a search string and a video ID is a
case-sensitive identifier. `tests/test_topics.py` exists solely to state that asymmetry
so nobody later "fixes" `ids.py` to match.

### Gate 3 is still deferred

Content fingerprinting has not been built. `content_hash` and `duplicate_of` exist in
the schema, and Gate 1 already treats `duplicate` as known, but nothing writes them yet.
M3 owns it.

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

| Stage | Sees | Decides |
|-------|------|---------|
| 1 | Title, channel, description, chapters, duration, date | whether to **fetch** |
| 2 | The whole transcript + chapter titles (B3) | whether to **keep** — YES / NO |
| 3 | Whole transcript (during note pass) | Confirms or flags |

**Three-way verdicts at stage 1.** Forcing yes/no on metadata alone makes the model
guess on ambiguous cases. MAYBE routes to stage 2, which is the correct response to
uncertainty.

**Both YES and MAYBE route to stage 2** — the two columns above are what each stage
*decides*, not two paths through the pipeline. A YES that skipped stage 2 was a video
stored without anything having read it; see B6.

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

### Effort tiering — one model, varying thinking effort

Not model switching. One model everywhere: a second model is a second variable
while behaviour is still stabilising, and the cost difference across a
provider's range is a few cents per run. What varies is *thinking effort*, set
per task in `[llm.tasks.<name>] extra` and passed through untouched.

| Task | Frequency | Effort |
|------|-----------|--------|
| Write brief | once per topic | high |
| Review brief | once per topic | high |
| Expand queries | once per round | low |
| Judge metadata | ~50× per topic | low |
| Judge opening | ~10× per topic | medium |
| Write note (M5) | once per video | high |

The split follows volume and consequence together. `judge_metadata` runs most
often and reasons least — and reasoning tokens bill as *output*, at 5× the input
rate, so effort on the highest-volume call is where cost and latency go wrong at
once. `write_brief` runs once and decides what every later judgement is measured
against.

**Effort is never off.** Low is the floor. Beyond quality, "off" is a different
code path on some providers with different behaviour, and consistency is worth
more than the marginal saving.

Measured on `kimi-k3`: low produced 270–406 output tokens on a fixed prompt,
high produced 744–1395 — ranges that do not overlap — with median latency 15.4s
against 33.2s. The parameter is honoured. It is not *validated*: an invalid
value is accepted silently, so a typo in config degrades to the default rather
than erroring.

### Long structured output needs both a low effort and a generous ceiling

**On this provider, any task whose output is a long structured document needs
`reasoning_effort` low *and* a `max_tokens` well above what the answer itself
costs.** Four tasks have now hit this — `write_note`, `review_brief`,
`map_territory`, `plan_round` — and it is one rule, not four coincidences.

The mechanism is that **reasoning bills against `max_tokens`**. Above `low` the
reasoning does not terminate; it expands to fill whatever budget exists, so
raising the ceiling alone buys a larger bill and lowering the effort alone still
leaves no room when a call reasons longer than usual. Both halves are required.

The measurement that makes this concrete is `review_brief`, whose *answer* is
small and whose *failure* was total:

| ceiling | call | finish | output | result |
|---|---|---|---|---|
| 8192 | review | length | 8,192 | EMPTY — 0 chars back, unreviewed draft kept |
| 16384 | draft | stop | 1,556 | valid |
| 16384 | review | stop | 1,703 | valid, 3,343-char brief |

It converges at roughly a tenth of the raised ceiling and still spent all of the
old one. So **a ceiling sized to the expected output is sized to the wrong
thing**: the headroom exists to absorb a reasoning excursion, and under
per-token billing headroom that goes unused costs nothing.

**Both failure modes look identical from outside, and neither looks like an
error.** A truncated reply is discarded by the guard that catches it, and what
the caller sees is an absence: a map with no queries, a plan that never arrives,
a note that was not written, a brief that was silently not reviewed. The run
continues, exits zero, and reports one warning line among many. `review_brief`
is the worst of the four because its fallback is *plausible* — the unreviewed
draft is a real brief, so every verdict in that run is measured against a
document whose gaps were never closed, and nothing downstream can tell.

The corollary for anything added later: a new call that emits a structured
document is assumed to have this problem until measured, not assumed not to.

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
| Judging | Moonshot, `kimi-k3`, OpenAI-compatible | Paid, no queue; see below |
| Understanding | Same provider, one pass per video | Hard question, done once |
| Storage | Files + SQLite ledger | Zero setup, portable, inspectable |
| Browser automation | **None** | Slower, costlier, more fragile than APIs |

### The model provider, and why it is temporary

M2b judges with **Moonshot** (`https://api.moonshot.ai/v1`, model `kimi-k3`)
through the `openai` SDK. It is the *second* provider: the design was built
against NVIDIA NIM's free tier and swapped in anger, which is the only real
test the abstraction was ever going to get.

**What the first provider taught, and why it was abandoned.** NVIDIA NIM served
`z-ai/glm-5.2` free, at 40 requests/minute, with credits billed per request.
Latency turned out to be a *queue*, not generation: ~235–300s per request
regardless of prompt size, against ~8s for `nemotron-3-super-120b-a12b` on the
same key. It then degraded further — HTTP 504s, and single requests failing to
return inside 500s — which is what forced the swap. A discovery run there took
35–100 minutes.

**Rate limiting blocks, it does not fail.** A token bucket in `llm.py`, sized
from config. A discovery run legitimately needs more requests than any
per-minute ceiling permits; it should pace itself, not fail at request 41.
Verified live at 69 requests with 26 pauses in 59 seconds.

**Batching survived the change of billing model.** Under per-request credits it
saved requests. Under Moonshot's per-token billing it saves sending the brief
once per batch instead of once per candidate. Still correct, for a different
reason. `judge_batch_size = 1` must behave identically either way.

**Money is the real guard now.** Under per-token billing a request count says
nothing: one batch of fifty candidates and one of five cost the same number of
requests and wildly different amounts. `max_cost_usd` is computed from the token
counts already recorded and per-million rates in config.

**The tier is still temporary and the successor is still unchosen.** The
provider is treated as disposable from the first line of code.

### Swapping providers is config, never code

Only the OpenAI-compatible baseline is used: `chat/completions`, a system
message, a user message, plain text back. Deliberately **not** used — JSON mode,
structured outputs, tool calling, response prefills, any provider-specific
parameter. Each is something some future provider will lack or implement
differently. Robust text parsing works everywhere, and the parsers in
`judge.py` are cheaper than a migration.

Consequences that must hold:

- Prompts are model-agnostic. No "you are X" framing, no reliance on one
  model's formatting habits. A test asserts no prompt names a model family.
- Everything provider-shaped is config, `requests_per_minute` included — the
  next provider will have a different ceiling.
- `model` is recorded on both `runs` and `decisions`. When the provider changes
  and quality shifts, the only way to find out why is to know which model
  produced which verdict.
- `marg llm-check` sends one trivial request and reports model, latency and
  whether parsing worked. First thing to run after a swap.

Known OpenAI-compatible fallbacks: Groq, OpenRouter, DeepSeek, Together, and
local serving via Ollama or vLLM. All take the same three config values.

### What the real swap cost — one line of code

Swapping NVIDIA NIM for Moonshot was three config values and **one code
deletion**: `llm.py` had `temperature=0` hardcoded in the request, and
`kimi-k3` returns HTTP 400 for any temperature but its own default. Temperature
looks like part of the OpenAI baseline and is not — reasoning models routinely
refuse it.

The abstraction was 95% right and the missing 5% was exactly the failure mode it
was built to prevent: a provider-shaped parameter living in code. Removed and
pushed into config as `[llm] extra`, which now carries anything provider-wide.
`_send` names only the four fields every endpoint requires — `model`,
`messages`, `max_tokens`, and the opaque `extra` — and a test reads its source
to keep it that way.

The lesson worth keeping: **an abstraction is only proven by a swap that was not
rehearsed.** Nothing in the test suite would have caught this, because a mock
transport accepts whatever it is handed.

### What kimi-k3 fixes, and what that costs

Two documented facts about the current model, both of which cost real time to
discover:

**`temperature` is fixed at 1.0.** Not configurable. A request must omit it
entirely — sending even the default value returns HTTP 400. This is exactly
what broke the swap, and it is why `[llm] extra` ships empty rather than
carrying `temperature = 0`.

**Thinking mode is always on.** `reasoning_effort` controls how *much*
reasoning happens, never whether it happens. There is no "off", which is the
other reason low is the floor rather than a saving to be optimised away.

Together they mean **K3 output is non-deterministic by design**. The same
candidate judged twice can get different verdicts, and no temperature or seed
setting will change that. That has a consequence for how the decision log
should be read: a row records what the judge concluded *on that occasion*, not
what it always concludes. A single surprising verdict is not yet evidence of a
biased judge — a pattern across many is. This is also why the M3 deferral of
judge tuning is right: tuning against noise produces confident nonsense.

### Per-task request extras

`[llm.tasks.<name>] extra = { ... }` is merged into the request body for that
task, with `[llm] extra` as the base. `llm.py` never looks inside either: no
named parameter, no validation, no branching on provider. A provider without
the feature has no blocks, gets an empty dict, and every request is the
baseline. That is what keeps the swap config-only.

### Provider abstraction

Transcript acquisition sits behind a `TranscriptProvider` interface with at least
two implementations. YouTube will eventually break one of them. This is the single
most important structural decision in the codebase.

Search sits behind a `SearchProvider` interface for the same reason: official API
primary, yt-dlp search as automatic overflow when the daily quota runs out.

### Quota is tracked locally, and checked before the request

A search costs 100 units against a 10,000/day default — roughly 100 searches. Spend is
recorded in the ledger's `quota` table and charged *before* the request is sent, for the
same reason `mark_pending` precedes the fetch: a crash mid-request must over-count
rather than under-count. A request that would exceed the cap raises `QuotaExhausted`
instead of firing and collecting an opaque 403 that looks identical to a revoked key.

### What falls through, and what surfaces

| Failure | Chain does | Because |
|---------|-----------|---------|
| `QuotaExhausted` | falls through | this is what the overflow provider is for |
| `ProviderUnavailable` (no key) | falls through, named in the report | not configured ≠ rejected |
| `TransientError` | falls through | the next source may be up |
| `PermanentError` (bad query, rejected key) | **surfaces** | it fails identically everywhere |

A fallback is never silent: the report names the provider that answered and, when it
fell back, which provider declined and why. A degradation nobody sees looks exactly like
a quota that never runs out — right up until the overflow breaks too.

---

## 9. Build order

| # | Milestone | Ships |
|---|-----------|-------|
| **M1** | Foundation | Plugin skeleton, ledger, file layout, ingest one URL |
| **M2** | Discovery | Brief, query expansion, cheap-model sorting, the loop-back |
| **M3** | Extraction hardening | Provider fallbacks, artifact cleaning, chapter capture |
| **M4** | ASR fallback | Videos without captions stop failing |
| **M5** | Understanding | Note worker, note schema, regeneration tooling — **built** |
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

- ✅ Ingesting a topic finds and stores relevant videos without supervision
- ✅ Running it twice re-processes nothing
- A video with no captions transcribes automatically — M4
- Ask answers with a clickable timestamp — M6
- ✅ "What does this video get wrong?" returns a real answer, not a summary. The note
  is where that answer lives; M6 only has to read it.
- ✅ All notes regenerate from stored transcripts with zero network calls, checked by
  running the regeneration with YouTube unreachable

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
│   ├── contracts.py             every structure that crosses a boundary
│   ├── llm.py                   all model calls, prompt loading
│   ├── ask.py                   load notes, escalate to transcript
│   ├── core/
│   │   ├── ids.py               canonical video ID extraction
│   │   ├── ledger.py            the three gates, decisions, quota
│   │   └── library.py           folder layout read/write
│   ├── providers/
│   │   ├── base.py              TranscriptProvider + SearchProvider interfaces
│   │   ├── ytdlp.py
│   │   ├── youtube_api.py       official search, metered
│   │   ├── ytdlp_search.py      overflow search, unmetered
│   │   └── whisper_local.py     faster-whisper small, int8, CPU
│   ├── brain/
│   │   └── judge.py             model output → verdicts (the parsers)
│   ├── flow/
│   │   ├── search.py            provider chain + gates 1 and 2
│   │   └── discover.py          brief → queries → judge → loop
│   └── enrich/
│       ├── note.py              background note worker
│       ├── timestamps.py        snap cited timestamps onto the transcript
│       └── normalize.py         artifact stripping, chapters, gap preservation
│
└── tests/
```

### The layered layout, and why it arrived when it did

The flat module list above was right for M1–M6 and stopped being right the moment the
discovery upgrade was scoped. That upgrade adds `territory.py` and `planner.py` —
modules whose entire job is to *decide*, and whose value depends on being testable
without a database, a network, or a machine. In a flat package there is nothing to stop
the first `Ledger` call being written into one of them, and once it is, nothing to
notice.

So the package now has four folders, drawn along the axis of what a module is allowed to
touch rather than what it happens to be about:

| Folder | Holds | May touch |
|---|---|---|
| `core/` | identity, ledger, file layout | the disk and the database — it *is* the state |
| `providers/` | the part guaranteed to break | the network, and yt-dlp |
| `brain/` | reasoning | `llm` and `contracts`. Nothing else. |
| `flow/` | execution | everything — that is what it is for |
| `enrich/` | what happens to a transcript after storage | the library, `llm` |

`brain/` is the one that matters and the only genuinely new rule. It is drawn now, with
one file behind it, because a boundary added to three files that already cross it is a
refactor, and a refactor is exactly what does not happen under deadline.

`contracts.py` is the other half. Types defined where they are produced force `flow/` to
import `brain/` to name a verdict and `brain/` to import `providers/` to name a search
result — each import defensible on its own, the import graph as a whole cyclic. One file
holding every structure that crosses a boundary makes the graph acyclic by construction.
Protocols and exception types stay in `providers/base.py`: a Protocol is the shape of an
implementation, not a structure in flight.

The move itself changed no behaviour — imports, file paths, `contracts.py`, and the
boundary tests, and nothing else. `tests/test_boundaries.py` enforces every rule below;
the ones this project has actually held held because a test enforced them, and the ones
written down and left unchecked drifted.

### A.1 — closing what A found

Phase A left two allowlisted violations, each pinned by a test asserting its exact
contents. A.1 closed both, and the pinning tests were deleted along with them — they
existed to fail when the debt was paid.

**`brain/` reached `core/ledger.py`** for `VERDICT_YES` / `VERDICT_NO` /
`VERDICT_MAYBE`, which pulled sqlite3 into any process that imported the judge. The
three strings now live in `contracts.py`. `brain/` has **no exemptions**.

**`config.py` held `DEFAULT_LLM_MODEL` and `DEFAULT_LLM_BASE_URL`.** Deleted, and both
keys are now required. The safety argument decided it: that default named the provider
this project abandoned *because it stopped completing requests*, so a config missing
the key silently reached a dead endpoint and failed three layers from the cause. There
is also no provider-neutral model name — any default is a vendor chosen on the user's
behalf. This is the one intentional behaviour change in A.1.

It produced a second, unintentional one, which the hook tests caught immediately.
`marg guard-write` fails **open** by design — a guard that cannot read its own input
must not block every edit in the repository. It reached the library path through
`load_config()`, so an incomplete `[llm]` block raised, the fail-open branch caught it,
and **hard rule 2 silently switched off**: a guard that stops guarding while still
looking present. The guard now reads `[library]` alone via `config.library_root()`,
and two tests hold that shut. A rule that only holds when an unrelated section of the
config is complete is not a rule.

**The remaining fourteen boundary-crossing types moved too**, along with the vocabulary
constants their fields take as values — leaving those behind would only have moved the
import backwards. The `Citation` collision found by `test_no_shared_structure_is_defined_twice`
was resolved first: `ask.py`'s became `AnswerCitation`, `enrich/timestamps.py`'s became
`NoteCitation`. `Config` and its sub-configs stay in `config.py`; configuration is what
a run is set up with, not data moving through it.


### B1 — the status / verdict split

`videos.status` carried two ideas: what we hold, and what we decided. The second is
topic-relative and the first is not, so a video declined for prompt caching was
globally labelled `rejected` while being a perfectly good video about Redis — and
every later feature would have written into that confusion.

Status is now asset state alone:

| State | Means |
|---|---|
| `seen` | Known to us. Nothing held. No fetch in flight. |
| `pending` | A fetch started and did not finish. **A crash indicator.** |
| `has_transcript` | `transcript.txt` is on disk |
| `needs_asr` | No captions available; awaiting transcription |
| `failed` | Acquisition failed — see `fail_kind` |
| `duplicate` | Same content as another video |

Migrated in place: `stored` → `has_transcript` (37 rows), `rejected` → `seen` (15).
A value rename rather than a change of meaning, which is why it migrates instead of
forcing the rebuild the no-migration rule demands elsewhere — and all 15 of those rows
still carry their NO verdict in `decisions`, checked by query, which is what makes
dropping the label lossless.

**Gate 1 changed meaning, not just vocabulary.** It used to drop anything `stored`.
It now drops anything *in the library*, and a video is in the library if it has a
note — `/ask` loads notes, so note existence is membership. `ask.py` already selected
exactly this way.

That unshadowed something Gate 1 had been hiding: a video can hold a transcript and
not be in the library, and discovery would then re-fetch it. `write_transcript` caught
it immediately by refusing to overwrite an immutable file, which is the guard working.
The fix is a second, separate check at the fetch step — *do we already hold the
bytes?* — because that is a different question from *is this in the library?* and B1
is about not conflating questions. No network call, no overwrite, and `marg note`
picks the video up from `has_transcript` regardless of which topic first found it.

Also added, with no consumer until later: `videos.channel_id` (never backfilled — 64
rows would have meant 64 network calls for a column nothing reads), `decisions.evidence`
and `decisions.prompt_version` (B3), `runs.territory_map` and `runs.territory_version`
(B4), and the `queries` table (B4, B5). `queries.origin` is the column that makes
"why did it search for *that*?" answerable at all; without it a run's queries are a
list with no causal story.


### B2 — keep what was fetched, and enrich stage 1

**A stage-2 NO now stores its transcript.** It used to drop the bytes and record
`seen`. That repeated the one step this plan calls fragile, for content already in
hand, and the fragility is measured rather than assumed: thirteen of thirteen fetches
died in nine seconds when YouTube throttled a run. A video rejected for prompt caching
and wanted later for Redis now costs zero network calls the second time — Gate 1 lets
it through because it has no note, and the fetch-step check finds the file.

Nothing else had to change to make it safe, which is the sign B1 drew the line in the
right place: `in_library` is False without a note, so `/ask` cannot reach it; the
fetch-step disk check skips the download; `marg note` finds it under `has_transcript`
if a later topic ever keeps it.

**Note selection had to become a real question.** Holding a transcript stopped meaning
anyone chose the video, so `wanted_for_a_note` decides instead, keyed on a stage-
`opening` NO — checked against the live library, it identified all fifteen rejections
and nothing else. Overridden by an existing note, by an acceptance for any topic, and
by having no verdicts at all, which is what `marg ingest <url>` produces.

**Chapters are parsed out of the description at stage 1.** A creator's table of
contents is the only structural view of a video that exists before anything is
downloaded, and stage 1 was judging from the raw blob. `providers/chapters.py` refuses
unless several timestamped lines run in sequence, start at or near zero, and ascend —
a fabricated table of contents is worse than none, because it reads as confident
structure for a video that never had any. Parsed before `_text` flattens the
description, which is a mistake the provider test caught.

**`contentDetails.caption` is recorded and not believed.** The phase asked for a skip
when it says false. Measured against the 40 videos in the library whose caption
outcome is known, the API called 31 of the 37 working ones captionless. It is not a
noisy signal but an exact one answering a different question: **it counts manually
uploaded tracks only** — 6 of 6 manual right, 31 of 31 auto-generated wrong, because
yt-dlp reads auto-captions and the API does not count them. The mechanism is built and
tested; `[discover] trust_caption_hint` gates it and ships false. Turning it on today
would route most of a run into `needs_asr`, whose worker is M4 and does not exist.


### B3 — judging on the whole transcript

Stage 2 read roughly the first thousand words. That cannot detect padding: a video that
opens well and never pays it off is identical, for a thousand words, to one that
delivers. It now reads everything.

**Thinking off, deliberately.** `[llm.tasks.judge_full]` runs at
`reasoning_effort = "none"` with `temperature = 0`. With the complete evidence in hand
the model is reading and deciding rather than inferring beyond what it was given, and a
constrained five-line classification has nothing to reason its way toward. Two
consequences, both wanted: `temperature = 0` becomes available, so the same transcript
tends toward the same verdict; and the runaway-reasoning failure that returned an empty
note after spending 32,768 tokens thinking becomes structurally impossible.

Measured on the longest transcript in the library — 14,341 words, 40,825 input tokens —
`finish_reason` was `stop`, 132 output tokens, 11.3s, $0.0058. Across ten real videos:
2.5s and 0.09¢ each.

**The parser enforces the contract.** Four rules, all guarantees rather than requests,
and every violation resolves toward keeping. The one that matters most is not the one
originally written down: "a NO must carry EVIDENCE: CLEAR" catches only a judge honest
enough to *label* its evidence as degraded. The failure that actually occurred was a
judge that said `NO` + `CLEAR` with the reason *"the transcript is garbled and contains
no substantive content"* — the mislabelled version, which is the one that costs a video.
So the rule is checked against the reason as well as the label.

**Re-judging ten previously-kept videos, three flipped to NO — and all three old
verdicts were factually wrong.** They claimed concrete cost numbers and worked examples;
the transcripts contain zero dollar amounts and zero code between them. Two of the three
were under a thousand words, so the *old* judge had read them in full as well: the
change that caught this was not more evidence but the instruction to use only the
evidence supplied. The old judge was filling in what a video with that title usually
contains.

**Stage 1 gained chapters and the untrusted-data rule.** B2 delivered chapters without
teaching the judge what they are; now it knows they are the author's own outline, which
is better evidence than a description blurb — and that a description is editable by any
uploader in ten seconds, which makes stage 1 the *more* exposed surface, not the less.

**Counters separated** before B5's planner reads them. `failed` means a fetch failed and
nothing else; `no_captions`, `already_held`, `degraded` and `unparsed` each got their
own.

### B4 — territory mapping, and filters the code decides

Search "AI agent memory" and you get videos with those words in the title. The video
called *"Building ReAct loops with tool calling"* is exactly what was wanted and never
says "agent". Nothing downstream recovers it: the judge only ever sees what the search
returned, so a gap here is invisible at every later stage. Careful judging cannot fix a
narrow search.

So before searching, a model works out what the subject actually covers — subtopics,
alternative names, practitioner jargon, tools, people — and the queries are written from
**that**. On "prompt caching" the map produced `llm prefix caching`, and that one query
returned *PagedAttention: Behind vLLM's Insane Speed*, *llm-d Precise Prefix-Cache-Aware
Routing*, and *6 Types of Caching in AI Systems* — none of whose titles contain the word
"prompt". Across the round, **39 of 129 candidates were surfaced only by map-derived
queries**, against a baseline run that saw 60 candidates from six brief-derived ones.

The map runs **once**, before round one, and is never revised. Revising it between
rounds is B5's planner; a map re-run each round would be that, arrived at by accident.
Its queries run *alongside* the brief's own expansion rather than replacing it, so
`queries.origin` answers "did the map help?" from the ledger instead of from an opinion.

**The risk this phase carries is the one B3 measured.** That phase found this judge
confabulating from suggestive context — three videos kept on reasons describing numbers
their transcripts do not contain, two of them short enough that the judge had read them
in full and completed the pattern the title implied anyway. A territory map is a page of
suggestive context, and both judging stages now receive one.

So the rule goes in as a rule, in both prompts, with a worked example: *a term appearing
in the map is never, on its own, a reason to keep a video; the brief decides what counts
as relevant.* A LangChain document-loading tutorial is not about agent memory because
LangChain is in the vocabulary. The judge is shown the concepts and vocabulary flat, and
never the queries, the filters or the map's reasoning — handing it those would be handing
it the conclusion it is supposed to check independently.

Checked rather than assumed. Stage 1 was run over 64 candidates with the map present and
absent, on queries chosen to be maximally tempting — LangChain cache tutorials, KV-cache
transformer explainers, semantic-caching videos, LangGraph and pgvector tutorials, all of
them dense in map vocabulary and named in the brief's EXCLUDED section. **Zero moved
toward keeping.** One moved the other way. Two of the three passes had a noise floor of
zero, so the arms were stable enough for that to mean something.

### Filters, and why the code decides

The map may attach search filters, and the four are not equivalent:

| Parameter | Behaviour | Use |
|---|---|---|
| `publishedAfter` | **hard — excludes** | only when recency was reasoned to matter |
| `videoDuration` | **hard — excludes** | never on a main query; only to *add* a variant |
| `order` | sorts only | free width: the same query by relevance and by date returns different slices |
| `relevanceLanguage` | soft — ranks | safe; other languages still come back |

The model proposes; the code checks. Six rules reject: an unsupported field, a malformed
or future `publishedAfter`, a value outside the allowed set, a `videoDuration` on a main
query, a filter contradicting the reasoned recency or the user's own words, and a
duplicate of a query already run.

**Every rejection widens.** It drops the filter, never the query, and it is counted and
logged — the same discipline as the unparsed counters. That direction is the search-layer
form of the asymmetry the judge follows: a hard filter applied by mistake removes videos
before anything in the system has seen them, which is the one failure nothing downstream
can detect.

`videoDuration` is the trap of the four. A rule that drops Shorts is the duration bias
`judge_metadata` spends four paragraphs forbidding, relocated to where nobody would look
for it. It is allowed only on a query that repeats one it names, so the band adds a slice
instead of removing one — and a real map used it exactly that way, running
`cache_control anthropic` unfiltered *and* as a short-form variant.

### Recency is reasoned, never ruled

Measured on three real topics, each with its stored reason:

| Topic | Call | Reason it gave |
|---|---|---|
| prompt caching | 6 months | provider pricing for cached tokens changed multiple times in 2025 |
| AI agent memory | 24 months | the practice emerged with LangGraph and MemGPT in 2023; older material is about RAG |
| SQLite WAL mode | any time | stable since 2010; the pragma, sidecar behaviour and durability trade-offs have not changed |

No rule produces all three. `ANY` is the default and the wider answer, and the reason is
printed in the report rather than only stored, because a call that silently excludes and
cannot be inspected is one nobody can correct.

### What the run cost, and what it found

The mapping call is one request per run: **2,187 in, 4,032 out, 30s, $0.0014** on the
prompt-caching run — about a fifth of what the brief costs, and under 6% of the run.

It has a measured effort setting, and it is the third time on this provider that the
ladder has not been monotonic. At `reasoning_effort = "high"` the first real mapping call
spent all 8,192 output tokens and returned **nothing** — the same failure `write_note`
has on long structured output. At `low` it returns a valid map in 27 seconds. The guard
held: a truncated reply is discarded rather than parsed, because a half-read vocabulary
looks like the subject's full extent, so the cost of getting this wrong was a narrower
run rather than a wrong one.

Two parser weaknesses were found by real replies rather than imagined. A filter reason
containing a semicolon was cut in half by the separator, rejecting a harmless `order`
filter twice; and a parenthesised list of tool names was cut by the comma split into
fragments with dangling brackets. Both failed safely and both were counted, which is the
design working. Both are fixed, with the observed strings as the tests.

### Recording what was searched, and why

The `queries` table gets its writer. Every search lands one row: the text, the origin
(`brief` | `territory` | `thread`), which map term produced it, the filters *with the
reason given for each*, how many results came back, which provider answered, and what it
cost in that provider's units. `runs.territory_map` stores the map whole.

Without these, *"why did it search for that?"* has no answer — and it is the first
question asked when a round goes wrong. The same argument as storing the brief: the
queries are the conclusion, and the map is the reasoning that produced them.

### B5 — the round planner

Until this phase the agent planned all of its searching before seeing a single result: a
brief, a map, then three rounds of the same idea rephrased. One call at the end of each
round now reads everything the round produced and answers two questions — *what is the
brief still missing*, and *how should I look for it?*

It sees the queries that ran with their origins and result counts, every video kept and
rejected with the judge's own reason and the stage that decided, the concepts stage 2
found mentioned but unexplained, the round's counters, and the quota left. It returns a
coverage assessment against the brief, the next round's queries, optional map revisions,
and a recommendation to continue or stop.

Its queries run **first** in the next round — they were written against the gaps that
round actually left open, while expansion is still working from the brief alone — and the
brief's expansion still runs alongside them, for B4's reason: a new step that silently
replaced a working one would make its own contribution unmeasurable. `queries.origin`
gets its third writer, and "did the planner change anything?" becomes a query against the
ledger rather than an impression.

### The risk, and why it is the whole design

**Rejection reasons come from another model's judgement, not from the world.** The
concrete case is in this project's own history: when the judge had a duration bias,
twelve of twenty-five rejections in one round cited runtime. A planner reading that
pattern would conclude *"we keep rejecting short videos, so stop searching for them"* —
which reads as intelligent adaptation, and would have permanently blinded the library to
short-form content because of a bug in a different component. The redirect would have
looked like the system working.

**Consistency is what a systematic error looks like from the outside.**

So the prompt states, as a rule with that failure attached: a rejection reason is evidence
about what the judge decided and not a fact about what exists on YouTube; the brief is the
authority; the primary question is what the brief asks for that has not been found, never
why things keep being rejected.

Two structural halves back it up, because a rule that lives only in a prompt is a rule
enforced by a model. The planner is shown **what was kept as well as what was rejected** —
a planner given only failures only ever learns to avoid, and coverage is a claim about
what the library holds, which cannot be made from failures. And **a planner query may
never carry `videoDuration`**: it is a main query by construction, so B4's validator
refuses the band outright. The worst version of the failure cannot happen whatever the
prompt achieves.

**Tested rather than reasoned about.** A synthetic round was built with twenty of
twenty-five rejections citing runtime — worse than the historical case — and handed to
the real planner. Three samples, and every one of them held. It named six coverage items
against the brief, three of them gaps, proposed five queries aimed at those gaps, and
proposed no duration filter and no length-based strategy at all. It also said what the
pattern was, unprompted:

> continue — cache expiry/TTL and cross-provider comparison still have nothing against
> them, and the heavy runtime-based rejection pattern in round one is more likely judge
> bias than evidence that short videos are irrelevant.

and, on the second sample:

> note that 15 of 25 rejections in round 1 were runtime-based, which looks like judge
> bias rather than a property of the subject, so I have not filtered by length in any
> query

The only duration-shaped words anywhere in either plan were in those two sentences. (The
second miscounts 20 as 15 — an arithmetic slip, not a reasoning one.)

### Revising the map

A wrong map inherited by every round is worse than no map, so the planner may change it —
under the asymmetry this project applies everywhere.

**Additions are free.** A concept the map missed, a term the kept videos keep using. The
worst case is one query whose results are visible.

**Removals require evidence**, checked in code rather than asked for in the prompt.
Removing a term narrows every later search, and a class of video that stops being searched
for is one nobody notices the absence of — rule 8's invisible error, moved to the search
layer. So a removal must name a term the map actually holds, quote a query that really
ran, and be one of at most three that round. The check is deliberately mechanical: it
cannot know whether reasoning is sound, but it can know whether the claim is about
searches that happened. *"This term feels off topic"* is refused; *"the query `prompt
caching vs semantic caching` returned ten results, all rejected"* is accepted.

**Every revision is versioned.** A revision produces map v2, and v1's term lists stay
beside it on the run along with the diff and the reason. Same argument as storing the map
at all: a change you cannot see the before of is one nobody can judge.

### Stopping, and telling the reasons apart

```
stopped_because = coverage_satisfied | no_new_videos | max_rounds
                | max_videos | max_requests | max_cost | quota
```

`coverage_satisfied` is its own word and that is the point. A run that stopped because
the brief was covered and a run that stopped because it ran out of money are opposite
outcomes, and one label holding both would hide the interesting one. The planner is
advisory, never sovereign: every budget ceiling is checked before it is consulted, and a
planner declaring satisfaction in round one is a real failure mode that is only findable
because it is labelled.

`quota` fires **before** a round, not inside one. A round that runs out of allowance
halfway leaves the planner reading half a round's evidence as though it were the whole
thing — which is the one input this design must never produce. It only fires when every
provider in the chain is metered: an unmetered overflow provider means there is no ceiling
to hit, and stopping then would be inventing a limit that does not apply.

### Two fixes carried from B4

**`ytdlp_search` no longer records filters it did not apply.** The fallback runs the query
unfiltered, which is the right direction — the two filters that matter *exclude*, so
ignoring them returns a superset — but the `queries` row still stored the proposal. The
planner now reads that history to decide what to try next, and *"we already searched this
with `order=date`"* is false when the search that ran was plain. Providers report
`applied_filters()`, and the row records what ran.

**A quota guard.** Fourteen searches at ~101 units is ~1,400 per run and the planner adds
rounds on top. `quota_headroom` asks the chain what it has left and what a search costs,
before the round starts.

### What a real run looks like

`marg discover "postgres connection pooling" --max-rounds 3 --max-videos 14` — a fresh
topic, three rounds, ten videos stored, 49 requests, **$0.048 and 7.2 minutes**.

| round | queries | candidates | stored | what the plan said |
|---|---|---|---|---|
| 1 | 6 brief + 9 territory | 141 | 6 | 5 of 8 brief items covered; 3 named gaps |
| 2 | **4 planner** + 6 thread | 66 | 3 | 1 of 7; added `PgCat` to the map |
| 3 | **6 planner**, nothing else | 27 | 1 | — (last round is never planned) |

**By round three the planner was the only source of new searching.** Query expansion
still ran and produced nothing that was not already a duplicate; every query that
executed came from the plan.

The round-one coverage assessment is what the phase is for. It named videos against brief
items — *"both videos reproduce the exact error by connecting many clients directly"* —
and its three gaps were specific enough to write queries from:

> **NO** transaction vs session pooling with SET/LISTEN/NOTIFY pitfalls — prepared
> statements are covered, but no kept video explicitly names SET or LISTEN/NOTIFY
> breakage or workarounds.
>
> **NO** pool sizing rule of thumb — no kept video mentions a formula like cores*2+disk.

which became `pgbouncer set session state transaction pooling` and `pgbouncer pool size
calculation rule of thumb`.

**The clearest evidence that this is not the brief restated** came from a second run,
`sqlite wal mode`, where the planner read a *result count*:

> `sqlite checkpointing` — from: gap — checkpointing is a MUST CONTAIN item; the round 1
> query "SQLite checkpoint wal_autocheckpoint" returned 0 results, so the concept needs a
> different phrasing

Nothing in the brief or the map could produce that. It required seeing what round one
returned.

**Honestly, though**: the planner rarely introduces new *subject* vocabulary into query
text. Its round-two and round-three queries recombine terms the map already held against
gaps it named, and the only words appearing in neither brief nor map were "tutorial",
"comparison", "calculation", "example". That is a consequence of B4 working — the postgres
map already listed PgBouncer, Pgpool-II, Odyssey, RDS Proxy and Supabase pooler before
anything was searched — and it is where new vocabulary now enters instead: stage 2
surfaced `PgCat` as mentioned-but-unexplained, a thread query used it, and the planner
noticed it was missing from the map and added it. Three mechanisms, one term.

The one territory revision across both runs was that addition, recorded with its reason
and versioned to `1/v2`, with revision 1's term lists kept beside it. **No removal was
proposed in any run**, so the evidence rule was never exercised outside its tests — which
is a result rather than a gap, and the safer of the two directions to have gone untested.

### What the planning call costs, and the fourth ladder

**One request per round, ~2,600 in and ~2,500 out, 22–31s, $0.0011.**

Effort was measured, not chosen, and this is the fourth non-monotonic ladder on this
provider. Same input each time:

| effort | finish | output | latency | result |
|---|---|---|---|---|
| low | stop | 2,618 | 30.6s | valid — 5 queries, 6 coverage items |
| low | stop | 2,338 | 22.0s | valid — 5 queries, 6 coverage items |
| high | stop | 7,670 | 75.7s | valid, 522 tokens under the ceiling |
| high | **length** | 8,192 | 70.7s | **truncated, discarded** |

`high` is not merely expensive here, it is *unreliable*: it converged once with 6%
headroom and hit the ceiling on the next identical call — and that was a small round, so a
real one has less margin. The truncation guard held both times, discarding the fragment
rather than parsing it, because a half-read coverage assessment reads as a complete
account of what the brief is missing.

Four times out of four now, on this provider, "the most important call deserves the most
thinking" has been the wrong instinct whenever the output is long and structured.

### B6 — nothing enters the library unread

Three findings, all from exporting run `19941ec23cb7` and then testing an `/ask` answer
against the planner's own coverage claim.

`/ask` answered a question the planner had declared a gap — *"SET/LISTEN/NOTIFY pitfalls
not covered in any kept video"* — citing `EGNykTqFDBs` three times as the authority. The
export says why. That video has **no `full`-stage decision row at all**. It was accepted
at stage 1 on its title and description, stored, and its transcript was never judged. The
planner's entire evidence for it was one sentence:

> shows connection sharing and queueing behavior with real setup

The planner was not wrong. Given that sentence, "SET/LISTEN isn't covered" is the correct
reading. It reported honestly on the only evidence it had, and the video's actual content
surfaced later — when `marg note` read the transcript, after the run was over.

**Every keeper goes to stage 2.** A stage-1 YES used to skip it, with the comment that it
"needs no second model call, so it is the cheaper keeper". That was true when stage 2 read
an opening at `max` effort, and it stopped being the relevant trade at B3. What the
shortcut actually bought was a video entering the library that nothing had read — and B3
established, with evidence, that this judge confabulates from metadata: three videos kept
on reasons describing numbers their transcripts do not contain. All three were caught
because they were MAYBEs. A confident YES never reached the check.

So stage 1 is now a filter deciding whether to **fetch**, and stage 2 is the verdict
deciding whether to **keep**.

**Two counters, not one.** A MAYBE stage 2 declines is `flipped_to_no` and is stage 2
doing its job. A YES stage 2 declines is `overturned` and is stage 1 having been wrong.
One integer holding both would hide the only number that says how far a metadata verdict
can be trusted on its own — rule 14 applied to the case it was written for.

#### Measured: the same topic, twice, on two empty libraries

`marg discover "postgres connection pooling" --max-rounds 3 --max-videos 14`, run at
5a15185 and again with the change, each against a fresh library.

| | before | after |
|---|---|---|
| videos fetched and held | 39 | 47 |
| **held but never read by stage 2** | **11** | **0** |
| kept (in the library) | 14 | 8 |
| keep rate, kept / fetched | 36% | 17% |
| keep rate *of what stage 2 read* | 3/28 = **11%** | 8/47 = **17%** |
| stage-1 YES rows / distinct videos | 20 / 14 | 16 / 16 |
| videos judged twice at one stage | 9 | 1 |
| requests | 52 | 69 |
| cost | $0.0463 | $0.0866 |
| wall clock | 10m 24s | 12m 29s |
| stopped because | max_videos | max_rounds |

**The headline keep rate halves, and that is not stage 2 getting harsher.** Stage 2 kept a
*larger* share of what it read after the change than before — 17% against 11%. What
disappeared from the numerator is the eleven videos nobody had read. The right reading of
"36% → 17%" is that the old figure was counting unread videos as keepers.

Two caveats stated rather than buried. The briefs differ between the runs — a brief is
model-written per run and non-deterministic, and the second one is stricter, 12 coverage
items against 8 — so some of the delta is brief variance rather than this change. And of
the seven videos that were a stage-1 YES in *both* runs, four flipped to NO and one more
came back `DEGRADED`: 4 in 7 is well above B3's 3 in 10, and worth watching.

The confound-free comparison lives inside the second run, where one brief judged
everything: **a stage-1 YES survives a read 40% of the time (6 of 15); a stage-1 MAYBE
survives 6% of the time (2 of 32)**. Both halves matter. Stage 1 carries real signal — a
YES is roughly seven times likelier to survive — and it is still wrong about the majority
of the videos it is confident about.

#### What the transcript said about the videos stage 1 was sure of

The clearest case is `mtCAb1uFDxg`. Stage 1 kept it on *"dedicated PgBouncer video,
**likely** covers configuration and pooling"* — the judge's own hedge, recorded as a keep.
Stage 2 read it and graded it `DEGRADED`: *"largely garbled and incomplete, with the
substantive demo portion cutting off mid-sentence"*. Under the old code that video was
stored, noted, and answerable from. Under the new one it is stored, not noted, and
`/ask` cannot reach it — which is what rule 13 already specified for every other video.

Three of the eleven unread keepers were held on a reason containing the word "likely".

The planner is the other half of the evidence. At the end of round two the **old** run said:

> **NO** root problem (max_connections, memory per connection) — no kept video confirms
> this is explained; only a title suggests it, with no transcript

— all eight brief items marked uncovered, by a run that had stored fourteen videos. The
**new** run's round-two assessment marks ten of twelve covered and names the videos:

> **yes** transaction-level vs session-level pooling with application impacts (prepared
> statements, LISTEN/NOTIFY, temp tables, advisory locks) — all three kept videos
> explicitly distinguish pooling modes and name session-state traps

That is the reported finding, closed, on the topic it was found on.

#### Gate 1 asks two questions

`EGNykTqFDBs` was judged and accepted twice in run `19941ec23cb7` — round 1 and round 2.
`in_library` asks whether a video has a note, notes are written by a worker that starts
after the run ends, so a video stored in round one is not in the library during round two.
Nine videos were judged twice at the same stage in the baseline run.

The fetch-step disk check meant no re-download, so the cost was model calls and inflated
counters — and **the planner reads those counters**, so a kept video counted three times is
two findings that do not exist.

`in_library` is unchanged, and deliberately. The tempting fix — let it accept a
transcript — is wrong twice: a stage-2 NO also holds a transcript and must stay
re-judgeable for a later topic, and `/ask` cannot answer from a video with no note, so
calling it a member is a claim the library contradicts. Instead the run's own
`stored_ids` is passed to the gate as a second question, exactly the way B1 separated
*is it in the library* from *do we already hold the bytes*. A test pins `in_library`
against this specific pressure.

The skip logs `maybe`, never `no`. Gate 1's library branch logs a `no`, which is right
there; logging one here would tell Gate 2, on every later run on this topic, that a video
this run *kept* had been turned down — rule 8's invisible error arriving through a counter
fix. Only kept videos ever needed this: a stage-2 NO writes a `no` for the topic, so Gate
2 already stopped those.

One duplicate survives in the new run and it is correct. `B35HeNLm2Mw` has no captions —
`needs_asr`, nothing fetched, nothing stored, no verdict recorded — so a later round is
entitled to look at it again. The same is true of the one stage-1 YES with no stage-2 row.
The invariant that matters holds exactly: **of 47 videos holding a transcript, 0 were
never read.**

#### The planner's reasoning is stored, not just its queries

`marg run export` wrote the queries a plan produced and the short gap label on each. The
assessment itself — the coverage table, the reasoning, the recommendation — existed only
in memory and was gone when the process exited. So *"why did the planner decide that?"* was
unanswerable, which is what storing runs was meant to fix.

The plan is now a row per round in `plans`, stored whole as `RoundPlan.as_dict()`, and
rendered under its round in `report.md`. Written **before** its revisions are applied, so
a crash between the two leaves the reasoning on disk rather than only its consequences.

It paid for itself on the first real run. The round-one plan carried a parse problem the
CLI line only hinted at — four coverage lines the parser could not read — and the report
now says so in place, above the partial table:

> This reply could not be parsed. Everything below is whatever survived, and a plan the
> code could not read is a plan the loop largely ignored.

A run that predates the table says which kind of nothing it has, because "the planner said
nothing" and "nothing recorded what it said" are different claims:

> *No plan was stored for this round. Runs before the `plans` table gained a writer have
> none — their planning survives only as the gap label on each query it produced.*

#### What it costs

19 extra stage-2 reads on this run: **+$0.040, +2m 05s, +17 requests** — about 0.21¢ and
6.6s per extra video. B3 measured 0.09¢ and 2.5s; these transcripts are longer and the
limiter is set to 20 requests a minute, so treat B3's figure as the floor rather than the
estimate. Against a run that costs 8.7¢ and takes twelve minutes, the price of reading
everything before keeping it is 46% of the bill and 20% of the clock.

### Structural rules

- `providers/` is a folder because it is the part guaranteed to break. Adding a source
  means adding one file, never a refactor. Nothing outside `providers/` imports yt-dlp.
- `brain/` reasons and returns decisions; `flow/` executes them. `brain/` performs no
  filesystem, database, network or provider access.
- Every structure crossing a module boundary is defined in `contracts.py`. No bare dicts
  across boundaries.
- `llm.py` is the only place model names appear. Switching tiers is one file.
- `cli.py` is the only entry point. Skills are thin — they call the CLI and read the
  report. Logic never lives in a SKILL.md.
- `core/library.py` builds every library path. No other module constructs one.
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

### Milestone 2a adds

`search.py`, `providers/youtube_api.py`, `providers/ytdlp_search.py`,
`hooks/hooks.json`, the `marg search` and `marg guard-write` commands, and the
`decisions` and `quota` tables.

`search.py` is a separate file from `discover.py` (M2b) because finding candidates and
deciding which are *known* is free, deterministic, and testable without a model, while
deciding which are *good* is none of those things. They will change for different
reasons.

### Milestone 5 adds

`note.py`, `prompts/write_note.md`, the `marg note` command, a `[note]` config block,
and `note_version` / `note_model` / `noted_at` on `videos`.

Those three columns are added **in place** rather than by rebuilding. The no-migration
rule held for `rejections` → `decisions`, where a table changed meaning; it does not
hold here, where three nullable columns read NULL on existing rows and that is the
honest answer to "which prompt wrote this note?" for a video noted before notes
existed. A library of transcripts predating notes is exactly the library notes are
for; destroying it to add a column would trade the irreplaceable artifact for the
regenerable one.

`note.py` mirrors the `judge.py` / `discover.py` split internally — pure section
parsing at the top, the drain loop at the bottom — for the same reason: the prompt
will change many times and the loop will not.

The worker deliberately cannot reach a provider. It reads transcripts through
`library.py` and calls the model through `llm.py`, and nothing else. That is what
makes "notes regenerate with zero YouTube calls" a property of the code rather than
a habit of the caller.

### Milestone 2b adds

`llm.py`, `judge.py`, `discover.py`, `prompts/`, the `marg discover` and
`marg llm-check` commands, the `runs` table, and `run_id`/`model` on `decisions`.

`judge.py` sits between the other two rather than inside either. `llm.py` knows
how to make a call and nothing about verdicts; `discover.py` knows the loop and
nothing about parsing. Between them is the part most likely to break — turning
free text into a decision — and it is worth isolating because the model is
temporary and the prompts will change while the loop does not.

The three-way split also matches the test surface: `test_judge_parsing.py` is
pure functions over strings, `test_llm.py` is pacing and retries with an
injected clock, `test_discover.py` is control flow with everything mocked.

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

### Model calls: settled, and not through Claude Code

M1's plan left this open — prototype judging with Claude Code subagents, then move
it into `llm.py` when unattended runs were needed. **M2b settled it: all model
calls go through `llm.py` against an OpenAI-compatible HTTP provider.** Subagents
were not used at all, and `agents/judge-relevance.md` was never written.

| | Python → API (`llm.py`) | Claude Code subagents |
|---|---|---|
| Billing | Provider key | Uses the session |
| Model choice | `config.toml` | Frontmatter |
| Unattended batches | **Yes** | No — session only |
| Rate limiting | Ours, and blocking | Not exposed |
| Provider swap | Three config values | Tied to Claude Code |

The deciding factor was never billing. It is that a discovery run must be
reproducible, rate-limited, and diagnosable from the `decisions` table months
later, and a subagent judging inside a conversation is none of those. The
loop-back that makes discovery work is a `while` in `discover.py`, not a model
deciding to search again.

The consequence is that this project is only a *Claude Code plugin* at its
edges — skills that call a CLI. The intelligence is in Python, which is also
what lets it run unattended for M4's ASR queue and M5's note worker.

### Still worth using

- **`hooks/`** — `PreToolUse` blocks direct writes to the library. Enforcement,
  not instruction. Shipped in M2a and verified firing.
- **`bin/`** — CLI on the Bash tool's PATH while the plugin is active.
- **`isolation: worktree`** — isolated checkouts if two agents ever work the
  codebase concurrently.

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
  when the prompt improves. Built in M5 as `videos.note_version`, which makes "what is
  stale?" a query rather than a list.
- **A note asserting something the transcript does not contain, in the transcript's
  voice, is the failure mode that matters most.** M6 answers from notes, so an
  unmarked inference becomes an asserted fact two layers from any transcript that
  could contradict it. Everything not from the transcript carries `[note: ...]`;
  inference is welcome, indistinguishability is not.
- **A rule stated inside one section is scoped to that section.** v2 told the note to
  mark outside knowledge, in the *Who's talking* paragraph — and that is exactly and
  only where it complied, while *Leaves out* asserted provider prices and token
  thresholds no transcript contained. General rules belong in a general place, and it
  took an audit rather than a review to see it.
- **Automatic captions mangle exactly the terms a note cares about.** A garbled
  threshold at the moment the speaker states the most important number is worth
  recording — but as the transcriber's error, not the speaker's. `caption_kind` is
  passed to the note prompt so the two can be told apart; without it a note puts
  mistakes in people's mouths that they did not make.
