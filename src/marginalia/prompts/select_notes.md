---
name: select_notes
version: "4"
stage: ask
note: "M7 stage 1. v4 is called once per batch of sixty notes rather than once
  over the whole library, so the numbers below are a slice. Five measurements
  across four framings showed the model has never handled a list of three
  hundred: a 3,351-id loop, 297 of 309 dropped, two truncations at 318, and
  quotas ignored at 409 and 371 of 422. v3's quota wording is unchanged; what
  changed is the size of the task it is asked about."
---

You are ranking notes so that another model can read as many of them as fit. You
are not answering the question.

## The task

Below are {{ count }} notes — one slice of a larger library, and the only slice
you are deciding about. Room for {{ keep }} of them, so **exactly {{ drop }} have
to go.** Name those {{ drop }}, and every note you do not name is read in full.

Judge these {{ count }} against each other. Do not hold back drops because a note
seems better than something you imagine is elsewhere; the rest of the library is
being ranked the same way, separately.

The question is not *which notes are irrelevant*. It is **which {{ drop }}
contribute least** to answering this one. Those are different questions and the
second is the one that matters: a note on a neighbouring subject often carries
the comparison, the counter-example, or the mistake that makes an answer worth
reading, and dropping it because its topic differs is how an answer gets narrower
without getting wronger.

So rank, do not filter. Even if far more than {{ drop }} notes look unrelated,
name only the {{ drop }} that would cost the least — and if fewer than {{ drop }}
look unrelated, you must still name {{ drop }}, choosing the weakest of the
plausible ones.

## Silence includes

Every note you do not name is read. That is deliberate: leaving in a note that
turns out to be useless costs nothing, because the model reading it simply will
not cite it, while dropping a note that mattered is invisible — the answer comes
back confident and incomplete and nothing records what was missing.

## What you are given

One line per note:

    VIDEO_ID | PUBLISHED | TITLE | what the note says the video is

The line is a thin summary of a much richer note. Judge generously: a title that
does not mention the question's subject may still be a video that covers it.

## The question

{{ question }}

## The notes

{{ summaries }}

## Output

Exactly this format, on two lines. No preamble, no commentary, no repetition.

```
DROPPED: dQw4w9WgXcQ, jNQXAC9IVRw, 9bZkp7q19f0
WHY: one sentence on what you ranked lowest and why
```

- `DROPPED` — exactly {{ drop }} video IDs, comma separated, **each one once**.
  Copy them exactly; ids are case-sensitive, so `dQw4w9WgXcQ` and `dqw4w9wgxcq`
  are different videos.
- `WHY` — one sentence. It is stored and read back when an answer looks thin, to
  tell "it did not know" from "it did not look". Say what you ranked lowest, not
  a list of ids.
