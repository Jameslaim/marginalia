---
name: write_brief
version: "2"
stage: brief
note: "v2 corrects who the brief is written for. v1 said a judge sees only
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

Write the brief using exactly these five headings, in this order, with no
preamble and no closing remarks:

INTENT
One or two sentences. What is the person actually trying to learn or decide?
Name the reading you have chosen when the topic is ambiguous.

MUST CONTAIN
Three to six bullet points. A video that lacks all of these is not relevant,
whatever its title says.

ALSO WANTED
Three to six bullet points. Genuinely useful but not required. These break ties.

EXCLUDED
Three to six bullet points. What looks relevant by keyword but is not — the
near misses a keyword search will drag in. Be concrete.

EDGE CASES
Two to four bullet points. Judgement calls a reader could reasonably get wrong,
each with the answer you want applied.

Rules:
- Write for a judge who will see only this brief, a list of the subject's
  vocabulary and a video's metadata, and for a second one that then reads the
  whole transcript.
- Be specific enough to act on. "Good quality content" is not a criterion;
  "shows actual configuration or code, not just concepts" is.
- Do not invent a narrower topic than the one given. Sharpen it, do not replace
  it.
- Plain text. No markdown emphasis, no numbering beyond the bullets.
