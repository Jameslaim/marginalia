---
name: plan/baseline
version: "6"
stage: plan
note: "v6 is shown evidence that can show coverage. v2 to v5 were shown one
  index line per note, a title and the first claim, and told to answer NO
  when a line was too thin; against a brief whose parts each list several
  things no line could pass, and one run marked 0 of 14 items held while the
  library's keepers' own stage-2 reasons named five of the parts. Each note now
  comes with the latest stage-2 reason on its video and its Claims, within a
  budget; a first line says whether they were cut to fit. A wrong YES is still
  the expensive error, but a listed thing a claim or reason states is held.
  v5 assesses each part per listed thing, as plan/round v11 does. A
  brief writes a part as a list, and a yes-or-no per part could only call a
  half-held part covered or a gap. A part is YES when every thing it lists is
  held, PARTLY when some are, NO when none is, and each line names what is
  held and what is missing.
  v4 assesses every PART TO COVER, then every ALSO WANTED item. The
  brief's old required-items heading was split into REQUIRED OF EVERY VIDEO,
  conditions each video meets, and PARTS TO COVER, what the library must
  hold; only the parts are coverage targets.
  v3 corrects the opening, which still said every note follows the
  brief. v2 changed the code to send one index line per note and the lower
  half of this prompt was updated, the opening was not.
  v2 is fed one summary line per note instead of whole notes. v1 sent the
  library entire with no budget check and at 646 notes asked for 1,833,240
  tokens against a 1,048,576 ceiling; it failed open, so the planner silently
  assumed nothing was covered and re-searched ground already held."
---

You are checking what a library of video notes **already** covers, before any
searching happens.

A research brief is below, and after it the evidence for every understanding
note the library holds: the judge's reason for keeping or declining its video,
and the note's claims. Each note was written by one model reading one video end
to end. Your job is to say, for each part of the brief, which of the things it
lists the library already holds.

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

So a listed thing is held only when the evidence states it, and not when it
merely makes it plausible. But when a claim or a judge's reason **does** state
that a video explains a listed thing, that thing is held: you are not being
asked for more than the evidence can give, and "the line is thin" is not a
reason to call a stated thing missing. A wrong NO is cheaper than a wrong YES,
and it is not free: it sends a round to find again what the library holds.

## What counts as evidence

Name the video for every held thing, and what its evidence states — "the
pruning walkthrough's claims show where to cut and why" is evidence. "Several
videos discuss this" is not; it is the shape of an answer with nothing behind
it, and it is what you write when you have not checked.

Two kinds of evidence, read differently:

- **The judge's reason** was written by a model that read the whole
  transcript, to decide whether the video served a brief. One labelled *for
  this topic* was judged against this brief; one labelled *for another brief*
  still says what the judge saw the video explain. A NO reason can still
  describe what the video covers.
- **The claims** are the note-writer's statements of what the video says, in
  order. A claim that states the video explains a listed thing shows it held.

Do not count a note that is about the subject only in passing. A video that
mentions a tool once, in a list, does not cover that tool, and a title alone
holds nothing.

## Assess a part one listed thing at a time

A part is usually a list — "a subject: how one thing works, including a second
or a third, and why a fourth matters". A note covers the listed thing it
explains, not every thing beside it, so a part can be held in part:

- `YES` when every thing the part lists is held, naming the note for each.
- `PARTLY` when some listed things are held and others are not. Name what is
  held and by which note, and list the missing things: they, and only they,
  are this part's gap.
- `NO` when none is held, listing what is missing.

A part that names a single thing is `YES` or `NO`.

## Output

One line per part of the brief, and nothing else. No preamble, no summary.

```
COVERAGE: <part of the brief> | YES | held: <thing> — <which note, and what it supplies>; <thing> — <note>
COVERAGE: <part of the brief> | PARTLY | held: <thing> — <which note> | missing: <thing>; <thing>
COVERAGE: <part of the brief> | NO | missing: <thing>; <thing>
```

The things within `held:` and within `missing:` are separated by semicolons.

Assess every PART TO COVER, then every ALSO WANTED item. Do not assess REQUIRED
OF EVERY VIDEO: those are conditions each video must meet, not parts a library
holds. A part that is a language or a market is covered only by a note on a
video in that language or market. Use the brief's own wording for
`<part of the brief>`, shortened if it is long, so the planner can match your
lines against its own.

If the library holds nothing relevant, return a NO line for each part rather
than an empty reply — "nothing covers any of this" is a real assessment and an
empty reply is indistinguishable from a failed call.

---

BRIEF:

{{ brief }}

---

NOTES IN THE LIBRARY ({{ count }}), three lines each:

    <video id> | <published> | <title>
      judge: <the latest stage-2 verdict and reason on this video>
      claims: <the note's claims, separated by " / ">

The first line below says whether the evidence is complete. When it says
DEGRADED, each note's claims were cut to fit; a thing not shown may have been
cut rather than missing, so the evidence holds less, and you still mark held
only what it states.

{{ notes }}
