# marginalia — rules

These are the rules that define how marginalia works. marginalia is a Python CLI
that stores YouTube transcripts on disk and answers questions about them; `PLAN.md`
holds the full design, and these rules are the part of it that must not drift.

The rules belong to the agent. They do not depend on any particular tool, editor or
chat harness, and every one of them was written from a failure that was measured on
this system — the numbers, video ids and run ids beside each rule are the evidence it
was built from. **Read them before changing anything in this repository.** Editing
without them re-breaks bugs that have already been found, fixed and paid for.

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
   **A note's line is note text, including when it is short.** `snap-timestamps`
   printed seventy characters of the note's line beside every moved citation, on
   the reasoning that a note's words about a video are not the video's words.
   On a `## Verbatim` line they are exactly the video's words, and a
   library-wide run printed hundreds of them. It now prints the citations and
   their sections and nothing else.

2. **Never write into the library directory directly — always go through the CLI.**
   No `Write`, no `Edit`, no shell redirection into the library path. The ledger and
   the files must stay in agreement.

   **No program can enforce this from inside itself.** A process cannot stop another
   process from writing to a folder; only the operating system can, and it does not.
   The `guard-write` hook covers one editor's tool calls — `Write`, `Edit` and their
   kin — and **not shell writes**, so a script writing into the library walks straight
   through it. It is a convenience, not a boundary.

   **This project broke the rule twice during cleanups**, deliberately, with the
   reasoning recorded each time. That is the honest account of what backs it: the rule
   holds because the agent follows it, not because anything makes it impossible to
   break.

   **Prevention is impossible; detection is not.** `marg doctor` compares the library on
   disk against the ledger and reports every disagreement — a directory with no row, a
   row whose status claims a transcript that is not there, a note the ledger never
   recorded, a transcript whose hash has changed, a transcript on disk its row does not
   record. It fixes nothing. The last of those has its own repair command, `marg
   reconcile`, because an interrupted store leaves it and nothing else records it.
   Finding a violation after the fact is the strongest guarantee available, and it is
   why the rule can stay an absolute rather than a hope.

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

   **The corollary: `pending` must mean a fetch died, and nothing else.** A
   budget ceiling is checked *before* a fetch, never between a fetch and what
   follows it, and a fetched transcript is stored before anything else is asked
   of it. Discovery used to check its request and cost ceilings after the fetch
   and store only after stage 2 returned, so a ceiling reached there — or a 402
   from the judge — discarded a transcript already paid for and left a
   `pending` row claiming a crash that never happened.

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

   **`_send` may name only `model`, `messages`, `max_tokens`, `user` and `extra`.**
   Everything else a provider wants comes from `[llm] extra` or `[llm.tasks.<name>]
   extra`, merged in and passed through without `llm.py` knowing what any of it means.
   This is not theoretical: a hardcoded `temperature=0` was the single thing that broke
   the first real provider swap, because `kimi-k3` refuses any temperature but its own
   default. A test reads `_send`'s source to keep it honest.

   **`user` was added to that list on 2026-09-09, and the test that it is not a
   provider-specific parameter is that it is in the OpenAI Chat Completions baseline** —
   every compatible endpoint accepts it, so naming it binds this project to nobody. That
   is the whole standard for widening this list, and nothing else has met it.

   It could not come from `extra`, and that is the argument for the widening rather than
   a convenience: **config is static and this value must not be.** xKiro replays
   byte-identical blocking requests within a short window — their words, *"automatic
   replay of byte-identical blocking requests"* — and publishes no opt-out header.
   Measured: five identical calls returned **one** distinct answer, four of them in under
   0.4 seconds. The window is under 60 seconds, so the damage is bounded to one thing,
   and that one thing is the thing that matters: **an in-run retry gets the failure
   replayed instead of a fresh sample.** Three notes recovered on retry in the week
   before the swap and would not have here. A unique `user` per call makes every request
   distinct at the byte level; it was measured as the cleanest buster (`probe-1` and
   `probe-2` both returned fresh, different responses) and costs nothing on a provider
   that does not cache.

   **It is a cache-buster, not an identity**, and `_call_id` returns random hex for that
   reason. Sending a real user would put an identifier on every request for no benefit
   and could become a tracking key by accident. Recorded here because the next reader
   will otherwise see a pointless unique string on every request and delete it.

8. **Anything a judge cannot parse becomes MAYBE, never NO.** Keeping a useless video
   costs one download and is visible in the library. Rejecting a good one costs a
   video nobody will ever know existed — invisible and permanent. A parser bug, a
   refused batch, a network blip mid-judgement: all resolve toward keeping. The two
   mistakes are not symmetric and the code must not treat them as if they were.

   **At stage 2 the costs are different, and have been since rule 12**
   (recorded 2026-09-22). A stage-2 NO keeps the transcript, and the video still
   gets a note — so it is not lost, it stays in the library, and questions are
   still answered from it. What stage 2 decides is coverage for the topic: a NO
   means the video does not count toward the brief and later searches on the topic
   skip it. The error that hides at stage 2 is a wrong **YES**, because the planner
   counts it as covered and may stop, and nothing records the gap it left — the
   `EGNykTqFDBs` shape under rule 13. The stage-2 prompt now states both costs and
   no longer calls a NO invisible and permanent. Stage 1 is unchanged: a metadata
   NO still means never fetched, and everything above still holds there. The
   parse-failure → YES handling in `parse_full` was **not** revisited.

   **A stage-2 call that produced no verdict is MAYBE, and is not coverage**
   (recorded 2026-10). A failed call, a transcript over the model's window, a reply
   cut off by the ceiling, a reply with no verdict word: each used to resolve to
   YES, which is the keep rule 8 asks for and also exactly the wrong YES the
   paragraph above names — the planner listed it under KEPT and counted it, and
   discovery read its `decisions` row as "already judged for this topic", so one
   provider blip stopped the topic from ever reading the video again. Now the
   verdict is MAYBE with `judged=False`: still kept and stored, still noted
   (note-writing never consulted the verdict), shown to the planner under its own
   KEPT BUT NOT JUDGED heading, and judged again by the next run on the topic. The
   one exception is a transcript too large for the model that refused it, which
   waits for a different model rather than spending a refused call every run. A
   reply with a verdict and a stray line beside it is still a verdict.

   **Whether a judge decided anything is stored, never inferred** (2026-10-11).
   `decisions.outcome` is `judged`, `call_failed`, `too_large`, `truncated` or
   `no_verdict`, and `decisions.said` keeps the judge's own verdict word when the
   stored one resolves it. It used to be reconstructed from `verdict == maybe` and
   from exact reason strings, and three historical shapes were never recognised,
   so those videos read as "already judged for this topic" for good. Rows written
   before the column are read by `ledger.legacy_outcome`, the one place that
   still interprets reason text. Two resolutions moved with it, both toward *kept,
   not coverage* rather than toward YES: a judge's literal `MAYBE` stays MAYBE (it
   was counted as YES — this paragraph's wrong YES), and a NO resting on a
   transcript the judge called unreadable is MAYBE + DEGRADED with `said = no` (it
   was stored as `yes`). Coverage requires an actual YES.

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
    that decides. **Note existence no longer implies anyone judged the video relevant to
    anything** — a note says only that a readable transcript was on disk. Relevance is a
    property of `decisions` alone, read through `current_verdicts`, and the two must not
    be inferred from each other in either direction. Notes are what `/ask` loads, so note existence *is* membership;
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

    **Note-writing consults the transcript, never the verdict.** `wanted_for_a_note`
    refuses on exactly two grounds, and both are claims about the *text*: `EVIDENCE:
    DEGRADED`, and a transcript below `[note] min_transcript_chars`. A stage-2 NO no
    longer suppresses a note.

    That reverses an earlier reading of this rule, and the measurement is why. Of the
    69 videos a stage 2 declined and note-writing kept anyway, **37 are cited in stored
    answers and one in nine of them**. Two different questions had been sharing one
    function: *stage 2* asks whether a video satisfies the brief for topic T, `/ask`
    asks whether a note helps answer question Q. `x6akeHQ5f-M` was declined for *ai
    agent memory* because it missed that brief, then cited by "what do these videos get
    wrong?", where a mediocre video is excellent evidence. Neither judgement was wrong.

    Rule 13's concern never applied here. It exists to stop *"a video stored because of
    its title"* — but this rule keeps the transcript either way, so the title only ever
    decided whether to **fetch**. A note is written from the transcript, so a note for a
    declined video is a faithful account of real content.

    **And the cost that justified the old rule has since changed.** When it was written,
    an extra note made answers worse: a library over budget filled the context by sorted
    video id, so one more note displaced a better one at random. The selector now ranks
    and fills to a budget. A rule written under a cost that no longer holds is worth
    re-deriving rather than inheriting.

    **For one video and one topic, a later stage supersedes an earlier one.** Stage 2
    read the transcript; stage 1 read the title. When they disagree the one that read
    wins — not a new rule, but rule 13 expressed where `wanted_for_a_note` can act on
    it. The acceptance clause above used to count *any* stage-1 YES, including one the
    same run's stage 2 overturned seconds later on the same topic: the pipeline judged
    the title, judged the transcript, correctly recorded that the title had been wrong,
    and then kept the video on the title's verdict. Rule 13 already names that case —
    `overturned` — and B6 made every keeper reach stage 2 precisely so nothing would be
    stored because of its title.

    Measured: **69 of 422 notes were in that shape**, and 68 stop being wanted. The
    sixty-ninth is the clause working — `3KWRWdOqGg4` carries a stage-2 NO for one topic
    and a stage-1 YES for another that never reached stage 2, a genuine cross-topic
    acceptance that still overrides.

    **The 69 keep their notes.** `noted_at` overrides everything but degraded evidence,
    so this changes what gets noted next rather than what is on disk — and removing them
    would not be free: **37 of the 69 have been cited in stored answers**, one of them in
    nine. Stage 2 declining a video that then carries answers is a finding about stage 2,
    not a cleanup task.

    **A note is refused below `[note] min_transcript_chars`** — its own outcome and its
    own counter, not a failure, not degraded evidence, not the length ceiling. Measured
    rather than chosen: a note is normally about half its transcript (library median
    0.469x, p90 1.42x), and below ~1,200 characters that inverts to 5x–126x, with a
    37-character transcript producing a 4,681-character note. Kept low on purpose — a
    floor at 2,000 would start excluding genuine short-form video, which is the duration
    bias `judge_metadata` forbids, relocated to where nobody would look for it.

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

    **A candidate already on disk reaches it too.** A transcript held from another
    topic, or stored just before a judge refused, used to be skipped as "already on
    disk" — no fetch, which was right, and no stage 2 for this topic, which was not:
    it never counted as coverage, and the planner could name as a gap a video the
    library was holding. Stage 2 now reads it from the stored file, counted in
    `judged_from_disk`, unless it has already been judged for this topic.

    **And it goes straight there, without stage 1.** Stage 1 decides whether to
    fetch, and held bytes need no fetch. A held candidate was screened by stage 1
    first, so a title-only NO stopped stage 2 from reading a transcript the library
    held — in that run, and permanently through Gate 2. Gate 2 likewise does not
    let a *stage-1* NO block a video whose transcript is held; stage 2's reading
    then supersedes the title's (rule 12's "later stage supersedes"). **Held is
    what the disk says**, asked by both callers — discovery and `marg search` —
    and never read off the row: a `has_transcript` row over a missing file is not
    held, and a caller with no library to ask claims no exemption.

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

    **The damage-word proxy over-fired a second time, on subject matter.** Bare
    `quality`, `noise`, `empty`, `truncated` and `corrupt` matched what a video was
    *about* — "a code quality tutorial", "data corruption recovery", `TRUNCATE` — so a
    content rejection became a DEGRADED keeper, and DEGRADED denies a note for every
    topic: the one path that took a video out of the library. A damage word now counts
    only when the reason says it of the transcript, captions, subtitles or audio.

    **`EVIDENCE: DEGRADED` suppresses the note**, and that is its point. A note written
    from unreadable text is worse than no note, because `/ask` loads it and cannot tell.
    It overrides even `noted_at` — regenerating a note from a transcript now known to be
    garbled spends a model call to make the same bad note again.

    **So DEGRADED must be earned by the reason, not only written on the label.** A
    DEGRADED whose reason names no damage was counted as a violation and then honoured,
    which let a format slip take a video out of the library for every topic. It now
    resolves to CLEAR — the parser's own diagnosis is that it was "I am not sure"
    wearing the label — and a NO resting on it is kept, as every resolution here is.

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

    **And the other half: a counter's name is a claim about what it measures, and the
    claim needs checking.** Four times in three days a counter measured something
    narrower than its name — `unwanted` meant "declined for relevance" and then
    "unreadable" under an unchanged name; `unresolved_ids` could not see a
    twelve-character id; `unverified_quotes` conflated a scare-quote with attributed
    speech; and `check_grounding` matched the whole note while meaning the speech
    section, so 101 of 204 "verified" quotes turned out to be the note-writer's words.
    Every one was found by accident — a hyphen, a hand-split, a failing test draft — and
    not one by someone asking whether the counter measured what it claimed. Splitting an
    outcome into its own counter is worth nothing if the counter beside it is quietly
    measuring something else.

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
    The parse of that judgement is code too, and was wrong: it read the first number and
    ignored its unit, so "2 years" derived a 2-month cutoff. Years now count as twelve
    months and a range takes its wider end.

    **The window is sampled three times and the widest is taken.** Making the call
    load-bearing made its variance load-bearing, and the variance was then measured —
    ten samples per topic, brief and prompt held fixed:

    | topic | distribution | distinct |
    |---|---|---|
    | `ai agent architecture` | ANY ×4, 24m ×4, 18m ×2 | 3 |
    | `langchain` | 24m ×4, 18m ×3, 12m ×3 | 3 |

    The real `ai agent architecture` run answered 36 months, wider than any of the ten.
    Against the 31 videos it kept, 24 months would have excluded 2, 18 months 4, and
    12 months 7 — and one of the four is *Building Effective Agents with LangGraph*,
    the video the planner named as its evidence that a MUST CONTAIN item was covered.
    **A different sample does not only shrink the library; it invalidates a coverage
    claim the run reports as fact.**

    The reasons are individually correct, and that is the point: *"the modern meaning
    of 'AI agent' only settled from late 2023"* and *"ReAct and tool-use loops are
    stable patterns taught the same way since 2022"* are both true. This is a
    well-posed question with several defensible answers, not a prompt to fix.

    **What makes the widest safe is the direction/magnitude asymmetry in the same
    measurement.** The call got the direction right 10 of 10 on `langchain` — always a
    window, never `ANY` — and varied only on how far back. So widening neutralises the
    unreliable half of the judgement and keeps the reliable half, and it does *not*
    collapse to `ANY`: on `langchain` widest-of-three still returns 24 months. Odds of
    landing on the narrowest observed window fall from 0.2 to 0.008 and from 0.3 to
    0.027, for two extra calls at about a third of a cent.

    Stated plainly, because it is the whole argument: **an unstable exclusion is
    ambiguity, and rule 8 says ambiguity resolves toward keeping.** This is that rule
    reaching the search layer.

    Three things it does not change. `user_constraints` still outrank it — widening is
    a floor on the *model's* judgement, never on the user's, or it would become a route
    around a constraint the user set. The per-query waiver is untouched and counted
    separately in `recency_waived`; the two cover different runs, since the waiver fires
    when the map notices the conflict itself (10 of 20 measured samples) and widening
    covers the runs where it does not. And a `videoDuration` variant stays wide.

    **Every sample is stored**, not just the winner: `RECENCY_DERIVED_WHY` carries the
    window that ran and what the other two said, so *"was this a consensus or a coin
    flip?"* is answerable from a stored run. Rule 23 — the conclusion is the half you
    cannot correct from.

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

    **And it has now been right once, which this file has to say too.** The rule above
    was written entirely from the failure direction, and a rule that only ever warns
    would have someone override a planner that was correct. On `ai agent architecture`
    the planner stopped after round one at 10 of 10 brief items, and four questions
    asked of the resulting library came back on **52 distinct videos — 40 cited in
    exactly one answer, 11 in two, and one in three**. No handful was carrying the
    answers, which is what a premature stop looks like from the outside. Round-one
    satisfaction is a failure mode *and* sometimes a fact; the load spread across
    several real questions is what tells them apart, and it costs four answers rather
    than another round.
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

    **And a refusal may not resolve toward keeping.** `judge_metadata` and `judge_full`
    each catch a failed call and keep the video, which is rule 8 applied correctly to a
    blip. A refusal breaks that arithmetic: every later call fails identically, so
    keeping would walk the whole candidate set through as MAYBE, fetch it, and store it
    on judgements that never happened. Both re-raise `LlmConfigError` ahead of the
    catch-all (as did `judge_opening`, removed in 2026-10 with no caller left). Keeping on a blip is caution; keeping on a
    refusal is storing an unjudged library.

    **A 429 is the one refusal the provider dates, and that is read rather than
    guessed.** `Retry-After` is honoured — delay-seconds or an HTTP date, unparseable
    treated as absent rather than as zero, so a malformed hint falls back to the
    backoff instead of becoming a hot loop. Above `_MAX_RETRY_AFTER_S` it stops with
    `LlmRateLimited` instead of sleeping, on this rule's own judgement: a wait longer
    than the run is a refusal in a different costume.

    **One request over the context window is refused for that request, not for the
    run.** `LlmRequestTooLarge` — a 413, or a 400 whose code or message says the
    context was exceeded — is not retried, because an identical request is refused
    identically, and it is not an `LlmConfigError`, because the next request may fit.
    Classed as config, one long transcript ended a discovery run at stage 2, and in
    the note drain it stopped on the same video every time, blocking the queue for
    good. Stage 2 keeps the video unread; the drain records it SKIPPED.
    **Three in a row, with nothing fitting between them, is a config error again**:
    that is a `max_tokens` larger than the model's window, or a model too small for
    the library, and handled per request it would keep every candidate unread —
    an unjudged library by another route.

    **Where requests are independent the streak is per caller** (2026-10-11). The
    note drain and discovery's stage 2 reset the client's count per video, because
    three long videos in a row are ordinary and used to end a discovery run. In
    their place discovery stops after five stage-2 overflows in a row with nothing
    fitting between them, under its own reason `context_window` — the
    configuration, not the videos — and the client's own streak still guards the
    small calls (stage 1, expansion, planning). It stops **at** the fifth, even when
    the fifth was its round's last candidate; checked only before a next candidate,
    a run whose fifth overflow ended a round went on to search and screen again.

    **Only the request id is logged, never the error body.** A provider's body is the
    field its support asks about *and* an echo of the request, which here is a system
    prompt plus a whole transcript — so `_safe` puts the classification and the id in
    the log line and nothing else, and an id that cannot be found is logged as absent.
    Hard rule 1's worst case is reached by the most natural implementation of this
    feature, which is why a test asserts the transcript never reaches a log record.

    **And no error carries the body either** (2026-10). `_safe` formats the
    classified error, and the classifier used to build that error from the
    exception's own text for anything it did not recognise and for every 402, 401
    and other 4xx — so the body reached the retry line, the judge's warning, a
    note's failure detail, and the failed answer `marg ask` prints and saves. Errors
    are now built from the status, the provider's error code when it is an
    identifier, and the request id; anything else is named by its class
    (`safe_reason`).

    **The failure neither this rule nor rule 13 can see: a *semantic* failure.**
    A reply that terminates cleanly, parses, fills every required field and is
    simply wrong. `_classify` reads HTTP statuses and sees success; rule 13's
    five-line contract reads shape and sees a well-formed verdict. Both are
    structural checks, and this failure is structurally indistinguishable from a
    good answer.

    Not hypothetical: B3 measured three videos kept on judge reasons describing
    numbers their transcripts do not contain — clean replies, correct format,
    confabulated content. The library names the class too, in a video Q3 cited,
    which is where the word comes from.

    Recorded as a known class rather than a task. There is no current instance,
    detecting it in general is the hard open problem behind every eval suite,
    and the partial defences already in place are the ones worth having: stage 2
    reads the whole transcript rather than the opening, `[outside knowledge]`
    marks era claims, and citations resolve against the notes that were loaded.
    What none of them do is catch a fluent wrong answer, and no counter in this
    system will.

    **Degrading instead of stopping is a real alternative, and it does not apply
    here.** The library says it plainly — *"a memory outage degrades your agent
    instead of taking it down"* — and that is right for an **optional subsystem**
    whose absence still leaves the agent able to answer. This is not that: a billing
    refusal or an hour-long rate limit means every later call fails identically, so
    there is nothing left to degrade to. Recorded so it is not re-litigated from the
    same video.

25. **A guard added to one caller must be traced to every caller that shares the
    failure.** Fixing the caller where a failure was *observed* is half the work; the
    other half is asking what else calls the same thing in the same way, and it is
    the half that gets skipped. Twice now, twelve days apart, and both times the
    sibling was found by accident rather than by anyone asking.

    **The 402 went into the note drain.** Rule 24's refusal handling was written
    where the billing error surfaced, and `judge_metadata`, `judge_full` and
    `judge_opening` were only hardened when someone asked what else caught a failed
    call and kept the video. All three implement rule 8 correctly for a *blip* —
    keeping on an error is caution — and a refusal breaks that arithmetic, because
    every later call fails identically. Untraced, a 402 mid-run would have walked the
    whole candidate set through as MAYBE, fetched it, and stored a library on
    judgements that never happened.

    **The context guard went into `ask.py`.** M7 gave the answering path an
    estimator, an 80% warning and a hard refusal, on the argument that running out of
    context is a cliff rather than a slope. `_assess_library` loads *the same notes*
    into *the same ceiling* and got none of it. It failed silently on the first
    discovery run after the library crossed the line — 1.75× over, failing open, the
    planner re-searching ground already on disk.

    The two are the same shape: a correct fix, applied where the symptom appeared,
    stopping there. What makes this a rule rather than an observation is that both
    siblings were reachable by one question — *what else calls this, and does it
    share the failure?* — asked at the time, of a diff that was already open.

    **The 2026-10 review found five more, all by asking that question.** The
    truncation guard (`finish_reason == "length"`) was on the map, the plan, the
    baseline, the selector and stage 2, and not on the note, the answer or the brief —
    the three whose fragments are stored as if whole. The snapper learned that an
    ASCII word class cannot see an Arabic quote; the grounding check never did, and
    skipped every non-Latin quote. `ESCALATE` existed to fetch exact wording, and the
    check that judged quoted wording never read what it fetched. Rule 7's
    model-family guard scanned `prompts/*.md` and not the three subdirectories, and
    three prompts there named vendors. And three store paths carried three copies of
    one schema until one was written.

26. **The transcript is the record. A note and an answer are checked against it,
    never against each other.** A note is one model's reading of a transcript, and
    its `## Verbatim` section is that model's choice of quotes. Measured on the
    archived library: **1,004 of 5,989 Verbatim quotes (17%) had no match in their
    own transcript at the closeness the note check accepts (0.8)**, in 360 of 719
    notes — 581 of 5,316 (11%) in notes of English videos, 423 of 673 (63%) in the
    rest, mostly translations. (A first pass, counting quotes differently, found 571
    of 5,733 with nothing even at 0.5.) The answer check treated Verbatim as
    speech, so it passed those, and marked as `[not spoken]` quotes the transcript
    held word for word: **62 of 102** in the stored answers that could be re-scored.

    So the note worker checks every Verbatim quote against the transcript before
    the note is written, and marks a line it cannot find with the note format's own
    `[note: not found in the transcript as worded]`; and `marg ask` checks every
    quote an answer cites against the stored transcript of the video it cites,
    reporting how many it checked and found. The note's Verbatim stands in only when
    no cited video has a transcript on disk, and those quotes are counted apart.

    **Checking reads the transcript; it never repeats it.** What may be reported is
    whether a string was found and how close the nearest passage was, as a number —
    never the passage. The same line rule 1 draws everywhere else.

    The loose match is calibrated against chance, not chosen: of 570 cited quotes a
    transcript did not hold exactly, 121 reach closeness 0.6 against the transcript
    they cite and none against a random one. That is the `[loosely quoted]` line.

27. **A brief separates conditions every video must meet from parts the library
    must cover.** REQUIRED OF EVERY VIDEO holds one to
    four conditions, and a relevant video meets all of them. PARTS TO COVER holds
    two to eight parts of the subject, and a relevant video covers at least one;
    the library as a whole should cover every one. **Relevant means: every
    REQUIRED item, at least one PART, no EXCLUDED item** — and every reader of
    the brief states it that way: both judges, both planner prompts, the map and
    the expansion.

    One heading used to carry both ideas, and checkpoint 1 measured the cost on
    12 briefs. The pipeline read it three ways — `write_brief` and stage 2 as
    one-is-enough, the planner as coverage targets, stage 1 not at all — and the
    briefs failed in opposite directions. A topic made of parts got 6 to 9 items,
    and three of six briefs called every part required, so stage 2, which obeys
    the brief, would reject a good video for covering one part. A topic made of
    conditions needs all of them, and stage 2's "something the brief requires"
    would pass a video meeting one. Neither is visible from a run: the first
    declines good videos, which at stage 1 means never fetching them, rule 8's
    invisible error; the second is the wrong YES rule 8 names at stage 2.

    **A goal only the library can meet is a part.** "Include English and other
    languages" had no section and landed among the required items in 5 of 6
    briefs, where no judge can act on it and from where it reached no search. As
    a part it is a coverage line for the planner and, at the map, a query in
    that language with `relevanceLanguage` set.

    **Every criterion must be checkable from the title, description or
    transcript.** Two of six briefs on one topic made an on-screen screenshot a
    hard gate; nothing in this pipeline sees the picture, so such a criterion is
    one no video can be shown to meet.

    **The review keeps the limits; it merges or cuts, never appends.** Drafts kept
    every limit in 12 of 12 briefs and reviews broke them in 12 of 12 — up to 16
    EXCLUDED and 15 EDGE CASES items, briefs up to 2.2× longer — because the
    review prompt never restated the limits and its checklist only added. Every
    judge reads the whole brief, so each extra item is another way for a good
    video to fail.

    **Stored briefs are read as they were written.** Ledgers hold briefs under
    the old heading. Nothing parses a stored brief's sections — the section check
    reads only a reply just returned, and the export writes the stored text
    verbatim — so they need no translation, and must not be given one.

28. **A gate that runs more than once is idempotent, and what it decided travels on
    the query as state.** `validate()` runs on every query
    at least twice — where it was proposed (`Mapper.map`, `Planner.plan`) and again
    in `_loop` before its round — so `validate(validate(x).kept)` must equal
    `validate(x)`: the same queries, filters and waivers, and no new record. Running
    a pure gate twice costs nothing only when that holds; it is what makes the second
    pass safe to keep as the one path to the provider.

    It did not hold, and the failure was a decision stored as an absence. The first
    pass honoured a map's `publishedAfter=any` waiver and dropped the marker; the
    second saw an ordinary query and derived the window onto it — **3 of 12 maps** at
    checkpoint 2, on exactly the queries the map had exempted because their material
    is older than the window. Nothing failed and nothing was logged as wrong: the
    second pass's "derived" line read as the system working. The round's
    `recency_waived` counted that pass's records and so reported zero.

    So a waiver lives on `PlannedQuery.waiver`, never in `filters` — `filters` is
    exactly what the search provider is sent, and `any` is not a value it accepts.
    The pass that reads a marker records and logs the waiver once; a later pass
    derives nothing, records nothing and logs nothing. A round counts its waivers
    from the queries it will search, through the same `waiver_applies` the record
    uses, so one waiver is one count per round however many passes it went through
    (rule 14). The ledger's `filters_json` shows it under its own key with its
    reason, because no date ran.

    The general form, for the next gate: **if a pass consumes a marker, it must leave
    behind something every later pass reads** — or the second pass will make the
    decision again, without the information the first one had.

29. **A REQUIRED item must be one every PART's videos can meet.** A requirement
    worded around one part's subject rejects the videos of every other part, and
    both judges obey REQUIRED over the parts and over any edge case: checkpoint 3
    measured stage 1 refusing 3 explainers of a listed part and stage 2 refusing 2
    more on their whole transcripts. The brief prompts test each item against each
    part; the judges read an item so that each part can meet it, never as a licence
    to relax one a part's videos can meet. A stage-1 NO that guesses is MAYBE in
    code, under rule 8, kept in `said` and counted in `guessed_no`.

## Structure

```
src/marginalia/  cli.py  config.py  contracts.py  llm.py  ask.py  quotes.py
  core/     ids.py  ledger.py  library.py        identity, state, file layout
  providers/                                     the part guaranteed to break
  brain/    judge.py  territory.py  planner.py  selector.py   reasoning; no I/O
  flow/     search.py  discover.py  acquire.py  store.py  refetch.py  doctor.py  export.py
                                                 execution
  enrich/   note.py  timestamps.py               what happens after storage
```

- `cli.py` is the only entry point.
- `flow/acquire.py` is the one path a transcript is *fetched* by — ingest, discovery
  and refetch all call it (2026-10-11). It writes `mark_pending` first, walks
  `providers.transcript_order`, and records the provider that answered. Before it,
  discovery and refetch used only the first configured provider, so rule 4's "a new
  provider is one file" held for ingest alone.
- Every `try` around a model call that could catch a refusal re-raises
  `LlmConfigError` first; `tests/test_refusal_policy.py` scans the source for it. The
  scan is lexical — it sees a `.complete(` call written inside the `try`, not one
  reached through a helper — so it catches the shape that was missed three times and
  does not replace asking rule 25's question of a new call site.
- `quotes.py` also holds the note format's section names and the one section
  splitter every reader of a note uses (validator, Verbatim check, answer check,
  selector summary, snapper).
- `flow/store.py` is the one path a fetched transcript is stored by — ingest,
  discovery and refetch all call it — and `core/library.py`'s `render_transcript` is
  the one definition of the stored format, so a length measured before storing is the
  length the note worker will see.
- `quotes.py` is the one quote matcher. The answer check and the note's Verbatim
  check both use it, so a quote cannot pass one and fail the other for reasons of
  implementation. It is Unicode-aware, for the reason rule 25 records.
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
  retroactively. Two reads are deliberately not accesses: hashing it (`marg doctor`)
  and snapping a stored note's timestamps against it. Checking an answer's quotes
  *is* one — a cited video is a video that was used.

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

Do not add ASR or Whisper, or embeddings — each belongs to another milestone in
`PLAN.md`. **Note selection shipped as M7** and is the exception that proves the rest:
it was built when the ceiling arrived (874,987 tokens against a 1M window, 44 notes of
room), not when a count crossed a line. Two stages, and **both read whole notes** — the
selector sees one line per note and returns identifiers, `ask` loads the chosen notes
unchanged. No embedding, no vector, no chunk, no fragment; that constraint is the
milestone, not an implementation detail of it. **Content fingerprinting (Gate 3) is not on that list:
it is deliberately rejected**, and PLAN.md §10 has the argument. Adding it back needs that
argument answered, not a milestone. Do not build a gold evaluation set or tune the judge;
both wait until real failures are observed, which is what the `decisions` log exists to
make possible.

**`/ask` loads every note, and at 309 that is still right.** The number that used to sit
here — *"under ~100 videos the answer is all of them"* — was a guess, and it has since been
measured and found wrong in the direction that matters: **182 notes produced better answers
than 87 did.** More context helped. So a note count is not evidence that a selection layer
is needed, and a future session reading a stale threshold against a library that has passed
it would build the one thing this project has twice decided not to build, triggered by a
number already known to be false.

**The trigger was a symptom, not a count** — and the symptom that arrived was neither of
the two predicted. Cost stayed at $0.12 and the answers stayed good; what ran out was
room. Below the budget every note still loads, because that is the measured-better path
and M7 must not cost it.

**Both stages read whole notes, never fragments.** This is a constraint, not a
preference, and M7 is built to it. The standard approach — chunk each note, embed the chunks,
retrieve the chunks — destroys the whole-video understanding the note format exists to
produce, which is the same argument that rejected chunking transcripts at the start of the
project (PLAN.md §10). A cheap first pass over note *titles*, selecting which full notes to
load, preserves it. Vector search over fragments does not. Without this written down the
default implementation is the wrong one.

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
marg note [<video_id>...]          write notes; --limit N, --regenerate [--all],
                                   --missing-only, --status
marg refetch                       retry transient fetch failures; --limit N, --dry-run
marg snap-timestamps [<id>...]     align an older note's timestamps; --dry-run
                                   (new notes are aligned as they are written)
marg ask "<question>"              answer from the notes; --deep <id>, --json,
                                   --no-save (saving to answers/ is the default);
                                   exits 1 when no answer was produced
marg run export <run_id>           write exports/<run_id>/ from the ledger
marg doctor                        compare disk against ledger; read-only; --json
marg reconcile [<id>...]           record transcripts an interrupted store left on
                                   disk; no network, bytes untouched
marg llm-check                     one request to the configured provider
marg status <url|id>               show ledger state for one video
marg list [--status S]             list videos by asset state (default has_transcript)
marg guard-write                   internal: the PreToolUse hook, reads stdin
```

Config resolution: `MARGINALIA_CONFIG` → `./config.toml` → `~/.marginalia/config.toml`.
