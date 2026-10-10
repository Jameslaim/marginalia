# Independent review R2 — notes, answers, prompts, quote checking

Reviewer: subagent (general-purpose), read-only, run 2026-10-10 against `56d04fa` (the files in its
scope were confirmed identical to HEAD with `git diff --quiet`). Its harness refused report files; the
coordinator saved this text from the reviewer's final message. Probes and raw outputs:
`scratchpad/reviews/answers/probe_quotes.py/.out`, `probe_answers.py/.out`, `probe_notes.py/.out`,
`probe_record.py/.out`, `probe_answers_lib.py` — temp libraries, scripted fake clients, no network.
The reviewer ran `test_ask`, `test_note`, `test_answers`, `test_selector`, `test_timestamps`,
`test_llm`: 334 passed.

Reconciliation against source and executable evidence is in `../ARCHITECTURE-REVIEW.md` §4.

## 1. Coverage
Read fully: RULES.md; ask.py, quotes.py, enrich/note.py, enrich/timestamps.py, brain/selector.py,
llm.py, core/library.py; prompts/answer.md, select_notes.md, write_note.md; skills/ask/SKILL.md.
Read partly: contracts.py (HEAD 1440–1860: note runs, answer types, selection, grounding);
core/ledger.py (in_library, current_verdicts, wanted_for_a_note, list_by_status, mark_noted,
videos_needing_note, note_counts, touch_accessed); cli.py (main error handling, _cmd_note reporting,
_cmd_ask, _ask_summary, _save_answer); config.py (ask/note defaults). Tests: names in test_answers,
test_selector, test_timestamps; prompt/render tests in test_llm; helper sections of conftest, test_ask,
test_note; Verbatim tolerance tests. Prior audits: REVIEW §1, A1–A5, A12–A13, C8–C22, Q2, Q6, §6–7, E/F;
TECHNICAL-MAP §8.1–8.2, §10–12; AUDIT fully. Not read: discover, providers, judge, planner, territory.

## 2. Findings (most severe first)

**F1 — S1. The default note drain jams permanently behind deterministically failing videos.**
`enrich/note.py:371-413, 530-551`; `core/ledger.py:916-972` (`videos_needing_note`), `:720-730`
(`ORDER BY first_seen, video_id`). A FAILED note (truncated reply, invalid note) records nothing in the
ledger; if the first three queued videos fail the same way each time, every `marg note` spends three
whole-transcript calls on them, stops with `repeated_failures`, and never reaches video four. `--limit`
counts attempts, so it does not help. Verified by probe: two runs over 5 videos, first 3 scripted to fail
→ both `1:failed, 2:failed, 3:failed | stopped=repeated_failures requests=3`; nothing noted after both.
The comment at `note.py:315-320` fixed exactly this jam for SKIPPED and not for FAILED. Fix: record
failures (additive `note_failures`/`note_failed_at` + `mark_note_failed`), order `(previously failed,
first_seen)`. Risk: additive migration; rule 24 unaffected (a refusal still records nothing).

**F2 — S1. A line starting with `#` ends whatever note section it is in (REVIEW C20, open and wider).**
`note.py:144-150` (`missing_sections`), `:251-255` (`check_verbatim`); `quotes.py:50-51, 89-96`
(`verbatim_of`); `timestamps.py:400-404`. A Verbatim section opening with a shell comment (`# install the
dependencies`) is "empty" → FAILED invalid every run (feeds F1). A `#` line mid-Verbatim stops the quote
check (a fabricated quote after it → `(checked, unfound) = (1, 0)`). The two section parsers disagree:
`### Verbatim`, `## Verbatim:`, `## **Verbatim**`, `## verbatim` pass validation but `verbatim_of`
returns nothing (answer-side fallback has no Verbatim for that note); a `#####` line ends the section for
note.py but not verbatim_of; `_CLAIMS_RE` (`ask.py:1060`) has the same exact-heading issue; a Claims
section whose first line starts `#1` is reported missing. Verified by probe. Fix: one section splitter
(in quotes.py) that recognises only the known `## <Section>` headings (normalised) and skips fenced code,
used by validation, the Verbatim check, `verbatim_of`, `summarise`, and the snapper.

**F3 — S1. An `ESCALATE` line with a Unicode dash is printed as the answer, exit 0.** `ask.py:156`,
`:638-680`, `:1463`. The code itself records that the model writes U+2011 inside ids. `ESCALATE:
‑YTupW6YG1k: …` matches neither the escalation parser nor the escalation-only check, so the line is
printed and saved as the answer. Verified: `calls=1 failed=False text='ESCALATE: ‑YTupW6YG1k: need the
exact wording'`. A mis-cased id is treated as failed rather than repaired — the opposite of citations.
Fix: `normalise_dashes` before both regexes; resolve ids via `_find_id`; treat a reply whose every
non-blank line starts with ESCALATE as an escalation.

**F4 — S1. The loose matcher's scores depend on quote length.** `quotes.py:141` (window is always quote
+ 4 words); `note.py:222-226, 267` (`_VERBATIM_TOLERANCE = 0.8`); `ask.py:409, 416` (0.6 / 0.5). An exact
n-word quote scores at most 2n/(2n+4): 0.60 at 3, 0.75 at 6, 0.80 at 8. A one-word caption fix passes
0.8 only at ≥13 words; at 6–12 it is "not found" and answer.md then forbids quoting that line. Verified:
ceiling `n=3 0.600 … n=8 0.800 … n=13 0.867`; one mid-transcript fix `n=6..12 unfound=1`, `n=13
unfound=0`. `tests/test_note.py:772` passes only because its quote is the transcript's last 14 words
(window clipped). Fix: score by coverage (matched quote words ÷ quote length) or best of window lengths
quote±2; recalibrate 0.6/0.5/0.8 against the archived corpus. Risk: moves measured thresholds — corpus
re-score required.

**F5 — S2. Reversing a quote's meaning still scores "loosely quoted".** `quotes.py:118-143`;
`ask.py:551-557, 703-704, 964-967`; `note.py:267`. Transcript "always invalidated", a 20-word quote with
"never" or an inserted "not" → 0.85 / 0.857 → `[loosely quoted]` in an answer ("the source holds the
passage in almost these words"); in Verbatim it passes 0.8 unmarked. Verified. Fix: never grant loose
when quote and window differ by a negation/polarity token.

**F6 — S2. One long or stray `"` flips quote pairing for the rest of an answer.** `quotes.py:36` (one
regex, 4–300 chars, straight `"` as both open and close); `ask.py:524, 739`; `note.py:259`. A >300-char
quote or a stray inch mark makes the next real quote unchecked, and the text between quotes is checked
and marked `[unverified]`. Verified: `checked=1 verified=0 phrases=['[dQw4w9WgXcQ 00:00] and later']`; a
>300-char Verbatim quote is neither checked nor counted (`(0, 0)`). Fix: small scanner pairing quotes,
curly quotes directional; count over-long quotes as "unchecked (too long)".

**F7 — S2. Whole classes of quotes are never checked; some are never counted.** `quotes.py:36`;
`ask.py:524-533, 709-747`; `cli.py:1063-1080`; `skills/ask/SKILL.md`. A quote with no citation within
200 chars gets no inline marker and the stdout grounding line omits uncited quotes; quotes in `«»`,
`「」`, `‘’`, `„“` are invisible (not checked, not counted, no Grounding section) — and answer.md asks the
model to answer in the question's language; quotes under 8 normalised chars are silently skipped.
Verified: uncited fabricated quote → `checked=0 uncited=1 inline marker added=False`; guillemets / curly
single / CJK brackets → `checked=0 uncited=0`; summary prints `grounding: 0 of 0 … 0 not found` when an
uncited quote is the only failure. Fix: directional pairs; inline-mark uncited; print uncited count;
correct the skill's claim.

**F8 — S2. A quote verifies if any nearby cited video holds it; timestamps are never checked.**
`ask.py:529-543`. A quote spoken only in B but attributed to A is verified when B's citation is within
200 chars; a correct quote with a wrong timestamp (`[A 59:00]` for a 00:04 line) is verified and the
link goes to the wrong moment; unquoted factual claims are unchecked. Verified. Fix: attribute to the
immediately following citation, count "found only in another cited video" apart; add a ±N-second check
of the cited moment as its own counter (REVIEW Q2 #2 still unimplemented).

**F9 — S2. The context guard never sees opened transcripts or the output allowance.**
`ask.py:1385-1389, 1399-1440, 1593-1608`; `config.py:347-355` claims the 0.75 budget share "leaves room"
but nothing computes it. Escalation or `--deep` can push the second request past the provider limit — it
fails after the first full-library call has been paid for — and the record/stdout show the smaller
first-call estimate. Verified: recorded `estimated=12,229 headroom=20%`, second request ≈72,281 tokens
(120% of the ceiling) plus `max_tokens=32,768`. Fix: estimate the whole request before every call; cap
transcripts to remaining room and disclose the cut.

**F10 — S2. Capped transcripts are cut mid-line, undisclosed (AUDIT new #3, open).**
`ask.py:1593-1608` (`text[:ceiling]`), `:1319-1325`, `:1001-1013`. Verified: 2,073 of 25,090 chars sent,
no cut notice in the block, last line `'[00:00:32.000 --> 00:00:33'`; the answer record says only
`- dQw4w9WgXcQ — wording`. The model can say "he never says X" about the half it was not shown. Fix: cut at
a line boundary; the block header states given/total and that the rest was not given; record "cut at X
of Y" in the file and on stdout.

**F11 — S2. `--deep` disables the model's own escalation and tells it the wrong thing.**
`ask.py:1405-1406` (comment says it "never prevents" escalation), `:1424` (`allow=not opened`),
`:260-264`; `answer.md:76, 142`. The model is told "not allowed — you already have the transcripts you
asked for" though it asked for nothing, and cannot open a second video. Verified. Fix: allow escalation
while fewer than `max_transcripts` are open; separate wording for user-forced transcripts.

**F12 — S2. The answer record says "the notes were enough" when requested transcripts were not
opened.** `ask.py:1001-1013, 1429-1433, 664-669`. Model escalates for a video with no transcript → file
says `(none — the notes were enough)`; over-limit and unknown-id requests leave no trace. Verified:
`calls: 2 | record says: (none — the notes were enough)`. Fix: record each request with its outcome.

**F13 — S2. Selection: overflow still trimmed in id order; under-dropping replies count as success
(REVIEW C8, open).** `ask.py:1212-1214`; `selector.py:43, 119-133, 263-268`. Overflow always falls on the
ids that sort last; `DROPPED:` with one id per line drops only the first id (regex consumes the newline)
with `parsed=True, problem=''`; `DROPPED: none` is accepted as parsed, and the "asked to drop" line is
omitted from the record — both produce the order-based fill that `SelectionFailed` exists to refuse, the
only trace being "N over budget". Verified: `trimmed: ['Lxxxxxxxxx1' … 'Pxxxxxxxxx1'] overflowed: 5`;
`parsed=True kept=[A, C] requested=1 problem=''`; `'none': parsed=True kept=3`. Fix: trim within batches
or shuffle before trimming; read ids from DROPPED through WHY; flag batches far under quota; size quotas
in tokens.

**F14 — S2, possibly intentional. A truncated answer exits 0.** `cli.py:994, 1035-1037`. The fragment is
printed as the answer; the only warning goes to stderr after it; the skill treats exit 0 as an answer.
The saved file carries a "fragment" banner. Verified (`truncated=True failed=False`). Fix: distinct exit
code and skill wording, or record the intent in RULES.

**F15 — S2. A Verbatim line marked "not found" still verifies an answer quote.** `ask.py:483-484,
544-548`: with no transcript on disk the check falls back to Verbatim, and a line the note worker marked
`[note: not found in the transcript as worded]` still verifies. Verified: `verified=1 unchecked=1`. Fix:
exclude marked lines.

**F16 — S2 (S1 for affected languages). Non-space scripts, combining marks, normalisation.**
`quotes.py:47-48, 75-81, 112, 132`. CJK: a caption line is one "word", so loose matching is effectively
absent — one-character difference scores 0.000 (English analogue 0.727); a correct CJK Verbatim quote
with one caption fix is "not found"; exact matching works. Devanagari/Arabic diacritics: Hindi splits at
every vowel sign and exact matching reduces to consonant skeletons (too permissive); vocalised Arabic
tokenises into 22 fragments vs 6 words. NFC vs NFD fails the exact test (Spanish, Vietnamese). CJK
Verbatim lines never get timestamps aligned, while `answer.md:102` says they were. Verified. Fix:
normalise both sides (NFKC), drop combining marks consistently, per-character tokens for scripts without
spaces, calibrate separately.

**F17 — S3. False or overstated prompt statements.** `answer.md:123` "at most three" (should render
`max_transcripts`; extra requests dropped silently, F12); `answer.md:102` "aligned by code to the
transcript line" (not for unsnapped lines, CJK, older notes); unconditional "the library does not cover
this is a good answer" vs the selected-mode scope sentence ("You have all of them" itself is fixed in
v7); "TRANSCRIPTS YOU ASKED FOR" also covers user-forced transcripts; `select_notes.md:49` "what the note
says the video is" is the first Claims bullet (REVIEW A12, open).

**F18 — S3. Verification claims made to users exceed the checks.** `skills/ask/SKILL.md` "every quote
in the answer is checked against the transcript" and "a marker beside any quote the cited transcript does
not hold" (F6–F8 contradict); `cli.py:936` `--json` `"answer"` is unmarked text (REVIEW C19 partly fixed).

**F19 — S3. Stale docs in scope.** `contracts.py` HEAD:1629 `fallback` = "selector failed and the budget
was filled" (now raises `SelectionFailed`); `ask.py:1306-1313` `render_notes` docstring claims discovery
uses it (no other caller); `ask.py:1405-1406` `--deep` comment (F11); `note.py:222-226` tolerance claim
(F4).

**F20 — S3, inferred. The selector's WHY may carry note text into `answers/`.** `ask.py:857`;
`selector.py:135-136`: the model-written WHY (≤300 chars, produced from note summaries) is written to the
answer file, whose other contents are ids and digests — an uncovered rule-1 surface. Fix: state in RULES
that it is allowed, or cap and strip quoted text.

**F21 — S4.** Punctuation/space removal erases word boundaries ("in the loop" found in "within theloop");
nested straight `"` mis-pairs; `mark_unverified` marks every occurrence of a string quoted twice; a
missing template placeholder is reported as "The model call failed" in ask and "transient" in the note
drain; provider error `code` echoed (bounded to 64 identifier chars `[A-Za-z0-9_.:-]`; one message shows a
doubled quote `'m''s`); a corrupt note/transcript (decode error / OSError) crashes every answer or ends
the drain; two concurrent `marg ask` runs of the same question on the same day can lose one record (same
`.tmp`, `library.py:280-285, 336-339`).

**Checked and sound:** prompt rendering (`{{ x }}`, `\1`, `\g<0>` in notes and questions arrive verbatim;
pinned `test_llm.py:1049-1072`); error safety (no provider body reached a printed/saved message for 400,
402, 418, 500, a context 400, or an unknown exception — §12.11 fixed); note truncation handled
(`note.py:530-541`) apart from F1's jam; write ordering (file before `mark_noted`); grounding reads the
full stored transcript, not the capped one.

## 3. Status of prior findings
| id | status | evidence |
|---|---|---|
| REVIEW A1 | Already-fixed | write_note v8 states no count; pinned |
| A2 | Already-fixed | answer.md v6 + grounding reads transcripts |
| A3 | Confirmed-still-relevant (effect inferred) | question appears once, at top |
| A4 | Confirmed-still-relevant | answer.md never mentions "How current is it" |
| A12 (select_notes; "at most three") | Confirmed-still-relevant | F17 |
| C8 | Confirmed-still-relevant | probe K / F13 |
| C11 | Already-fixed | `note.py:530` |
| C13 (selector row) | Already-fixed | contracts sibling stale (F19) |
| C16 | Already-fixed | `LlmRequestTooLarge` |
| C19 | Partly fixed | JSON `answer` unmarked |
| C20 | Confirmed-still-relevant, worse | F2 |
| C21 | Already-fixed | — |
| C22 | Fixed, with defects | F4, F5, F16 |
| Q2 #2 | Confirmed-still-relevant | no timestamp check in ask |
| Q6 (2) | Intentional, fragile | F2 |
| TECH-MAP §10 ask truncated answer | Record fixed; exit code open | F14 |
| §10 note row | Already-fixed | — |
| §12.11 | Already-fixed | probe J |
| §12.13 | Already-fixed | — |
| §12.14 ask.py docstring | Already-fixed | — |
| §12.14 render_notes docstring | Confirmed-still-relevant | F19 |
| §12.14 "You have all of them" | Already-fixed | v7 `library_scope` |
| §12.16 | Already-fixed | — |
| §12.17 | Already-fixed | previews no longer printed |
| AUDIT new #2 | Already-fixed (sibling is F11) | — |
| AUDIT new #3 | Confirmed-still-relevant | F10 |
| AUDIT "quotes.py has no direct tests" | Superseded | `test_ask.py:1600-1660` |

## 4. Architectural observations (reviewer's)
- **Whole-note selection: keep.** Batched selection over summary lines answers a measured limit, and
  refusing on total failure is right. Fix the edges: note-count quotas vs token budget, id-order overflow
  trimming, degenerate replies accepted, summary depending on exact heading spelling.
- **Whole-transcript processing:** sound for notes (length/window skips correct; the weak spot is that
  failures are not persisted, F1). For answers, transcripts are added on top of a notes-only guard (F9)
  and cut blindly (F10) — one budget function used before every model call would fix both.
- **Quote verification: right idea, wrong shape.** Checking against the transcript, reporting only
  found/closeness, and calibrating against chance are right. But the +4-word window makes every threshold
  length-dependent (F4); tokenisation is wrong for unspaced scripts/combining marks and nothing is
  normalised (F16); one undirected regex finds quotes, so a stray mark changes what is checked and common
  quote styles are invisible (F6/F7); attribution is "any citation within 200 chars" (F8). No new
  dependency needed, but each fix changes the scorer and the measured 0.6/0.5 thresholds must be
  re-measured. Unquoted claims stay correctly recorded as a known limit (rule 24); a cited-moment check
  would narrow it cheaply.
- **`ask.py` (1,615 lines): refactor along its seams, do not rewrite.** Five jobs (citation parsing,
  grounding, selection/budget, the record, the two-call flow); pure parts are tested; bugs cluster at the
  seams — escalation parser lacks the citation parser's id repair (F3); the guard does not see what the
  flow opens (F9); the record does not see failed escalations (F12); answer and note checks parse sections
  differently (F2). Extracting one id resolver, quotes+sections, one budget estimator and the record makes
  the shared parts explicit. `llm.py` is in good shape.
