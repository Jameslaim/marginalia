---
description: Ingest YouTube material into the marginalia library. Use when the user gives a YouTube URL to store, or a topic to build a library about.
---

Route on the argument, then report. Do not judge, plan queries, or decide anything
yourself — all of that lives in Python where it is testable.

## Which command

If the argument is a YouTube URL or a bare 11-character video ID:

```
marg ingest <url>
```

Otherwise it is a topic:

```
marg discover "<topic>"
```

If you are unsure which it is, run `marg status <argument>`. It exits 2 with
`not a YouTube URL or video ID` when the argument is a topic.

## Reporting

Report the lines the command returns and nothing more. Never read, open, print, or
summarise a transcript file, and never write into the library directory yourself —
a `PreToolUse` hook blocks it.

`marg ingest` returns one line:

- `stored` — the transcript is in the library
- `skipped` — already had it; say so
- `needs-asr` — no captions, queued for transcription (not yet implemented)
- `failed` — report the reason and whether it was permanent or transient

`marg discover` returns a brief line, a line per round, and a summary. Show all of
them. The brief line matters most: it is what the run decided the topic *meant*, and
it is the first thing to check when the results look wrong.

## Flags worth offering

- `--dry-run` judges without fetching or storing anything. Offer this when the user
  is unsure the topic is specific enough, or wants to see what it would pick.
- `--max-videos N`, `--max-rounds N`, `--max-requests N` cap a run.

If the user asks why a particular video was or was not kept, the answer is in the
ledger's `decisions` table, not in anything you should re-derive.
