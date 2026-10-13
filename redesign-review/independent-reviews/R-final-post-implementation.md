# Independent post-implementation review — the candidate diff

Reviewer: a fresh read-only subagent that did not write the code, run after implementation
against `56d04fa..ca91a15` (it started at `f2a512e`; the extra commit touched only RULES.md and
COVERAGE.md). Probes under `scratchpad/reviews/final/` (`p1`–`p6`, `test_p5_deep_reescalate.py`);
it ran the 13 changed test files (531 passed). It modified no project file. Its harness refused
report files; this is a faithful condensation of its final message, with every finding kept.

**Reviewer's verdict:** has blocking defects (one), otherwise the architecture and commits hold up.

## Findings and what was done

| id | sev | finding (reviewer's evidence) | coordinator's check | resolution in `3d019cf` |
|---|---|---|---|---|
| F1 | S1 blocking | Polarity guard: `_POLARITY` holds `t`, `not`, `cannot`; a contraction swap ("don't"/"do not", "can't"/"cannot", "won't"/"will not") differs by a negation token, so every non-exact window was skipped and the score fell to 0.0 (baseline 0.81–0.83). `check_verbatim` marked a faithful Verbatim line unfound. Probe p3/p3b. | Reproduced (0.0 vs 0.667/0.714 on baseline). | Compare the *count* of negations on each side of the differences. Contractions score as on baseline; reversals and an inserted "not" still refused. Three contraction tests added. |
| F2 | S2 | Discovery resets the overflow streak per stage-2 call; a config whose window fits no transcript stores every candidate `too_large`, which sticks per model. Rule 24 not updated. By reading. | Confirmed by reading. | Run-level stop: 5 stage-2 overflows in a row with nothing fitting → `context_window`. Rule 24 amended. Test added. The per-model stickiness remains an owner decision. |
| F3 | S2 | `--deep` + re-escalation of the same id opened it twice (probe test_p5). | Confirmed by reading. | Refused as "already opened"; test added. |
| F4 | S3 | Splitter rejected notes the old validator accepted: unclosed fence, duplicated section with empty first copy, heading indented 4 spaces; ```` ```cmd``` ```` toggled the fence. Probe p4. | Confirmed. | All four accepted again; tests added. |
| F5 | S3 | A drifted ledger (missing a core column) was still altered before refusal. Probe p2. | Confirmed. | Full read-only schema check before any write; test added. |
| F6 | S3 | `note_refusal` claimed to be shared with the worker; the worker kept its own checks. | Confirmed. | Worker decides length limits through the same helper. |
| F7 | S3 | Failed notes ordered by `first_seen` within the failed group; the same three return once untried run out. | Confirmed by reading. | Sort by (failures, oldest failure); test added. |
| F8 | S3 | Gate 2 exemption read row status; the partition reads the disk. | Confirmed by reading. | Gate 2 takes the disk check from discovery; test added. |
| F9 | S3 | Held + already-judged not carried on the run; recounted every round. | Confirmed by reading. | Added to `read_from_disk`; test added. |
| F10 | S3 | Selector read every id up to WHY, including ids in prose. | Confirmed by reading. | Continuation lines must be list lines; test added. |
| F11 | S3 | The refusal-policy scan is lexical; an `_ALLOWED` entry was dead; RULES overclaimed. | Confirmed. | Limit stated in the test and RULES; dead entry removed. |
| F12 | S3 low | Narrowing the daily-quota pattern is inferred from documentation, not observed. | Agreed. | Left as is, flagged in the report as inferred. |

**Follow-up (second independent review, uncommitted corrections; see ARCHITECTURE-REVIEW
§4b).** Three of the resolutions above had boundary gaps: F1's whole-quote count let a
negation move between clauses (now counted per changed span); F2's stop missed a fifth
overflow that ended its round (now also checked after the loop); F4's inline-code fix stopped
recognising four-backtick fences, and the fence state ignored which character opened it (one
open/close policy now). F3's duplicate check ran after the capacity cut (now before), and
F8's disk check reached discovery but not `marg search` (now both). F5's pre-write check
skipped every absent table (now only tables later than M2).

## Reviewer's "checked and found correct"
Migration and read-only opens (ALTER with NOT NULL DEFAULT on an existing table, unmigrated
ledgers read byte-identical, baseline can reopen a candidate-migrated ledger); every historical
reason string in `legacy_outcome`; Gate 2 never stricter than baseline; no double judging
within a run; stage-2 NO still rejects; NO-on-unreadable → MAYBE + DEGRADED with note
suppressed; `acquire` ordering, provenance and outcome handling; the stale-`pending` age floor;
rule 1 (no new surface carries transcript or note text); `ask.py` budget arithmetic and cut;
`_find_id` repair cannot point at a different id; `find_quotes` offsets; NFKC on both sides;
refusal re-raises; `STOP_REFUSED`; note exit code; the justified test-expectation changes.
