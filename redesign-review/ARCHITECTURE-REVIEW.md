# marginalia — architecture review and redesign candidate

Candidate: worktree `C:\youtube-agent\projects\marginalia-redesign`, branch `redesign/agent`,
15 commits on top of `56d04fa` (listed in §9). Decision record: `ARCHITECTURE-DECISION.md`. Coverage of what
was read: `COVERAGE.md`. Independent reviews (verbatim): `independent-reviews/`.

## 1. Verdict

**Keep the architecture; refactor the seams where state was implicit and policy was
per-caller. No rewrite.**

The layering (`cli` → `flow` → `brain` → `llm`, with `core`, `providers`, `contracts`) is
sound and AST-enforced; the ledger as source of truth, write-before-fetch, immutable
transcripts, text-protocol parsers and whole-note/whole-transcript processing are each
defended by measured failures and nothing found here argues against them. Every confirmed
defect traced to one of two shapes — **state reconstructed instead of stored**, or **one
policy implemented separately by each caller** (rule 25's failure class) — and each was
fixable by giving the state or the policy one home, without moving a package boundary. A
rewrite would have traded ~17k lines of measured safeguards and 1,624 tests for code that
looks different.

## 2. What changed, and why (one commit each)

| # | commit | problem (evidence) | change |
|---|---|---|---|
| 1 | `b15c7f4` | Stage-2 "decided anything?" was rebuilt from `verdict` + exact reason text. A judge's literal MAYBE became YES and counted as coverage; NO-on-unreadable was stored as `yes`; three historical reason shapes were never recognised, so those videos read as "already judged" forever. Gate 2 read any `no` row, including its own search-stage echoes; `current_verdicts` had no caller. Read-only `doctor` refused any ledger missing an additive column. A legacy ledger was migrated before being refused. (probe_state.py on baseline; R1 F4/F15; R3 S4/S5/S10; git history of the reason strings) | `decisions.outcome` + `decisions.said` (additive); one `legacy_outcome` classifier for history; literal MAYBE kept as MAYBE (kept, noted, not coverage); NO-on-degraded → MAYBE + DEGRADED, `said=no`; `covers` requires YES; Gate 2 = current *judgement* via `current_verdicts`; read-only tolerates additive gaps; legacy table refused before any write. |
| 2 | `f829066` | A held transcript went through stage 1 first; a title-only NO then blocked stage 2 from reading bytes on disk, permanently via Gate 2. Held keepers were re-screened every round. Three long transcripts in a row ended discovery as a "config error". (R1 P1/P2/P4) | Partition before stage 1 (held+judged → skip; held+unjudged → stage 2 from disk; else stage 1); Gate 2 does not let a *stage-1* NO block held bytes; run-scoped `read_from_disk` in Gate 1; overflow streak reset per stage-2 call. |
| 3 | `05ebb8c` | Fetch implemented three times, three policies; discovery/refetch used only the first provider and recorded it as provenance whoever answered; captionless counted as failure in discovery; a provider-name typo condemned the video for good; an unclassified provider exception or an interrupt left a `pending` row nothing retried; ingest fetched over an untracked transcript and died. (R1 F10/F16; R3 S1–S3, S13; AUDIT new-1/new-5) | `flow/acquire.py` (one path; `Acquisition` in contracts); unknown provider → `ConfigError` before any row; ingest refuses an untracked transcript without network; discovery counts `no_captions` and skips known-gone videos (`unavailable`); refetch walks the whole chain and retries stale `pending` rows. |
| 4 | `54d7d88` | Mapper (×3 samples), library baseline and planner swallowed 402s; the run then died as `incomplete`. `marg note` exited 0 on a refusal. (TECHNICAL-MAP §12.7; R1 F6; R3 S7) | Re-raise in the three sites; `provider_refused` stop reason for discovery; note exit 1; **AST test that fails if any try around `.complete(` catches broadly without re-raising `LlmConfigError` first** (self-tested on a synthetic violation). |
| 5 | `a7f8980` | A failed note left no trace, so the same three failing videos took the first three attempts of every drain and stopped it (R2 F1). Discovery counted as coverage videos the note worker refuses (floor, cross-topic DEGRADED) (R1 F3). Note write order untested (mutant M37). | `note_failures`/`note_failed_at` (additive), cleared on success, never written on refusal; queue puts untried first; `note_refusal` shared by worker and discovery; ordering test. |
| 6 | `51ab8e4` | Five readers parsed note sections five ways: a `# comment` in Verbatim emptied the section (note failed every drain) and hid later quotes from the check; `### Verbatim` passed validation but was invisible to the answer side. (R2 F2, REVIEW C20) | One splitter in `quotes.py` (`NOTE_SECTIONS`, `note_lines`, `note_sections`) used by validator, Verbatim check, answer fallback, selector summary, snapper. |
| 7 | `8119517` | ESCALATE with U+2011 printed as the answer (exit 0); second call never budgeted (120% of ceiling); capped transcript cut mid-line, undisclosed; refused requests recorded as "the notes were enough"; `--deep` disabled escalation; DROPPED read one line deep; overflow trimmed in id order. (R2 F3, F9–F13; REVIEW C8) | Dash-normalised, `_find_id`-repaired escalations; transcripts opened into the room left under the ceiling, cut at a line boundary, disclosed in the header, record, stdout and JSON; `escalations_refused`; escalation stays available after `--deep`; DROPPED read to WHY; overflow shuffled before trim. |
| 8 | `2b1eaca` | Reversed quotes ("never" for "always", inserted "not") scored 0.85 — "loosely quoted"; NFC/NFD failed exact match; «» „“ 「」 invisible; a Verbatim line the note worker marked unfound still verified answers. (R2 F5, F7, F15, F16) | Polarity guard inside the scorer (negation *counts* on the differing words — see §4a: the first version over-fired on contractions); NFKC; more quote styles (not ‘’); `standing_verbatim`. Threshold values unchanged; scores change only where a negation count differs. |
| 9 | `62f56d9` | A chat replay could be chosen as captions; a zero-line track went to `needs_asr` with an auto track beside it; per-user/per-minute rate limits locked the API for the day; reconcile overwrote an unreadable `meta.json`. Six mutants survived the whole suite. (R1 F2, F8; R3 S8; mutants M08–M12, M16) | `live_chat`/`rechat` excluded, next-candidate fallback; rate-limit classification; reconcile leaves the file; tests for the six unwatched paths. |
| 10 | `f2a512e` | Planner counters counted search hits; this run's NOs reported as "from an earlier run"; "declined" omitted overturned YESes; planner shown requested rather than applied filters; stage 1 could run many batches past `max_requests`. (R1 F11, F12, F17; REVIEW A9; §12.4) | Unique-video counters; `rejected_this_run`; honest declined line; applied filters to the planner; stage 1 bounded by the request budget. |

`RULES.md` was edited only where a rule's text changed (rule 8's stored-outcome paragraph,
rule 13's held-transcript paragraph, the Structure list). No prompt file changed.

## 3. Changed test expectations (each justified in the test's docstring)

* `test_judge_full.py`: literal MAYBE → MAYBE not YES (4 tests); NO on degraded /
  damage-blaming / unsupported-degraded → MAYBE with `said=no` (9 cases). The old docstring's
  premise — "there is no stage 3, so a MAYBE has nowhere to go" — predates the 2026-10
  kept-not-coverage state.
* `test_search.py`: `test_a_later_yes_does_not_undo_an_earlier_no` used a **search-stage**
  `no` (gate bookkeeping) as if a judge had declined; replaced by five tests pinning the
  intended rule with real judgement rows.
* `test_discover.py`: the stage-2 MAYBE test now expects `maybe` + `uncertain`; a this-run
  NO now counts as `rejected_this_run`; the fixture's note floor is 0 (its transcripts are a
  few words; the floor has its own test); `_store` takes an `Acquisition`.
* `test_boundaries.py`: the pinned `contracts.__all__` gains the new vocabulary.
* `test_refetch.py`, `test_content_hash.py`: patch the shared provider seam instead of the
  removed per-module one; behaviour under test unchanged.

## 4. Reconciliation of findings

Reviewers' findings were checked against source and, where marked, reproduced by probe or by a
test that fails on the baseline. Agreement between reviewers was not treated as proof.

**Prior audit items (REVIEW / TECHNICAL-MAP / AUDIT)** — see `independent-reviews/` for the
reviewers' full status tables. Summary of items this candidate settles: §12.2 siblings (R1 F3),
§12.3 (`current_verdicts` now the Gate 2 path), §12.4, §12.6, §12.7, §12.8 (partition), Q4
(literal MAYBE, NO+damage), C1(b), C8, C9 (safe parts), C20, A9, AUDIT new-1, new-3, new-5,
new-6 (stage-1 failure now tested). Items confirmed still open are in §6.

## 4a. Independent post-implementation review

A fresh read-only reviewer that did not write the code reviewed the diff after implementation
(`independent-reviews/R-final-post-implementation.md`). It found **one blocking defect**: the
first polarity guard treated a contraction swap ("don't" / "do not") as a reversal and scored
faithful quotes 0 — the very failure rule 26 records. That was reproduced and fixed by counting
negations rather than detecting them. It also found ten S2/S3 issues (a lost stop for a
configuration in which nothing fits the context window, a transcript opened twice, splitter
regressions on four note shapes, a drifted ledger written before refusal, and smaller ones);
each was checked against the code and fixed with a test in `3d019cf`. The reviewer's own list
of things it verified as correct is in that file.

## 4b. Second independent review — six boundary cases (uncommitted corrections)

A separate review reproduced the 1,757 passing tests and reported six boundary cases from
focused synthetic probes. Each was treated as a claim: reproduced on a fresh snapshot of
`9b95fab` (and, for origin, of `56d04fa` and `c590f16`), given a regression test and a
legitimate control, and shown to fail before the correction and pass after it. The
corrections are **in the working tree of `redesign/agent`, not committed**, for independent
review.

| | finding | verdict | origin | correction |
|---|---|---|---|---|
| A | A negation moved to another clause kept the per-quote negation count equal: 0.916667, "loosely quoted" in an answer, not marked unfound in a note. | **Confirmed** through both paths. | **Regression from an intermediate candidate.** Baseline had no guard (scores the same 0.916667). `c590f16`'s presence-based guard rejected it (0.0, unfound) but scored contractions 0; `3d019cf` fixed contractions by counting over the whole quote, which let a moved negation balance. | `quotes._flips_polarity` compares negations **per changed span** (each `SequenceMatcher` opcode): a move is two unbalanced changes, a contraction one balanced change. Contraction scores are unchanged from baseline and `3d019cf` (probe: 0.777778 on all three); no threshold moved. **Known limit, tested as such:** a transposition beside a negation ("not always" / "always not") aligns the two "not"s, so no negation differs; baseline and `c590f16` miss it too. |
| B | A four-backtick fence was not recognised; a `~~~` line inside a backtick block toggled the fence; headings in code became sections. | **Confirmed**, and widened: a ```` ```python ```` line inside a ```` ``` ```` block also closed it. | **Introduced by the candidate, partly a regression.** Baseline had no fence concept (every `#` line in code was a heading). `51ab8e4` added fences with "any marker toggles" (the tilde case); `c590f16` still recognised four backticks; `3d019cf`'s inline-code fix (`[^`~]*$`) stopped recognising them. | One explicit policy in `quotes.py`: opens on ≥3 backticks or ≥3 tildes at any indentation (a backtick opener's info string has no backtick, so ```` ```cmd``` ```` stays inline); closes only on the **same character, at least as long, nothing after**; an opener with no closer is not a fence and the scan continues. Tested through `note_sections`, `verbatim_of`, `missing_sections`, `check_verbatim`, the selector summary and the timestamp snapper; ordinary, inline, indented, repeated-section and unclosed cases still pass. |
| C | With `videos` dropped from an initialised ledger, a read-only open succeeded and the first query raised `OperationalError`; baseline refused with `LedgerError`. Write mode recreated the table. | **Confirmed and qualified.** | Read-only acceptance **introduced** by `b15c7f4` (skipping every absent table as an additive gap). Write-mode recreation **inherited** from baseline (`CREATE TABLE IF NOT EXISTS`). Also found: a ledger without `runs` opened read-only and then crashed `marg doctor`'s `list_runs` (same origin); an empty file opened read-only (baseline refused); a read-only refusal left the SQLite handle open, so on Windows the file could not be moved aside as the message says (inherited). | Required tables established from history: `videos`, `decisions` and `quota` were all created by M2 (`95993ef`), the first schema without the legacy `rejections` table, so every supported ledger has them; `runs` (M2b `0fabe7d`), `queries` (B1 `5dedd1f`) and `plans` (B6 `a32389c`) are legitimate gaps. `_CORE_TABLES` missing → refused **before any write**, in both modes, with a "damaged, not old" message; later tables still skipped read-only and added in write mode; `list_runs` answers empty for a ledger that predates `runs`; a file with no marginalia table is a fresh ledger in write mode and refused read-only; the read-only refusal closes its connection. |
| D | With `max_transcripts=2` and A opened by `--deep`, a request for A then B took the last slot with A, refused B as over the limit, and opened only A. | **Confirmed and qualified:** duplicate-later ordering also misreported A as "over the limit" instead of "already opened". | **Inherited.** Baseline granted the duplicate (opening A twice) and refused B the same way; `3d019cf` (F3) stopped the second copy but kept the check after the cut. | `escalation_requests` takes the opened ids and refuses a duplicate as "already opened" **before** the limit is applied; repeated ids are reported once; the limit still binds new requests. |
| E | `marg search` passed no disk check: a `has_transcript` row over a missing file was reported `new` (held); with the check, `rejected`. A `pending` row over bytes also differed. | **Confirmed.** Production callers traced: discovery (`_gather` → `run_search`, passes `library.has_transcript`) and `marg search` (`_cmd_search`, passed nothing). | **Introduced** by `f829066` (exemption read from row status); `3d019cf` (F8) gave discovery the disk check only. Baseline had no exemption. | `_cmd_search` passes `library.has_transcript`; a caller with no disk to ask gets **no** exemption — the stage-1 NO stands, as on baseline — instead of a "held on disk" claim nobody checked. Rule 13 updated. |
| F | Exactly five overflowing candidates: five overflows, `stopped_because=max_rounds`; six: five judged, `context_window`. | **Confirmed.** Across rounds, a fifth overflow ending round two let round three search and screen before stopping. | **Introduced** by `3d019cf` (F2's run-level stop, checked only before a next candidate). Baseline had no run-level stop. | The same check also runs after the stage-2 loop, so the stop fires at the fifth; the fifth is stored, logged (`too_large`) and counted before it. Tested at 4/5/6, across a round boundary, and with a fitting call resetting the streak. Rule 24 updated. |

Evidence: 28 new test cases fail on `9b95fab`'s source and pass on the corrected source (same
test files; `newtests-pre2.txt` / `newtests-fix2.txt`). Controls that pass both before and
after: contraction equivalents in either clause and through answer grounding; ordinary,
inline, indented, repeated and unclosed fences and a shorter run inside a longer fence; later
tables absent, fresh initialisation; a single new escalation with one slot; a held exemption
when the disk agrees; four overflows, and a reset.

The third area reviewer (tests/recovery/diagnostics) was cut off by a usage limit before
writing a report. Its probe outputs and baseline mutation results were recovered and re-checked
by the coordinator and are cited as such; no claim here rests on it as an independent review.

## 5. Validation

### 5.1 Suite
* Baseline in C under isolation: **1,624 passed** (reproduces the stated baseline).
* Candidate: **1,757 passed, 0 failed, 0 skipped** (+133 tests, none removed except the
  replaced `test_search` case).
* Corrected working tree after §4b (uncommitted; run on a fresh snapshot with credentials
  stripped): **1,807 passed, 0 failed, exit 0** (+50 tests, none removed or loosened; one
  draft case was replaced by a recorded-limit test, see §4b A).
* Every run: network blocked (socket guard), home redirected to a scratch folder, an injected
  `MARGINALIA_CONFIG`, no API keys in the environment, and an audit hook that raises on any
  open/sqlite/rename/remove touching the live library, archive, home config, A or B. Each run
  printed "no protected-path access attempted" and the import path of the worktree under test.
  A negative-control run confirmed the guard blocks the live ledger, the home config and the
  network.

### 5.2 Baseline-vs-candidate sensitivity
New test files run against the **baseline source** (scratch copies; stand-in imports where an
API is new):
* `test_note_sections.py`: 9 of 11 fail on baseline (the 2 that pass pin already-correct cases).
* `test_ask_escalation.py`: 8 of 8 fail on baseline.
* `test_quote_guards.py`: 6 of 9 fail on baseline (the 3 that pass are no-over-fire guards).
* `probe_state.py`: all six decision-state defects reproduce on baseline and are fixed.

### 5.3 Revert-the-guard mutation run
31 mutations, each reverting one guard this candidate adds, applied one at a time to a scratch
`git archive` of HEAD and run against the targeted test files through the isolation harness
(`scratchpad/mutC/mutations.py`, `run_all.sh`, `results.tsv`).

* **31 of 31 caught** after one test was strengthened. The first pass caught 30: K24 (the
  chat-replay exclusion) survived because the new zero-line fallback still reached the
  automatic track; the test now asserts the chat replay is never downloaded, and K24 fails it.
* Caught mutants include every decision-state guard (K01–K05), the held-transcript partition
  and Gate 2 exemption (K06–K07), acquisition (K09–K12), refusal policy (K13, K14, K30), note
  drain (K15, K31), coverage/note agreement (K16), section splitter (K17), the answering path
  (K18–K21), quote guards (K22–K23), reconcile (K25), the stage-1 budget and planner counters
  (K26–K28) and rate-limit classification (K29).
* The earlier partial review's baseline mutants M08–M12, M16 and M37 that survived the
  baseline suite are now each covered by a test (`test_coverage_gaps.py`, `test_note.py`).

One earlier driver run reported "no tests ran" for every mutant (a `\r` in the test list on
Windows); those results were discarded and the run repeated with an INVALID guard.

**Re-run on `3d019cf`** (after the post-implementation fixes): 28 caught at first, and the 3
reported "survived" (K04, K05, K07) turned out **not to have been applied** — the review fixes
had moved their anchors, and the driver classed an unapplied mutation as survived. They were
re-anchored, the driver now reports an unapplied mutation as INVALID (`run_some.sh`), and all
three are caught. **Final: 31 of 31 caught on `3d019cf`.**

**Re-run on the §4b corrected tree** (uncommitted; each mutant on its own fresh copy of the
snapshot): K04 and K05 were re-anchored to the corrected ledger code, and twelve mutants were
added, each reverting one §4b guard — per-change negation count (N-A), fence character, length
and four-backtick opener (N-B1–3), core-table check, read-only empty file, pre-M2b `list_runs`
and read-only handle release (N-C1–4), duplicate-before-limit (N-D), the CLI disk check and
the no-callback fallback (N-E1–2), and the post-loop overflow stop (N-F). **43 of 43 caught**,
none unapplied. (N-C4's test depends on Windows file locking; on another OS it would pass
vacuously.)

### 5.4 Synthetic workload comparison
`scratchpad/bench/bench.py`, identical deterministic input, run against the pristine baseline
source and the candidate through the isolation runner, three interleaved repetitions each.
Workload: a 3-round discovery over a 200-video pool with overlapping search results (9
queries × 10 results per round), 10 transcripts already held (2 noted); then 4,000 Gate-2
lookups and the note queue over a ledger with 20,000 decision rows and 2,000 transcripts.
Fake judge/planner/provider; requests counted at the fakes.

| measure | baseline | candidate |
|---|---|---|
| stage-1 batches (model requests) | 11 | **10** |
| stage-2 calls | 70 | 70 |
| `metadata` decision rows written | 94 | **87** (held videos no longer re-screened) |
| total decision rows | 434 | 427 |
| videos stored | 57 | 57 |
| candidates reported to the planner | 270 (search hits) | 223 (videos) |
| discovery wall time, 3 runs | 10.9 / 15.1 / 12.9 s | 11.2 / 10.5 / 11.0 s |
| peak traced memory | 0.35 MiB | 0.42 MiB |
| 4,000 Gate-2 lookups | 1.17–1.46 s | 0.61–0.91 s |
| note queue, 2,000 videos | 0.46–0.51 s | 0.53–0.65 s |

Reading: fewer model requests and rows on the same input, no wall-time regression within the
noise of this machine, Gate 2 faster (it reads one video-topic slice), the note queue ~15%
slower (the added sort; sub-second at 2,000 videos). Memory differences are negligible.
These are synthetic; real-run costs depend on how often held videos and literal MAYBEs occur.

## 6. Decisions for the owner (not made here)

1. **Stage-1 NO is permanent per normalised topic, across briefs.** Each run writes a new
   brief, yet a title-only NO decided against an earlier brief blocks the video for the topic
   forever (Gate 2). Options: keep; expire on brief/prompt version; or re-screen on demand.
2. **One DEGRADED from any topic suppresses the note forever** (pinned as intentional:
   one transcript, every topic). History matters: the damage-word proxy over-fired before
   C1(a) was fixed, so real ledgers may hold false DEGRADED rows that this rule makes
   permanent. Option: latest judged stage-2 reading wins, or a one-off audit.
3. **"Too large for this model" waits for a model-name change.** A wider window under the same
   id never retries. Options: key on a config fingerprint (model + max tokens), record input
   size, or an explicit re-judge flag.
4. **Map revisions change no search after round 1** (C5): the planner can edit only the
   judges' vocabulary line, and rule 19's asymmetry is inverted relative to what the code
   affects. Delete revisions, or feed added terms to expansion.
5. **Caption language preference** (R1 F2 remainder): with no hint, an English machine
   translation beats `*-orig`; a foreign *manual* track beats an auto track in the spoken
   language. Both store an immutable transcript; the choice is a quality-vs-language tradeoff.
6. **Quote scorer recalibration** (R2 F4, F16 CJK): scores depend on quote length (window =
   quote + 4 words), and CJK has effectively no loose matching. Fixing either moves the
   calibrated 0.6/0.5/0.8 thresholds; recalibration needs the archived corpus, which this
   work deliberately did not read.
7. **End-of-run coverage call** (C6): the run's printed coverage is one round stale or absent;
   fixing it costs one call per run.
8. **Truncated answer exit code** (R2 F14): a fragment exits 0 (the saved record says
   "fragment"). Distinct exit code, or record the intent.
9. **Config without `[library]` resolves to the live library path** (config default; R3 S11).
   Safer: require it, like `llm.model`.
10. **Prompt-text corrections** that need real-model validation: answer.md "at most three",
    "aligned by code", repeated question (A3), era carry-through (A4), select_notes field name
    (A12), plan/round.md map-revision wording.
11. **RULES.md restructure** (REVIEW B9): requirement / enforced-by / evidence / history.

## 7. Known open items (confirmed, not changed)

* Quota pre-check never fires with an unmetered provider in the chain (R1 F14); a filter
  value Google rejects drops the whole query instead of widening (R1 F18, inferred);
  `write_brief`/`expand_queries` have no failure fallback (R1 F19).
* Quote attribution is "any citation within 200 characters", timestamps are never checked,
  straight-quote pairing can flip after a stray `"` (R2 F6, F8); uncited quotes are not on
  stdout (F7 part).
* The selector's WHY (model-written, ≤300 chars) is saved to `answers/` (R2 F20, inferred).
* `doctor` does not report stale `pending` rows (refetch now retries them).
* Unknown `[llm.tasks.X]` names are silently ignored (C15).
* Semantic failures (fluent wrong verdicts/notes/answers) remain undetected by any counter —
  rule 24's recorded limit; nothing here changes that.

## 8. What is verified and what is not

* **Verified correctness improvements:** every item in §2, each by a test that fails on the
  baseline or under the reverting mutation (§5.2–5.3).
* **Measured performance:** §5.4, synthetic only.
* **Architectural judgements:** §1, the keep/refactor choices, and the decision record.
* **Estimated cost effects:** literal-MAYBE and held-from-disk changes can add stage-2 calls
  (~0.09¢ each, B3's figure) where a held video would previously have been dropped by stage 1;
  the stage-1 budget and unique counting can only reduce calls; the refused-escalation second
  call adds at most one call per affected answer.
* **Needs real-model validation:** whether planner behaviour improves with honest counters and
  headings; how often judges answer MAYBE; whether the PARTIAL transcript header changes
  answers. Fake replies passing tests says nothing about any of these.
* **Quote checking is not semantic.** The §4b A guard refuses negation changes it can name, per
  changed span; it does not establish that a loosely matched quote means what the source
  meant. Transpositions beside a negation, antonyms ("increase"/"decrease"), and non-English
  negation are outside it. Synthetic tests show the named cases only.

## 9. Commits on `redesign/agent`

| commit | content |
|---|---|
| `35fe546` | decision record and the two area reviews |
| `b15c7f4` | stored judge outcomes; Gate 2 from judgements; read-only and legacy-ledger safety |
| `f829066` | held transcripts read at stage 2, never re-screened at stage 1 |
| `05ebb8c` | one acquisition path for ingest, discovery and refetch |
| `54d7d88` | refusals never swallowed; AST enforcement |
| `a7f8980` | note drain failures recorded; coverage asks the note worker |
| `51ab8e4` | one note-section splitter |
| `8119517` | answering: budgets, disclosed cuts, repaired escalations, unbiased trims |
| `2b1eaca` | quote checks: reversals, quote styles, the note's own marks |
| `62f56d9` | providers (caption fallback, rate limits), reconcile, unwatched-path tests |
| `f2a512e` | planner evidence: videos not hits, this-run NOs, applied filters, stage-1 budget |
| `ca91a15` | RULES updates and coverage checklist |
| `c590f16` | chat-replay test strengthened after the mutation run |
| `3d019cf` | fixes from the independent post-implementation review |
| `9b95fab` | this report and the post-implementation review record |
| (uncommitted) | §4b corrections from the second independent review, left in the working tree for review |
