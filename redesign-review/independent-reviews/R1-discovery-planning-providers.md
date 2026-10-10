# Independent review R1 — discovery, planning, providers

Reviewer: subagent (general-purpose), read-only, run 2026-10-10 against `56d04fa`.
The reviewer's harness refused report files, so the coordinator saved this text verbatim from the
reviewer's final message. Probes: `scratchpad/reviews/discovery/probe_discover.py` (P1–P8),
`probe_answerable.py` (P9), `probe_providers.py` (P10–P12), run against a `git archive` export of
`56d04fa` with the import path verified. No pytest was run by this reviewer. Line numbers are at
`56d04fa`.

Reconciliation against source and executable evidence is in `../ARCHITECTURE-REVIEW.md` §4; agreement
between this reviewer and the coordinator is not treated as proof.

## 1. Coverage
Read fully: flow/discover.py, flow/search.py, flow/refetch.py, flow/store.py, brain/judge.py,
brain/territory.py, brain/planner.py, providers/__init__.py, base.py, ytdlp.py, youtube_api.py,
ytdlp_search.py, chapters.py; prompts/expand_queries.md, judge_metadata.md.
Read partly: contracts.py (260–980 filters/territory/planning/verdict constants; FullVerdict,
RoundReport, stop constants); core/ledger.py (quota_day, in_library, skip_reason, current_verdicts,
wanted_for_a_note, all mark_*, videos_to_refetch, latest_decision, decisions_for, quota methods);
llm.py (error classes, complete() overflow streak, forget_overflows); cli.py (_cmd_ingest, _fetch);
enrich/note.py (floor/ceiling 425–490); judge/full.md, plan/round.md, plan/baseline.md,
territory/map.md (contract/format/revision/bias sections); write_brief.md, review_brief.md
(placeholders only); tests (test_discover.py fakes and test list, held/unjudged/too-large tests;
greps of judge/planner/territory tests); prior audits: REVIEW §1, A5–A11, B3–B4, C1–C21, Q1–Q8, §7;
TECHNICAL-MAP §10–§12; AUDIT-2026-10-10 fully.

## 2. Findings (most severe first)

**F1 — S1. A held transcript is re-gated by stage 1, and a title-based NO then permanently blocks
stage 2.** `discover.py:779-823` sends every candidate through `judge_metadata` and keeps only
YES/MAYBE; the held/judged check is after it (`:846-853`). Gate 2 treats any `no` row (any stage)
as final (`search.py:191-193`). Scenario: run 1 stores a video and its stage-2 call fails (recorded
`full/maybe`, unjudged — rule 8 promises "the next run judges it again"); run 2's stage 1 says NO from
the title, so stage 2 never reads the transcript on disk; run 3 Gate 2 drops it as REJECTED. Same for
transcripts held from another topic, which rule 13 says "reach it too". Verified by probe P1: run 2
`stage-2 calls: [] rejected: [('aaaaaaaaaaa','metadata')]`; run 3 `rejected_before 1, stage-2 calls []`.
Fix: partition candidates before stage 1 — held & judged-for-topic → skip; held & unjudged → stage 2
from disk with no stage-1 call; else stage 1. Also let Gate 2 disregard a `metadata`-stage NO when the
transcript is held so existing ledgers recover. Risk: a few extra stage-2 calls; tests counting
stage-1 calls on held videos change.

**F2 — S1. Caption track choice can permanently store a chat replay, a machine translation, or a
foreign manual track (REVIEW C9).** `ytdlp.py:210-259` `_select_track`/`_pick_language`; a chosen
track parsing to zero lines goes to `needs_asr` with no fallback (`:150-155`). Verified (selection,
P10; yt-dlp `live_chat` shape inferred): `live_chat`+`en` auto → `live_chat` (→ 0 lines → needs_asr,
which has no worker); no hint with `hi-orig`+`en` translation → `en` (the machine translation); English
video with only an `es` manual track → `es` beats English auto; no hint, manual `ar`+`fr` → `ar`
(alphabetical). Rule 3 makes the stored transcript immutable; RULES 26 measured 63% Verbatim mismatch
in non-English notes, "mostly translations". Fix: skip non-language keys; prefer `*-orig` in the auto
table before English preferences when no hint; prefer an auto track in the hint language over a manual
track in another; on a zero-line parse retry the next track. Tests: none for `_select_track`.

**F3 — S2. The planner counts videos that can never be noted as coverage.** `answerable` checks only
this verdict's DEGRADED and `too_long` (`contracts.py:757-772`, `discover.py:1040-1041`); the note
worker also refuses a stage-2 DEGRADED on any topic (`ledger.py:661-666`) and transcripts below
`min_transcript_chars` (`note.py:441-473`). Verified P9: DEGRADED-for-another-topic → `covers=True`
while `wanted_for_a_note=False`; a 48-char transcript (floor 1,200) → `covers=True`. Same class as the
fixed §12.2; siblings missed (rule 25). Fix: one shared `note_possible()` used by discovery and the note
worker.

**F4 — S2. A stage-2 MAYBE is resolved to a judged YES and counted as coverage.** `judge.py:421-436`;
P7: `verdict=yes judged=True`, `covers=True`. Related: a NO whose reason blames an unreadable transcript
becomes YES+DEGRADED and is stored as `full/yes` (`judge.py:475-488`) — REVIEW C1(b) still open.
(Coordinator's uncommitted edits move both to MAYBE; `test_a_stage_two_maybe_is_counted_as_unparsed_and_still_kept`
pins the old behaviour.)

**F5 — S2. Three long videos in a row end a discovery run as a "config error."** `llm.py:369-383`
raises `LlmConfigError` after 3 consecutive overflows; only the note drain calls `forget_overflows()`,
though discovery's stage-2 requests are equally independent. Verified P4: `LlmConfigError` advising
"Lower [note] max_tokens / [ask] max_tokens…" (irrelevant to discovery); third video stored with no
stage-2 row; fourth never fetched; run recorded `incomplete`. Fix: `client.forget_overflows()` before
each stage-2 call; caller-neutral message.

**F6 — S2. 402s are swallowed by mapper, baseline and planner (TECHNICAL-MAP §12.7, open).** Bare
`except Exception` at `territory.py:1078`, `planner.py:745`, `planner.py:831`; discovery records a
refusal as `incomplete` (`STOP_REFUSED` exists but only the note drain uses it). Verified P8: all three
return normally on a 402 → 4 refused calls (3 map samples + baseline), stored map/baseline say "call
failed: 402" instead of the account, then the run dies at `expand_queries`. Fix: `except
LlmConfigError: raise` before each catch-all; map to `STOP_REFUSED` in `discover()`.

**F7 — S2. "Too large for the model" is never retried while the model name is unchanged.**
`discover.py:1104-1108`. P5: same model → stays judged; other model → retried. A wider window under the
same model id, a lower `max_tokens`, or a shorter prompt never retries. The planner is also told "the
next run on this topic will judge them again" (`planner.py:591-596`), false for this class — and false
for any unjudged video `marg note` notes before the next run (Gate 1 then drops it). Fix: record input
size/`max_tokens` on the row and key stickiness to a config fingerprint; make the planner text
reason-specific.

**F8 — S2. YouTube API rate-limit errors can lock the API for the whole Pacific day.**
`youtube_api.py:69-75`, `:250-256`. Verified classification (P11; Google semantics inferred):
`userRateLimitExceeded` → `QuotaExhausted`; `rateLimitExceeded` with message "Quota exceeded … 'Queries
per minute per user'" → daily cap. Consequence: the rest of the day falls to `ytdlp_search` (loses
recency window, duration, caption flag, description). Fix: `userRateLimitExceeded` → transient; require
"per day"/"daily" for the daily cap.

**F9 — S2. Held keepers are re-judged at stage 1 every round and every run (§12.8, partly fixed).**
Only `stored_ids` is run-scoped (`discover.py:672`); keepers judged from disk are never added
(`:1052-1057`). Verified P2: round 2 re-judges the held video at stage 1 (second `metadata` row). Across
runs, every too-long/DEGRADED/too-large/not-yet-noted keeper is re-stage-1'd each run — model calls,
inflated counters the planner reads, and F1's opportunity each time. Fix: run-scoped "handled this run"
set passed to Gate 1, plus F1's partition.

**F10 — S2. `failed` absorbs no-captions (§12.6), and permanently failed videos are re-fetched.**
`discover.py:909-912`, `:1153-1156`; P3: `needs_asr, failed=1, no_captions=0`. Also verified by
reading: discovery never checks row status before fetching, so `failed/permanent` (age-restricted,
members-only still appear in search) and `needs_asr` rows are re-fetched every round and re-counted as
`failed`. Fix: `_fetch` returns an outcome; skip permanently failed rows.

**F11 — S2. Planner counters count search hits and misattribute this run's own rejections (§12.4, AUDIT
new-4, A9).** P6 (one video via 3 queries): round 2 `candidates=3, rejected_before=3`, and the planner
reads "carried a NO for this topic from an earlier run" — the NO was round 1. "stage 2: … declined"
omits `overturned`. Fix: count unique ids, split earlier-run vs this-run rejections, add `overturned`.

**F12 — S3. The planner is told filters ran that did not.** `discover.py:704-705` passes the requested
filters to the planner, which prints them (`planner.py:545-547`); after a fall-through to `ytdlp_search`
it believes `publishedAfter`/`order=date` were applied. `_log_query` fixed this for the ledger only.
Fix: pass `result.filters_applied`.

**F13 — S3. Map revisions still change no search (C5/A8/B4).** Expansion never sees the map; map
queries run in round 1 only. A revision only edits the judges' TERRITORY line — so the "free"
direction (additions) widens the suggestive context rule 15 warns about, while the guarded direction
(removals) narrows it: rule 19's asymmetry is inverted relative to what the code actually affects. Fix:
delete revisions, or feed added terms to `expand_queries`.

**F14 — S3. The quota pre-check never fires with the default chain.** `search.py:286-325`,
`discover.py:570-587`, `config.py:37`: with `ytdlp_search` in the default chain `quota_headroom` always
says "no limit", so `STOP_QUOTA` and the planner's quota line are dead; a mid-round API exhaustion runs
the rest unfiltered — the half-filtered round `STOP_QUOTA`'s comment says must never reach the planner.
Fix: report "filtered searches left" and show it to the planner.

**F15 — S3. Legacy "unjudged" rows are recognised only by today's exact reason strings.**
`discover.py:1101-1108`; P5: `opening judge failed, kept`, `verdict could not be parsed, kept`, and
stage-2 YES rows "reply broke the output contract (… no readable verdict)" all count as real verdicts
(strings from `b5fb894`…`af1fb57`). Fix: ledger outcome column + one-time backfill.

**F16 — S3. Fetch+store orchestrated three times with three provider policies (AUDIT new-5).**
discover (`transcript_order[0]` only, records that name as provenance even when an injected provider
fetched), refetch (`[0]` only; literals `"permanent"`/`"transient"`), ingest (whole chain). Rule 4's
"a new provider = one file" holds for ingest only. Fix: one `flow/acquire.py`.

**F17 — S3. Budget checks skip stage 1 and the baseline.** `discover.py:269-275`, `:779-788`,
`:367-369`: a round admitted with 1 request of headroom can run ~17 stage-1 batches past `max_requests`
/ `max_cost_usd`, then stop before the first fetch — and the stage-1 NOs it wrote are permanent Gate-2
rejections. Fix: check headroom against the stage-1 batch count first; truncate candidates rather than
overspend.

**F18 — S3, inferred. A provider 400 on one query drops it instead of widening.** `search.py:250-259`,
`discover.py:674-686`: a filter value that passes shape validation but that Google rejects → 400 →
`PermanentError` → the whole query lost, contrary to rule 16's "every rejection widens". Fix: retry once
unfiltered and record a `FilterRejection`.

**F19 — S3. Failure policy inconsistent across pre-search calls.** `write_brief`/`expand_queries`
catch nothing (a transient blip in round-2 expansion ends the run though planner queries exist);
map/baseline/planner swallow everything incl. refusals (F6); `expand_queries` has no truncation check;
`parse_queries` accepts a ≤12-word prose line as a query.

**F20 — S4.** `QuotaLedger` narrower than what `YouTubeApiProvider` calls (fake built to the Protocol
raises `AttributeError`, P12, §12.15); `search.py:268-269` `getattr` fallbacks; `planner.py:651`
hardcodes "~101 units" (provider knowledge in `brain/`); `already_held` sums two outcomes;
`STOP_NO_NEW_VIDEOS` two causes (§12.5); `STOP_MAX_COST` defined twice; `discover()` docstring
"writes nothing" on dry run (§12.10); `judge.py:785` "reading real openings"; `_log_query` records
requested filters for a failed search.

## 3. Status of prior findings
| id | status | evidence |
|---|---|---|
| C1(a) | Fixed | `judge.py:212-234` |
| C1(b) | Confirmed-still-relevant | F4 |
| C2 | Fixed | `territory.py:339-345` |
| C3 | Fixed | `recency_readable`/usability split |
| C4 | Fixed | whole-word regex `territory.py:185` |
| C5/A8/B4 | Confirmed-still-relevant | F13 |
| C6 | Confirmed-still-relevant | no final planner call (`discover.py:364-366`) |
| C7 | Fixed | `planner.py:606` |
| C9 | Confirmed (probe) | F2 |
| C10 | Fixed | stored-format length |
| C12 | Confirmed-still-relevant | — |
| C13 | Mostly fixed | `judge.py:785` remains |
| C14 | Confirmed-still-relevant | — |
| A6 | Fixed | `discover.py:1030` |
| A9 | Confirmed | F11 |
| Q4 | Mostly fixed | spoken MAYBE + NO-damage still YES at 56d04fa |
| Q8 | Not verifiable | needs a real run |
| §7 related topic empty | Intentional product choice (Gate 1 KNOWN); still relevant | — |
| §12.1 | Fixed | — |
| §12.2 | Fixed (siblings open, F3) | — |
| §12.3 | Confirmed | no caller of `current_verdicts` |
| §12.4 | Confirmed (probe) | F11 |
| §12.5 | Confirmed | — |
| §12.6 | Confirmed (probe) | F10 |
| §12.7 | Confirmed (probe) | F6 |
| §12.8 | Partly fixed | F1/F9 |
| §12.9 | Mostly fixed | draft still used (with a warning) if both bad |
| §12.10 | Confirmed | S4 |
| §12.15 | Confirmed (probe) | F20 |
| §12.18 | Incorrect (self-described non-issue) | — |
| §12.20 | Fixed | — |
| AUDIT new-4 | Confirmed | F11 |
| AUDIT new-5 | Confirmed | F16 |
| AUDIT new-6 | Partly fixed | stage-1 failure→MAYBE, stage-1 refusal re-raise, track selection still untested |
| "2de7bc5 too-large → YES" | Fixed (now MAYBE) | stickiness is F7 |

## 4. Architectural observations (reviewer's)
- Duplicated orchestration: fetch+store ×3 (F16); "can this ever be noted" decided in three places that
  disagree (F3). Refactor to one `acquire()` and one `note_possible()`.
- Ambiguous encodings: `no` in `decisions` means three things and Gate 2 reads all of them; "unjudged"
  inferred from reason text; `incomplete` = crash or refusal; `already_held`, `failed`,
  `no_new_videos` each carry two meanings.
- Inconsistent refusal/retry policy across call sites (judges re-raise; mapper/baseline/planner swallow;
  brief/expansion crash on blips; note drain forgives overflow streaks, discovery does not; search falls
  through on quota/transient but drops a query on a filter-caused 400).
- Interfaces that don't fit: `QuotaLedger`; `quota_headroom` hiding the filtered budget; requested
  filters shown as applied; `answerable` blind to cross-topic DEGRADED and the floor.
- Hidden coupling: Gate 2 does not know the bytes are held, so a stage-1 opinion overrides the stage
  that reads; map revisions are coupled only to judge prompt text.
- Keep/refactor/replace: keep `territory.py` (pure validator, widest-of-three, unit-aware parsing are
  sound); keep the planning call, replace revisions, add refusal re-raise; keep `judge.py` (finish F4,
  add stage-1 failure tests); refactor `discover._judge_round` (~300 lines) into partition → acquire →
  judge → account; keep `search.py` gates (add held-awareness, unique counts, unfiltered retry); keep
  `store.py`; keep refetch's policy but move acquisition into the shared path; keep `youtube_api`
  (fix F8); keep ytdlp's error classification, rework track selection (F2); keep `ytdlp_search`,
  `chapters` (no defects found).
