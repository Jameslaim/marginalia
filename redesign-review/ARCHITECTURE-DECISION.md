# Architecture decision — marginalia redesign candidate

Status: **accepted for the candidate on `redesign/agent`** (worktree C), pending the owner's review.
Baseline: `56d04fa` (1,624 offline tests passing, reproduced in C under isolation).
Evidence for every claim below is in `ARCHITECTURE-REVIEW.md` (findings, probes, test results).

## Verdict

**Keep the architecture. Refactor four specific seams. Do not rewrite.**

The layering — `cli` → `flow` (executes) → `brain` (reasons, no I/O) → `llm` (the only model call
site) with `core` (ledger + library paths), `providers` (the fragile part) and `contracts` (every
cross-module structure) — is sound, and it is enforced by AST tests rather than habit. The ledger
as the single source of truth, write-before-fetch ordering, immutable transcripts, append-only
decisions and text-protocol parsers (no JSON mode, portable across providers) are each defended by
a measured failure in `RULES.md`, and I found no evidence that any of them is wrong.

What *is* wrong is concentrated in places where **state is encoded implicitly** and where **one
policy is re-implemented per caller**. Both are the rule-25 failure class ("a guard added to one
caller must be traced to every caller") showing up again, and both are fixable by giving the state
or the policy one home, without moving the package boundaries.

A full rewrite would discard ~18k lines of measured safeguards and 1,624 tests to fix problems
that are local. Its benefit would be that the code looks different. Rejected.

## Confirmed structural problems (each reproduced on the baseline; see review §Probes)

### D1. Stage-2 decision state is not persisted; it is reconstructed from verdict and reason text

`decisions` stores `verdict ∈ {yes,no,maybe}`, `evidence` and free-text `reason`. Whether a row
is a *judgement at all* exists only in memory (`FullVerdict.judged`) and is re-derived later by
`_judged_for_topic` from `verdict == "maybe"` **or** an exact match of `reason` against
`UNJUDGED_REASONS`. Consequences, all reproduced:

* A judge that literally answers `VERDICT: MAYBE` is resolved to **YES** and counted as coverage
  (`JudgedVideo.covers == True`) — uncertainty turned into success, the exact "wrong YES" rule 8
  says hides at stage 2.
* A judge that answers NO on degraded evidence is stored as **`yes`** — the ledger no longer says
  what the judge said.
* `maybe` means two things: "the judge was unsure" and "nothing judged it". A genuine MAYBE would
  be re-judged on every run as if it never happened (once the first bug is fixed).
* Historical unjudged rows are recognised only by exact current reason strings. Git history shows
  three legacy shapes the matcher misses (`"opening judge failed, kept"`, `"verdict could not be
  parsed, kept"` at stage `opening`, and pre-`cd43962` no-verdict replies stored as `yes` with
  `"reply broke the output contract (… no readable verdict …)"`). Those videos are treated as
  "already judged for this topic" forever. Conversely a one-character variant of a recognised
  string is treated as a real judgement.
* A transcript a model refused as too large is skipped for that model forever (`model ==` check),
  with no way to say the window changed.

**Decision — refactor:** persist the outcome explicitly. Two additive nullable columns on
`decisions`: `outcome` (`judged` | `call_failed` | `too_large` | `truncated` | `no_verdict`) and
`said` (the judge's literal verdict word before any resolution). One function,
`DecisionRow.is_judgement`, answers "did a judge actually decide here?"; legacy rows (`outcome IS
NULL`) go through one documented legacy classifier that also knows the three historical shapes.
Stored `verdict` becomes the *resolved* verdict and never claims more than the judge said: a
literal MAYBE stays `maybe` (judged, not coverage); NO-on-degraded becomes `maybe` + `DEGRADED`
(judged, kept, no note, not a rejection). Coverage requires `verdict == yes`.

*Alternatives considered:* (a) keep inferring from reason text and add the missing legacy strings —
cheaper, but keeps the fragile encoding for every future reason change; (b) a separate
`judgements` table — cleaner on paper but a non-additive migration of the one table every gate
reads; (c) rewrite historical rows in place — rejected: the live ledger must not be migrated
destructively and the project's own policy is "NULL is the honest answer for history".
*Compatibility:* additive columns only, added by the existing `_ADDITIVE_COLUMNS` path on first
write-mode open. No row is rewritten. Old code reading a new ledger ignores the columns.

### D2. Gate 2 reads every `no` row — including its own echoes — and ignores "later stage supersedes"

`latest_decision(verdict="no")` matches any stage, including the `search`-stage `no` rows Gate 1
and Gate 2 themselves write, while `current_verdicts` (which RULES 11 and its own docstring call
*the* read path for relevance) has no production caller.

**Decision — refactor:** Gate 2 asks the ledger for the current judgement per (video, topic) through
`current_verdicts`, over judgement rows only (`is_judgement`). Search-stage rows are bookkeeping and
never a rejection. Behaviour on every reachable baseline path is unchanged (a stage-1/2 NO still
blocks); what changes is that an echo can no longer stand in for a judgement and that a later
stage's verdict for the same topic wins, as RULES 11/12 already state.
*Product question left open, not changed:* a stage-1 NO is permanent for its normalised topic even
though each run writes a new brief. See "Decisions for the owner".

### D3. Acquisition is implemented three times with three different policies

Ingest (`cli._fetch`) walks `transcript_order`, lets a transient failure outrank "no captions" and
stops on a permanent failure. Discovery (`_fetch`) and refetch (`_try`) use only
`transcript_order[0]`, and both record `transcript_order[0]` as provenance whichever provider
answered. Discovery also counts a captionless video as a fetch **failure** (`report.failed`), so the
planner reads a route as an error. RULES 4 promises "adding a replacement provider means adding one
file"; today a second provider would be used by ingest and silently ignored by discovery and refetch.

**Decision — refactor:** one `flow/acquire.py` (`acquire(ledger, video_id, providers) ->
Acquisition`) owning mark-pending-first, the provider walk, failure classification and the
resulting ledger state. Ingest, discovery and refetch call it. `Acquisition` and its outcome
vocabulary go in `contracts.py` (rule 10). Storage stays in `flow/store.py`, which already was the
single store path.

### D4. "A refusal is authoritative" is re-implemented per call site, and three sites miss it

`judge_metadata`, `judge_full`, the note drain and the selector re-raise `LlmConfigError` ahead of
their catch-all; the territory mapper (×3 samples), the library assessment and the round planner do
not. A 402 or an hour-long `Retry-After` during mapping is swallowed three times ("map unusable"),
the baseline records "assessment call failed" as though the model failed, and the run then dies one
call later with `stopped_because = "incomplete"`.

**Decision — fix and make it structural:** re-raise in the three sites; record
`stopped_because = "provider_refused"` (the note drain's existing word) when discovery ends on a
refusal; and add an AST test that fails if any `try` around `.complete(` has a broad `except`
without a preceding `except LlmConfigError: raise`. The test, not reviewer diligence, is what
enforces rule 25 for this class from now on.

### D5. Read-only diagnostics break on any older ledger

`Ledger(read_only=True)` runs `_check_schema`, which demands every column the row dataclasses
declare. Any additive column (including D1's) therefore makes `marg doctor` refuse a ledger that a
write-mode command would have migrated silently — the diagnostic is unusable exactly when a version
changes. **Decision — fix:** in read-only mode, additive columns may be absent and read as NULL;
non-additive drift still fails loudly.

## What is deliberately kept

* Whole-note selection and whole-transcript processing (RULES Scope; measured: 182 notes beat 87;
  chunking destroys the per-video understanding the note exists for). No embeddings, no vector store.
* Two-stage judging, rule 12 (keep declined transcripts, note them), rule 13's five-line contract.
* Text protocols for every model reply; one model; config-only provider swap.
* The ledger schema's additive-only migration policy.
* The planner/territory design, including map revisions — but see the open product decision.

## Compatibility and migration

* Schema: additive nullable columns only. Existing rows are never rewritten; legacy semantics are
  read through one classifier. Tested on synthetic historical ledgers built with the old column set
  and the old reason strings. **The live ledger is not touched by this work.**
* Contracts: `contracts.__all__` grows (`Acquisition`, outcome constants); the pinned set in
  `tests/test_boundaries.py` is updated in the same commit.
* CLI output: discovery's round line gains a separate "no captions" count where captionless videos
  used to be counted as failures; `stopped_because` can now read `provider_refused`.

## What would show the candidate is better

1. Each D1–D5 probe, run against the baseline and the candidate on identical synthetic input, flips
   from the wrong result to the right one.
2. New regression tests fail when the corresponding guard is reverted (mutation/revert checks).
3. The full offline suite passes; every changed expectation in an existing test is listed and
   justified.
4. No increase in model calls on the baseline's synthetic discovery workloads (request counts
   compared), and no measurable slowdown in the ledger reads the gates make.

Items that need real-model validation (whether a literal MAYBE is common, whether planner behaviour
improves) are listed as such and not claimed.
