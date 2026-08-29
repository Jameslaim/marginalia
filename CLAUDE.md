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
   **The rule does not stop at stdout.** `answers/`, `exports/` and `logs/` are three
   more surfaces and are held to it identically — a log file is the worst place to
   leak, because it accumulates every run and nobody opens it until something is
   already wrong. This is why the log handler is attached to the `marginalia` logger
   rather than the root: at DEBUG the root collects the HTTP client's records, and
   those carry request bodies, which here means every transcript and note the model
   was ever shown. An answer file names the notes it was written from and never
   quotes them.

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

    **`seen` is legacy. Nothing produces it.** B2 noticed `mark_seen` had no production
    caller; the cleanup phase traced it and the answer is that no `videos` row is
    created at all when stage 1 says NO. `mark_pending` holds the only `INSERT INTO
    videos` in the system, and it runs immediately before a fetch — so a candidate that
    was judged and declined without being fetched exists in `decisions` and nowhere
    else. Measured on the real library: 235 stage-1 NOs have no `videos` row, and all
    15 rows carrying `seen` were written by B1's migration of the old `rejected` status
    on a single day. Treat `seen` as a state the schema can hold and the code does not
    write. Do not build on it, and do not assume a declined candidate has a row.

    **If `seen` ever gains a writer, it must stay distinct from `pending`.** `pending`
    means a fetch started and did not finish — the entire payoff of rule 6's
    write-before-fetch ordering. Collapsing the two would turn every candidate we merely
    looked at into a phantom crash and delete the one signal that says a fetch died here.

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

    **Nothing enters the library unread. Every candidate that will be stored reaches
    stage 2, whatever stage 1 said.** Stage 1 decides whether to *fetch*; stage 2 is the
    verdict that decides whether to keep. A stage-1 YES used to skip it as "the cheaper
    keeper", and the saving is real and tiny — B3 measured stage 2 at 0.09¢ and 2.5
    seconds per video — while what it bought was a video stored because of its title.
    That is not a tidiness complaint: B3 measured *this* judge confabulating from
    metadata, three videos kept on reasons describing numbers their transcripts do not
    contain, and every one of those was caught only because it was a MAYBE. A confident
    YES never reached the check. `EGNykTqFDBs` in run `19941ec23cb7` is the case in
    full — accepted on *"shows connection sharing and queueing behavior with real
    setup"*, never read, and the planner then reported as a gap a subject that video
    covers in detail. The planner was not wrong; it had one sentence of evidence and
    reported honestly on it.

    Two counters, never one. A MAYBE stage 2 declines is `flipped_to_no` and is stage 2
    working; a **YES** stage 2 declines is `overturned` and is stage 1 having been
    wrong. Summing them hides the only number that says how far a metadata verdict can
    be trusted alone — rule 14, applied to the thing rule 14 was written for.

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

    **The `[outside knowledge: ...]` marker is checked here too, and it is the one rule
    in this file that changes no verdict.** Era is the single axis a transcript cannot
    speak on — no video states that its own import path has been superseded — so PLAN.md
    §7 allows that exception and requires it to be marked, in order to be *counted*.
    Asked for in the prompt, it was measured at 1 of 4 currency claims marked on a real
    run, so the parser detects it: a reason calling something deprecated, superseded,
    obsolete or out of date without the marker sets `unmarked_currency`, which is logged
    and lands on its own counter.

    **Counted, not repaired, and not a parse failure.** Repairing it by appending the
    marker would put words in the judge's mouth and leave the count measuring the
    detector rather than the judge. Folding it into `unparsed` would report a convention
    nobody followed as a format nobody could read — rule 14 again. And the verdict is
    left alone because the judgement is usually right; what is missing is the
    attribution, which is a claim about where a reason came from, not about the video.

    **Attribution suppresses it.** "The speaker calls the old chain API legacy" reports
    the transcript and needs no marker; "this uses a legacy API" is the judge's own
    dating and does. The proxy is tuned to miss rather than to over-fire, because a
    counter that fires on every honest rejection measures nothing — the same trap as
    `"transcript"` matching every rejection that named its evidence.

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

    **`publishedAfter` is the exception: the code derives it, and the model does not
    propose it at all.** The judgement and the arithmetic sat in one field and only the
    judgement got made. Measured twice on `langchain`: a map reasoned *"last 18 months —
    the API has churned (LLMChain→LCEL→LangGraph)"* and then emitted queries carrying
    `order=date` and no cutoff — **0 of 29 queries filtered by date, with
    `rejected_filters` empty**. Nothing refused it; nothing proposed it. The run before
    managed 3 of 33, and that was read as the quota bug hiding the filter, which was
    substantially wrong: the quota bug stopped three from running and the map never
    wrote the other thirty.

    So `RECENCY` stays the model's — *does age matter here, and why* is a real judgement
    about an ecosystem — and *today minus eighteen months* is a calculation, done in
    `_derive_published_after`. A calculation asked of a model is one that is sometimes
    simply not done, and its absence looks exactly like a decision not to filter.

    **A query may waive it, and a waiver is its own outcome.** `publishedAfter=any
    (reason)` keeps the window off one query — the gap that is specifically about earlier
    practice, where the window is what has been hiding the answer. It is counted in
    `recency_waived` and never summed into `filters_rejected`: a refused filter means a
    search ran wider than the *model* asked, a waiver means one ran wider than the *code*
    decided, and a skipped duplicate means no search ran at all. Rule 14, three ways.

    Derivation fills an absence and never overrules a judgement that passed the rules; it
    does not touch a variant (a `videoDuration` slice exists to *add* width, and dating
    it takes that back); and it obeys `user_constraints` exactly as a proposed filter
    does — otherwise moving the arithmetic into code would have moved it out from under
    the one constraint that outranks the reasoning.

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

17. **Recency is reasoned per subject, never ruled — and then it is applied.** "Prompt
    caching pricing" wants six months and "SQLite WAL mode" wants any time, and no rule
    produces both. The reason is stored on the map and printed in the report, because a
    call that silently excludes and cannot be inspected is one nobody can correct. `ANY`
    is the default and the wider answer, and nothing is derived from it.

    A reasoned window that reaches no query is the same as no reasoning at all, and for
    two runs that is exactly what it was. Rule 16's derivation is what makes this rule
    true rather than merely stated, and a derived cutoff carries `RECENCY_DERIVED_WHY`
    in `filters_json` so the ledger still says whether a date was a judgement or
    arithmetic.

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

21. **`answers/` is a record; `exports/` is a view.** An answer is written once and
    never overwritten — a collision gets a counter — because the earlier answer to the
    same question is what you compare against when a prompt change was supposed to have
    improved something, and regenerating one costs a model call over the whole library.
    An export is the opposite: derived entirely from the ledger, overwritten freely, and
    safe to delete. Nothing is exported automatically and nothing is written back. The
    moment an export holds something the ledger does not, there are two sources of truth
    and no way to tell which is stale.

    **A run export reconstructs rounds; it does not read them.** `queries` carries a
    round number and `decisions` does not, so a decision is attributed to the last round
    whose queries had already started. That is sound — the loop is sequential — and it
    is an inference, so `report.md` states the method in the file rather than presenting
    it as recorded fact.

22. **Gate 1 asks two questions, and `in_library` is only one of them.** *Is this in the
    library?* is about notes and nothing else — rule 11 — and the answer for a video
    stored ninety seconds ago is **no**, because the note worker has not run and may
    never. So every round used to re-judge everything the previous rounds kept:
    `EGNykTqFDBs` carries two `metadata`/`yes` rows in run `19941ec23cb7`, one per round.
    The disk check meant no second download, so what this cost was model calls and
    inflated counters — and the planner reads those counters, so a video counted three
    times is two findings that do not exist.

    The second question is *did I store this in this run?*, and it is passed to the gate
    separately as `stored_this_run`. **`in_library` is not weakened to answer it**, and
    the tempting weakening — accept a transcript — would be wrong twice: a stage-2 NO
    also holds a transcript and must stay re-judgeable for a later topic, and `/ask`
    cannot answer from a video with no note, so calling it a member is a claim the
    library contradicts. Same distinction B1 drew between *is it in the library* and *do
    we already hold the bytes*, for the third time.

    **The run-scoped skip logs `maybe`, never `no`.** Gate 1's library branch logs a
    `no` and that is right there. Logging one here would tell Gate 2, on every later run
    on this topic, that a video this run *kept* had been turned down — rule 8's
    invisible error arriving through a counter fix. Only *kept* videos need this: a
    stage-2 NO writes a `no` for the topic, so Gate 2 already stops it next round.

23. **A reasoning step stores its reasoning, not only its output.** The `queries` table
    recorded what each plan produced and the one-line gap label behind it; the coverage
    assessment, the recommendation and its reason were thrown away when the process
    exited. So *"why did the planner call that a gap?"* was unanswerable from a stored
    run — which is the question storing runs exists to answer, and the same argument
    that already put the brief and the territory map on the ledger: the queries are the
    conclusion, and the conclusion is the half you cannot correct from.

    The plan is stored whole, one row per round in `plans`, and rendered under its round
    in `report.md`. Written *before* its revisions are applied, so a crash between the
    two leaves the reasoning on disk rather than only its consequences.

24. **A provider's refusal is authoritative; only a failure is retried.** A 4xx means
    the request was refused, and an identical request is refused identically — so
    `_classify` retries exactly three of them (408, 409, 429: a timeout, a conflict, a
    rate limit, each of which clears on its own) and stops on every other. The default
    used to be the reverse, and 402 fell through it: an account with no balance was
    asked four times per video for three videos before the streak counter noticed.
    Twelve calls that could only fail, on a condition that does not clear in seconds.

    This is the claim the quota work already settled one layer down. Google's 429 is
    authoritative about quota whatever the local counter reads; a provider's 402 is
    authoritative about billing whatever the run would like to do next.

    **Stopping the run and condemning the video are different claims, and only the
    first is warranted.** A billing error is not evidence about a transcript: nobody
    read it, it is unchanged on disk, and it must still be in the queue tomorrow. So
    the drain records *no* `NoteResult` for the video it was on, stops with
    `STOP_REFUSED` — its own reason, not `repeated_failures`, which is what it looked
    like the first time — and still reports the notes already written, because there
    were 74 of them behind the first 402.

    **And a refusal may not resolve toward keeping.** `judge_metadata`, `judge_full`
    and `judge_opening` each catch a failed call and keep the video, which is rule 8
    applied correctly to a blip. A refusal breaks that arithmetic: every later call
    fails identically, so keeping would walk the whole candidate set through as MAYBE,
    fetch it, and store it on judgements that never happened. All three re-raise
    `LlmConfigError` ahead of the catch-all. Keeping on a blip is caution; keeping on a
    refusal is storing an unjudged library.

## Structure

```
src/marginalia/  cli.py  config.py  contracts.py  llm.py  ask.py
  core/     ids.py  ledger.py  library.py        identity, state, file layout
  providers/                                     the part guaranteed to break
  brain/    judge.py  territory.py  planner.py   reasoning; no I/O
  flow/     search.py  discover.py  export.py   execution
  enrich/   note.py  timestamps.py               what happens after storage
```

- `cli.py` is the only entry point. Skills are thin: they call `marg` and report the
  line it returns. Logic never lives in a `SKILL.md` — the ingest skill routes on
  whether the argument parses as a video ID, and does nothing else.
- `core/library.py` builds every library path. No other module constructs one; `cli.py`
  reads `config.library_path` solely to hand it over. That now includes `answers/`,
  `exports/<run_id>/` and `logs/`, none of which is created until something writes into
  it — a library that has never been asked a question has no `answers/`.
- `flow/export.py` renders one run as a folder. It reads the ledger and writes files and
  reasons about neither, which is why it is in `flow/`; the rendering functions above the
  one entry point are pure and take rows rather than a `Ledger`.
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

**B6 closes three holes the first real export found**: every keeper reaches stage 2, so
nothing enters the library unread; Gate 1 is given the run's own `stored_ids`, so no video
is judged twice in one run; and the planner's whole assessment is stored in a new `plans`
table and rendered in `report.md`, so *why* it called something a gap survives the process
that decided it. Measured on `postgres connection pooling`, twice, on empty libraries:
11 videos held-but-never-read became 0, and the planner went from marking all 8 brief
items uncovered to naming videos against 10 of 12. See `PLAN.md` §B6 for the numbers,
including the two confounds.

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
marg ask "<question>"              answer from every note; --deep <id>, --json,
                                   --no-save (saving to answers/ is the default)
marg run export <run_id>           write exports/<run_id>/ from the ledger
marg llm-check                     one request to the configured provider
marg status <url|id>               show ledger state for one video
marg list [--status S]             list videos by asset state (default has_transcript)
marg guard-write                   internal: the PreToolUse hook, reads stdin
```

Config resolution: `MARGINALIA_CONFIG` → `./config.toml` → `~/.marginalia/config.toml`.
