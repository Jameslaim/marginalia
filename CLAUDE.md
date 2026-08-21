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

   **`llm.base_url` and `llm.model` are required and have no default.** `config.py`
   held one of each until A.1, which made this rule literally false and — worse —
   meant a config missing the key silently reached the provider this project
   abandoned for not completing requests. There is no provider-neutral model name:
   any default is a vendor chosen on the user's behalf, silently. A missing key now
   names itself, its section, and the file. The `guard-write` hook deliberately does
   *not* go through `load_config()` for this reason: it reads `[library]` alone, so
   an incomplete `[llm]` block cannot switch off hard rule 2 through the fail-open path.

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

9. **`brain/` reasons and returns decisions; `flow/` executes them.** `brain/` performs
   no filesystem, database, network or provider access — it may call the LLM abstraction
   only. A reasoning layer is worth having because it can be tested without a machine,
   and that property is worth exactly as much as it is enforced: `tests/test_boundaries.py`
   scans the imports. `brain/` gains `territory.py` and `planner.py`, which is why the
   boundary is drawn now, while there is one file behind it, rather than after there are
   three and the first `Ledger` call has already been written.

10. **Every structure crossing a module boundary is defined in `contracts.py`. No bare
    dicts across boundaries.** A type defined where it is produced makes `flow/` import
    `brain/` to name a verdict and `brain/` import `providers/` to name a search result —
    each import individually reasonable, the graph as a whole cyclic. **Shared vocabulary
    counts**: the `VERDICT_*`, `NEW`/`KNOWN`/`REJECTED`, `NOTED`/`SKIPPED`/`FAILED` and
    `STOP_*` strings are the values those structures' fields take, and leaving them
    behind would only move the import backwards.

    Two things stay put. **Protocols and exception types**: `TranscriptProvider`,
    `SearchProvider`, `PermanentError` and `TransientError` remain in
    `providers/base.py`, because a Protocol is the shape of an implementation rather
    than a structure in flight. **Configuration**: `Config` and its sub-configs stay in
    `config.py` — configuration is what a run is set up with, not data moving through
    it, and every module reading it is reading the same single object by design.


11. **`videos.status` is asset state. Relevance is not a status.** `status` says what
    we *have* — `seen`, `pending`, `has_transcript`, `needs_asr`, `failed`, `duplicate`
    — and never what we decided. What was decided lives in `decisions`, against the
    topic it was decided for, because a video declined for prompt caching is still a
    perfectly good video about Redis and one column cannot hold both claims. There is
    no `rejected` state and there must not be one again.

    **`seen` and `pending` stay distinct.** `pending` means a fetch started and did not
    finish — the entire payoff of rule 6's write-before-fetch ordering. Collapsing it
    into `seen` would turn every candidate we merely looked at into a phantom crash and
    delete the one signal that says a fetch died here.

    **A video is in the library if it has a note**, and `in_library()` is the only place
    that decides. Notes are what `/ask` loads, so note existence *is* membership;
    `ask.py` already selected this way, which is what makes it the honest rule rather
    than a new one. Holding a transcript is a different question and has a different
    caller: `flow/discover.py` checks the disk before the network so an unnoted
    transcript is never re-fetched, and `write_transcript` refuses to overwrite either
    way.

12. **A transcript is kept even when the video is declined.** A stage-2 NO stores
    `transcript.txt` and `meta.json` exactly as a keeper does; what keeps the video out
    of the library is the absence of a **note**, not the absence of bytes. Acquisition
    is the one fragile step, measurably so — thirteen of thirteen fetches died in nine
    seconds when YouTube throttled a run — and discarding content already in hand means
    paying that risk again for a video rejected for one topic and wanted for the next.

    The consequence is that `has_transcript` **no longer implies anyone chose the
    video**, so note selection cannot key on it. `wanted_for_a_note` decides, and the
    discriminator is a stage-`opening` NO — nothing else writes one. Three things
    override it: an existing note (regeneration must reach every note that exists), an
    acceptance for any topic, and no judgement at all (`marg ingest <url>` logs no
    decision, and a rule keyed on verdicts must not exclude the one path that produces
    none). **Naming a video explicitly always overrides**, for the reason `skip_reason`
    has always followed: a judgement made for one brief says nothing about a video the
    user has now handed over directly.

13. **Stage 2 reads the whole transcript, and its output format is enforced in code.**
    An opening cannot tell a video that delivers from one that opens well and never
    arrives — for a thousand words they are the same video, and padding is the thing
    stage 2 exists to catch.

    The five-line reply is a **guarantee, not a request**. A NO may not rest on evidence
    the judge says it could not read; a DEGRADED must name the damage; a clear keep must
    say where the value is; and there must be five lines and nothing else. A violation
    is a parse failure — counted, logged, surfaced — and **every resolution keeps the
    video**, because a parser bug that resolves to NO deletes videos nobody will ever
    know were missing. Prose alone does not hold: a real model returned *"the transcript
    is garbled and contains no substantive content"* with verdict NO and evidence CLEAR,
    and only the code caught it.

    **`EVIDENCE: DEGRADED` suppresses the note**, and that is its point. A note written
    from unreadable text is worse than no note, because `/ask` loads it and cannot tell.
    It overrides even `noted_at` — regenerating a note from a transcript now known to be
    garbled spends a model call to make the same bad note again.

14. **Every outcome gets its own counter.** A caption skip is not a fetch failure, a
    disk hit is not a Gate 1 hit, and degraded evidence is neither. Three meanings in
    two buckets is how a round where nothing went wrong reports failures — and the
    planner that reads these numbers cannot plan on a sum of causes. The rule already
    had a precedent: thirteen transient fetch failures once left no trace in a report
    at all.

15. **A territory term is never, on its own, a reason to keep a video.** The map says
    what words a subject uses; the **brief** says what counts as relevant. A LangChain
    document-loading tutorial is not about agent memory because LangChain is in the
    vocabulary. This is a rule in both judging prompts, with that worked example, not a
    note — B3 measured this judge confabulating from suggestive context, and a
    vocabulary list is a page of suggestive context. The judge is shown the concepts and
    the vocabulary flat, and never the queries, the filters or the map's reasoning:
    handing it those would be handing it the conclusion it is supposed to check.

16. **The model proposes a filter; the code decides whether it runs.** Two of the four
    search parameters *exclude* — `publishedAfter` and `videoDuration` — and an
    unjustified exclusion removes videos before anything in this system has seen them,
    which is the one failure nothing downstream can detect. `order` sorts and
    `relevanceLanguage` ranks; neither can remove anything, so both are safe.

    **`videoDuration` never goes on a main query.** Only on a variant that repeats a
    query it names, so the band *adds* a slice instead of removing one. A rule that
    drops Shorts is the duration bias `judge_metadata` spends four paragraphs
    forbidding, relocated to the search layer where nobody would look for it.

    **Every rejection widens, and every rejection is counted.** A refused filter drops
    the filter, never the query, and lands in `RoundReport.filters_rejected` and the
    report — the same discipline as the unparsed counters. `publishedAfter` additionally
    requires the recency call to have reasoned that age matters, and the user's own
    words outrank that reasoning: "history of transformers" with a twelve-month cutoff
    excludes precisely what was asked for.

17. **Recency is reasoned per subject, never ruled.** "Prompt caching pricing" wants six
    months and "SQLite WAL mode" wants any time, and no rule produces both. The reason
    is stored on the map and printed in the report, because a call that silently
    excludes and cannot be inspected is one nobody can correct. `ANY` is the default and
    the wider answer.

18. **A rejection reason is evidence about the judge, never a fact about the world.**
    The planner reads what the round decided, and the thing it must not do is redesign
    the search around a rejection pattern. When this judge had a duration bias, twelve of
    twenty-five rejections cited runtime; a planner "learning" from that would have
    removed short-form video from every later search permanently, and the redirect would
    have looked like the system working. **Consistency is what a systematic error looks
    like from the outside.** The brief is the authority, and the planner's question is
    *what does the brief ask for that I have not found* — never *why do I keep rejecting
    things*. Stated as a rule in `prompts/plan/round.md`, with that failure attached.

    Two structural halves back the prompt up, because a rule living only in a prompt is
    a rule enforced by a model. **The planner sees what was kept as well as what was
    rejected** — a planner given only failures only ever learns to avoid. And **a planner
    query may never carry `videoDuration`**: it is a main query by construction, so rule
    16's validator refuses the band outright. The worst version of the failure cannot
    happen whatever the prompt achieves.

19. **Additions widen and are free; removals narrow and need evidence.** The territory
    map may be revised between rounds — a wrong map inherited by every round is worse
    than no map. Adding a concept or a term costs one query whose results are visible.
    Removing one means a class of video that is never searched for again, which is
    rule 8's invisible error at the search layer, so a removal must name a query that
    actually ran and what came back, and at most three are accepted per round. Checked
    in code, not asked for in the prompt.

    **Every revision is versioned.** A revision creates map v2 with the diff, the reason
    and the previous version's term lists all stored on the run. Same argument as storing
    the map at all: a change you cannot inspect is one you cannot correct.

20. **`coverage_satisfied` is never confused with running out of money.** The planner may
    recommend stopping and is not the authority on it — every budget ceiling still
    applies, and a planner declaring satisfaction in round one is a real failure mode.
    What it gets is its own word in `stopped_because`, alongside `quota`, which fires
    *before* a round rather than inside one: a round that dies halfway leaves the planner
    reading half a round's evidence as though it were the whole thing.

## Structure

```
src/marginalia/  cli.py  config.py  contracts.py  llm.py  ask.py
  core/     ids.py  ledger.py  library.py        identity, state, file layout
  providers/                                     the part guaranteed to break
  brain/    judge.py  territory.py  planner.py   reasoning; no I/O
  flow/     search.py  discover.py               execution
  enrich/   note.py  timestamps.py               what happens after storage
```

- `cli.py` is the only entry point. Skills are thin: they call `marg` and report the
  line it returns. Logic never lives in a `SKILL.md` — the ingest skill routes on
  whether the argument parses as a video ID, and does nothing else.
- `core/library.py` builds every library path. No other module constructs one; `cli.py`
  reads `config.library_path` solely to hand it over.
- `llm.py` makes calls and knows nothing about verdicts. `brain/judge.py` turns replies
  into verdicts and knows nothing about the loop. `flow/discover.py` runs the loop and
  parses nothing. `brain/territory.py` splits the same way and adds a third part: pure
  parsing, then a pure **validator** deciding which proposed filters may execute, then
  the one call. The validator is the safety-critical half and it is a pure function of
  its arguments — including `today`, which is passed in so a date rule can be tested
  without waiting for a month to pass. `brain/planner.py` is the same three parts again,
  and it shares `parse_filters` and `tidy` with `territory.py` rather than restating
  them: two parsers for one filter syntax is how the two drift, and the validator's
  guarantees would then hold in only one of them. Keep that split; it is why the parsers
  are testable without a model.
  `enrich/note.py` holds the same split internally: pure section parsing at the top, the
  drain loop at the bottom, and nothing in between that needs a model to test.
- `print` appears only in `cli.py`. Everything else uses `logging`.
- Reading a transcript must be followed by `Ledger.touch_accessed(video_id)`. The
  deletion policy that arrives later depends on it, and it cannot be reconstructed
  retroactively.

## Scope

Milestone 5 adds **understanding notes**: `enrich/note.py`, `prompts/write_note.md`, the
`marg note` command, and `note_version`/`note_model`/`noted_at` on `videos`. A stored
transcript goes in, a note comes out, and the note is what every later question is
answered from.

**B4 adds territory mapping**: `brain/territory.py`, `prompts/territory/map.md`,
validated per-query search filters, `runs.territory_map`, and the `queries` table's
first writer. The map is built **once**, before round one, and never revised — revising
it between rounds is B5's planner, and a map re-run each round would be that arrived at
by accident. The map's queries run *alongside* the brief's own expansion rather than
replacing it, which is what lets `queries.origin` answer "did the map find anything the
old expansion would not have?" from the ledger rather than from an opinion.

**B5 adds the round planner**: `brain/planner.py`, `prompts/plan/round.md`, one call at
the end of every round but the last, and `ORIGIN_PLANNER` as the `queries` table's third
writer. Before it, the agent planned all of its searching before seeing a single result.
It reads the round — queries and their result counts, videos kept and rejected with the
judge's reasons, unexplained concepts, the counters, quota left — and returns a coverage
assessment, the next round's queries, optional map revisions and a recommendation. Its
queries run *first* in the next round and the brief's expansion still runs alongside
them, for B4's reason: a new step that silently replaced a working one would make its own
contribution unmeasurable.

This completes the discovery-layer upgrade. What comes next should be decided by running
it on real topics, not planned in advance.

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
marg ask "<question>"               answer from every note; --deep <id>, --json
marg llm-check                     one request to the configured provider
marg status <url|id>               show ledger state for one video
marg list [--status S]             list videos by asset state (default has_transcript)
marg guard-write                   internal: the PreToolUse hook, reads stdin
```

Config resolution: `MARGINALIA_CONFIG` → `./config.toml` → `~/.marginalia/config.toml`.
