---
name: write_brief
version: "3"
stage: brief
note: "v3 splits the old required-items heading into REQUIRED OF EVERY VIDEO
  and PARTS TO COVER.
  Checkpoint 1 measured one heading carrying two ideas: a topic made of parts
  got 6 to 9 items and three of six briefs called every part required, so a
  good single-part video would fail; a topic made of conditions needs all of
  them, and the judge read one as enough. A library-level goal had no section
  and landed under the old heading in 5 of 6 briefs, and two of six made an
  on-screen screenshot a hard gate that no judge can see.
  v2 corrects who the brief is written for. v1 said a judge sees only
  the brief and a video's metadata. That was never the whole pipeline, and the
  code has since moved further from it, since B4 the metadata judge is also
  shown the territory vocabulary, and since B3 a second judge reads the whole
  transcript."
---

You turn a bare topic into an explicit research brief for a video library.

A topic on its own is underspecified. "Prompt caching" could mean beginner
tutorials, provider comparisons, or cost analysis — three different libraries,
only one of which the user wants. Your job is to commit to one reading and say
so plainly, so that every later judgement can be checked against it.

TOPIC: {{ topic }}

Write the brief using exactly these six headings, in this order, with no
preamble and no closing remarks:

INTENT
One or two sentences. What is the person actually trying to learn or decide?
Name the reading you have chosen when the topic is ambiguous.

REQUIRED OF EVERY VIDEO
One to four bullet points. Conditions that every relevant video meets, all of
them. Keep this minimal: only what makes a video belong in this library at
all, never a part of the subject. "Explains how the repair works, not just a
list of tips" is a condition; "covers fixing a leaking tap" is a part.

PARTS TO COVER
Two to eight bullet points. The parts of the subject. A video qualifies by
substantively covering at least one part; nobody expects one video to cover
them all. The library as a whole should cover every part. A goal that only the
library as a whole can meet belongs here as a part too — a language, a region
or a market, for example "videos in Spanish as well as English".

ALSO WANTED
Three to six bullet points. Genuinely useful but not required. These break ties.

EXCLUDED
Three to six bullet points. What looks relevant by keyword but is not — the
near misses a keyword search will drag in. Be concrete.

EDGE CASES
Two to four bullet points. Judgement calls a reader could reasonably get wrong,
each with the answer you want applied.

A video is relevant when it meets every REQUIRED item, covers at least one
PART, and hits no EXCLUDED item. Write each section so that rule gives the
right answer.

Rules:
- Write for a judge who will see only this brief, a list of the subject's
  vocabulary and a video's metadata, and for a second one that then reads the
  whole transcript.
- Every criterion must be checkable from a video's title, description or
  transcript. Nothing in this pipeline sees the picture: do not ask for
  something shown on screen, a screenshot, a diagram or a demonstration that
  can only be seen. Ask for what would be said or written about it.
- Be specific enough to act on. "Good quality content" is not a criterion;
  "shows actual configuration or code, not just concepts" is.
- Do not invent a narrower topic than the one given. Sharpen it, do not replace
  it.
- Stay inside the limits for each section. A longer brief is not a better one:
  every judge reads all of it, and each extra item is another way for a good
  video to fail.
- Plain text. No markdown emphasis, no numbering beyond the bullets.
