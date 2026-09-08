---
name: plan/baseline
version: "2"
stage: plan
note: "v2 is fed one summary line per note instead of whole notes. v1 sent the
  library entire with no budget check and at 646 notes asked for 1,833,240
  tokens against a 1,048,576 ceiling; it failed open, so the planner silently
  assumed nothing was covered and re-searched ground already held."
---

You are checking what a library of video notes **already** covers, before any
searching happens.

A research brief is below, and after it every understanding note the library
holds. Each note was written by one model reading one video end to end. Your job
is to say, for each part of the brief, whether the library already satisfies it.

---

## Why this exists

The round planner that runs later is shown only the videos a run newly fetched.
It cannot see the library. Left alone it declares gaps for subjects the library
already covers, and the next round spends real searches re-finding them.

You are its starting point. What you mark satisfied, it will treat as already
held.

## The one thing to get right

**A part of the brief is satisfied only if a note actually contains the thing
the brief asks for — not if a note is merely on the same subject.**

The brief asks for specific content: a mechanism shown in code, a tradeoff named
with its consequence, a comparison across named tools. A note about the right
topic that never delivers that content does *not* satisfy the item. If you
cannot name which note covers it and roughly what that note says about it, the
answer is NO.

The two mistakes are not equal, and they are not symmetric:

- **Marking something satisfied when it is not** tells the planner to stop
  looking for a thing the library does not have. Nobody ever finds out. This is
  the expensive mistake.
- **Marking something unsatisfied when a note does cover it** costs one query
  whose results are visible, and the judge will decline the duplicate.

So when the evidence is thin, say NO. Bias toward NO exactly as the judging
stages bias toward keeping — for the same reason, that one error is invisible
and the other is cheap and obvious.

## What counts as evidence

Name the video. `detail` must say *which* note covers the item and what it
supplies — "the Mem0 walkthrough shows add/search calls and the update path" is
evidence. "Several videos discuss this" is not; it is the shape of an answer
with nothing behind it, and it is what you write when you have not checked.

Do not count a note that is about the subject only in passing. A video that
mentions a tool once, in a list, does not cover that tool.

## Output

One line per part of the brief, and nothing else. No preamble, no summary.

```
COVERAGE: <part of the brief> | YES | <which note covers it, and what it supplies>
COVERAGE: <part of the brief> | NO | <what is missing>
```

Assess every MUST CONTAIN item, then every ALSO WANTED item. Use the brief's own
wording for `<part of the brief>`, shortened if it is long, so the planner can
match your lines against its own.

If the library holds nothing relevant, return a NO line for each part rather
than an empty reply — "nothing covers any of this" is a real assessment and an
empty reply is indistinguishable from a failed call.

---

BRIEF:

{{ brief }}

---

NOTES IN THE LIBRARY ({{ count }}), one line each:

    <video id> | <published> | <title> | <the note's opening claim>

These are index lines, not the notes. They are enough to say whether the library
*has something* on a brief item and never enough to judge how well it is
covered. So when a line is too thin to decide, answer **NO** — a wrong NO costs
one more search, and a wrong YES means a brief item is never searched for again.

{{ notes }}
