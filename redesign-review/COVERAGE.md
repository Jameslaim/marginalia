# Coverage checklist — what was read, by whom, how deeply

Baseline `56d04fa`. **C** = coordinator (this session). **R1** = independent reviewer,
discovery/planning/providers. **R2** = independent reviewer, notes/answers/prompts/quotes.
**R3** = reviewer for tests/recovery/diagnostics/config/CLI — **terminated early by a rate
limit and produced no report**; its probe outputs and mutation results were recovered from
its scratch folder and re-checked by C, and are cited as *C-verified R3 probe data*, never
as an independent review.

Depth: **F** read fully · **P** read in part (sections named) · **G** grep/structure only ·
**X** exercised (tests/probes run against it) · **—** not opened.

## Production source (`src/marginalia/`)

| file | C | R1 | R2 | R3 data | notes |
|---|---|---|---|---|---|
| `flow/discover.py` | F, X | F | — | X | rewritten in parts (commits 2, 3, 4, 5, 10) |
| `flow/search.py` | P (gates, chain, headroom), X | F | — | — | Gate 2 changed |
| `flow/store.py` | F, X | F | — | X | reconcile meta fix |
| `flow/refetch.py` | F, X | F | — | X | moved onto acquire |
| `flow/acquire.py` | new | — | — | — | written in commit 3 |
| `flow/doctor.py` | G | — | — | X | read-only open fixed via ledger |
| `flow/export.py` | P (`_decisions_in`, `_round_boundaries`) | — | — | X | unchanged |
| `core/ledger.py` | P (schema, migrations, row types, gates, verdict policy, note queue, refetch queue, quota, read-only) , X | P | P | X | — |
| `core/library.py` | P (meta, transcript paths) | — | F | — | unchanged |
| `core/ids.py` | — | — | — | — | not reviewed this pass (prior audit read it; unchanged) |
| `brain/judge.py` | P (parse_full, judge calls, brief, batch), X | F | — | X | — |
| `brain/territory.py` | P (call site, catch-all) | F | — | — | refusal re-raise only |
| `brain/planner.py` | P (render_round, call sites) | F | — | — | counters/headings |
| `brain/selector.py` | P (parse_selection) | — | F | — | DROPPED parse |
| `llm.py` | F | P | F | — | unchanged; error-safety reviewed |
| `ask.py` | P (escalation, choose_notes, answer_question, record, transcripts, grounding inputs) | — | F | — | — |
| `quotes.py` | F | — | F | — | splitter + guards added |
| `enrich/note.py` | P (sections, Verbatim check, select, drain, `_one`) | P | F | X | — |
| `enrich/timestamps.py` | P (`snap_text`) | — | F | — | splitter only |
| `contracts.py` | P (verdicts, JudgedVideo, FullVerdict, RoundReport, DiscoverRun, VideoRow, Answer, Opened) | P | P | — | ~2,100 lines; not read end to end by anyone |
| `config.py` | G | — | P | X (S11) | unchanged |
| `cli.py` | P (main, ingest, discover report/json, note, ask output, refetch, llm-check) | P | P | X | — |
| `providers/__init__.py`, `base.py` | G | F | — | — | — |
| `providers/ytdlp.py` | P (fetch, track selection) | F | — | — | candidate fallback |
| `providers/youtube_api.py` | P (`_classify`, reasons) | F | — | — | rate-limit classification |
| `providers/ytdlp_search.py`, `chapters.py` | — | F | — | — | no defect found by R1 |

## Prompts (`prompts/`, recursively)

| prompt | C | R1 | R2 | changed? |
|---|---|---|---|---|
| `judge/full.md` | G | P (contract/format) | — | no |
| `judge_metadata.md` | — | F | — | no |
| `territory/map.md` | — | P | — | no |
| `plan/round.md`, `plan/baseline.md` | — | P | — | no |
| `write_brief.md`, `review_brief.md`, `expand_queries.md` | — | P / F | — | no |
| `write_note.md`, `answer.md`, `select_notes.md` | G | — | F | no |

**No prompt file was changed.** The planner's *rendered input* (`render_round`) and the
answer call's `{{ escalation }}` / transcript-header strings changed; their effect on model
behaviour is unverified without real-model runs.

## Tests, docs, tooling

| item | coverage |
|---|---|
| `tests/` (33 files at baseline) | C: every file touched by a change read in the relevant part; R2 ran six answer-side files; R3 ran the whole suite on mutants (25 mutations). No reviewer did a full quality pass of every test file. |
| `tests/fixtures/` | not opened |
| `RULES.md` | C: F. Edited where a rule's text changed (rule 8, rule 13, rule 24, Structure). |
| `PLAN.md` (2,743 lines) | — not read this pass; not edited |
| `README.md`, `config.example.toml`, `CLAUDE.md` | G / — ; not edited |
| `bin/marg`, `bin/marg.cmd`, `hooks/hooks.json`, `skills/*` | — this pass (prior audit read them); not edited. `skills/ask/SKILL.md` claims are listed as stale in R2 F18. |
| Prior audits (`REVIEW.md`, `TECHNICAL-MAP.md`, `AUDIT-2026-10-10-coverage.md`) | C: AUDIT F; TECHNICAL-MAP F; REVIEW §1–§7 F, §8 appendix (prompt rewrites) skimmed. R1/R2 read the parts for their areas. |
| `C:\youtube-agent\baseline\` | listed only |
| Live library, archive, home config | **never opened** (blocked by the isolation guard for every test and probe run) |

## What this means

The whole production path for discovery, acquisition, ledger state, notes and answering was
read and exercised. Thinner: `config.py`, `doctor.py`, `export.py`, `core/ids.py`,
`ytdlp_search.py`, `chapters.py` and the prompts were read by a reviewer or in part, not by
the coordinator end to end; none of them was changed except as noted. `contracts.py` was
read only in the structures that changed or that a change depends on. Claims in
`ARCHITECTURE-REVIEW.md` are limited accordingly.
