---
name: select_notes
version: "2"
stage: ask
note: "M7 stage 1. Names the notes to LEAVE OUT; everything unnamed is read.
  v1 asked for the keep-list and a real run looped: with a budget of 265 of 309
  it emitted 3,351 ids on one line and hit the ceiling. Naming the excluded few
  is the shorter list near the ceiling, and it puts rule 8 in the format itself
  — silence includes."
---

You are choosing which notes another model will *not* read. You are not
answering the question.

The library is slightly too large to read at once. A little has to go. Your job
is to say which — and nothing else.

## Silence includes

**Every note you do not name is read in full.** You are not building a list of
what is relevant; you are naming the few that are clearly about something else.

That direction is deliberate, because the two mistakes are not equal:

- **Leaving in a note that turns out to be irrelevant costs nothing.** The model
  reading it simply will not cite it.
- **Dropping a note that mattered is invisible.** The answer comes back
  confident and incomplete, and nothing anywhere records that the note existed.

So name only what you are *sure* about. If a note might be tangentially useful,
say nothing about it and it will be read. When you cannot tell, say nothing.

## What you are given

One line per note:

    VIDEO_ID | PUBLISHED | TITLE | what the note says the video is

The line is a thin summary of a much richer note. Judge generously: a title that
does not mention the question's subject may still be a video that covers it.

## The question

{{ question }}

## How much has to go

Name **at least {{ drop }}** of the {{ count }} notes below, and **no more than
{{ most }}**. Fewer than {{ drop }} and the rest are dropped for you, by
position rather than by judgement — which is worse than your choosing.

## The notes

{{ summaries }}

## Output

Exactly this format, on two lines. No preamble, no commentary, no repetition.

```
DROPPED: dQw4w9WgXcQ, jNQXAC9IVRw, 9bZkp7q19f0
WHY: one sentence on what class of note you left out
```

- `DROPPED` — video IDs, comma separated, **each one once**. Copy them exactly;
  ids are case-sensitive, so `dQw4w9WgXcQ` and `dqw4w9wgxcq` are different
  videos.
- `WHY` — one sentence naming the *class* of note you dropped, not a list. It is
  stored and read back when an answer looks thin, to tell "it did not know" from
  "it did not look".
