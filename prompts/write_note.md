---
name: write_note
version: "3"
stage: note
---

You have read one video end to end. Write down what you understood, for a
reader who has not seen it and may never see it.

This is not a summary. A summary of a video about caching would say "explains
caching, covers provider differences and expiry" — that is a topic label. It
would not tell you that the speaker calls quadratic growth "exponential" twice,
contradicts himself four minutes apart, and never mentions that cache writes
cost a premium, which is the single most useful thing about that video.

Write what a careful reader would tell a colleague who asked: *is this worth
watching, and what does it actually say?*

VIDEO: {{ title }}
CHANNEL: {{ channel }}
DURATION: {{ duration }}
PUBLISHED: {{ upload_date }}
CAPTIONS: {{ caption_kind }}
{{ chapters }}

TRANSCRIPT (each line carries its start and end timestamp):
{{ transcript }}

---

Output exactly these seven sections, in this order, each introduced by its
heading on its own line, spelled exactly as written here:

## Claims
## Wrong or shaky
## Assumes you know
## Leaves out
## Key moments
## Who's talking, what they want
## Verbatim

Free prose inside each section — sentences or bullets, whichever fits. The
headings are fixed so that fifty of these notes can be read together.

**Mark anything that is not from the transcript.**

This is the most important rule in this prompt, and it applies inside every
section above.

Your own reasoning is welcome and often the most valuable thing in a note.
"The video never states the break-even point, but from its own figures it is
about 1.3 cached reads" is worth more than silence. What is forbidden is that
sentence being indistinguishable from something the speaker said.

So: **if it did not come out of the transcript, wrap it in a marker.**

```
[note: from the figures given, break-even is ~1.3 cached reads — the video never states this]
```

The marker is required for all of:

* **arithmetic you performed** — ratios, percentages, totals, unit conversions,
  anything the speaker did not say out loud
* **extrapolation** — "this only holds under concurrency", "a single-user
  workload would see much less"
* **outside knowledge of the subject** — prices, limits, defaults, API
  behaviour, how the technology actually works, whether the speaker is right
* **outside knowledge of the speaker, channel, product or employer**
* **comparison to anything outside this video** — other providers, other
  tools, standard practice, what is conventional

The section where this goes wrong most is **Leaves out**, because saying "the
video never covers X" requires you to know X — and the explanation of X that
follows is then yours, not the video's. Mark it. The same applies to **Wrong or
shaky**: naming an error is a claim about the world, and the standard you are
judging against comes from outside the transcript.

A reader must be able to run `grep '\[note:'` over this file and see exactly
which parts are record and which are inference. Anything unmarked is a promise
that the video said it.

**Claims** — the actual assertions the video makes, stated so they could be
agreed or disagreed with. "Caching stores inputs, not outputs" is a claim.
"Discusses how caching works" is not. Include the specifics: numbers,
conditions, thresholds, the cases where the speaker says a thing does or does
not apply.

**Wrong or shaky** — errors, with timestamps. Terms used incorrectly.
Self-corrections, and how far apart the two statements were. Internal
contradictions. Hedges, and what they are hedging about. Numbers asserted
without support. Advice that only holds under conditions the speaker did not
state. Be specific and be fair: quote or cite the timestamp rather than
characterising.

If the captions are automatic, a mangled term is the transcriber's error and
not the speaker's, and treating it as the speaker's puts a mistake in someone's
mouth that they did not make. Still note it — a number garbled at the exact
moment the speaker states the most important threshold is worth a reader
knowing about — but say which kind of error you think it is.

**Assumes you know** — the prerequisites the video never explains. Terms used
as though familiar, tools named without introduction, steps skipped as obvious.
This is what tells a reader whether the video is pitched at them.

Do this as a deliberate pass rather than from memory: go back through the
transcript looking specifically for terms, tools, file names, parameters and
acronyms that appear without ever being defined, and collect them. They are
easy to read straight past, because by the end of a transcript every term in it
feels familiar. A term the speaker uses once, in passing, as though everyone
knows it is exactly what this section is for.

**Leaves out** — the most valuable section. Objections never addressed.
Questions raised and dropped. The obvious follow-up never taken. Where the
video stops short. Costs, failure modes, or "when is this *not* worth it"
questions that go unanswered. Someone searching for the answer to a question
this video fails to ask should still be led here.

**Key moments** — timestamps worth jumping to, each with what happens there.

Every timestamp you write, in this section or any other, must be **the start
timestamp of the transcript line where the claim begins** — not where the
sentence finishes, and not the line after. Caption lines overlap, so the phrase
you are citing often spans two or three of them: take the start of the first
one. A timestamp that lands a few seconds late puts the reader in the middle of
the sentence, which is tolerable for jumping and wrong for quoting, and both
are done with these.

Pacing is signal: if eleven minutes go to setup and ninety seconds to the most
actionable content, say so. If a transcript has conspicuous timing gaps between
lines, an edit was made there; note it where it is striking, but do not
speculate about what was cut.

**Who's talking, what they want** — the speaker, their affiliation, what is
being sold, whether there is a sponsor, and roughly what share of the runtime
is promotional. State it neutrally. A good video with a sponsor is still a good
video, and the reader can weigh it themselves.

Ground this in the video and the metadata above. If something you know about
the speaker from elsewhere genuinely changes how the video should be read, say
it — inside a `[note: ...]` marker like everything else that did not come from
the transcript. Who someone works for is exactly the kind of fact a reader will
otherwise assume the video stated.

**Verbatim** — exact quotes worth preserving: commands, configuration, prompts,
precise phrasings, specific numbers. These are what hand-written notes lose
first and what a reader is most likely to want quoted.

Two rules that override everything above:

1. **Never quote more than is needed.** Verbatim is for short, high-value
   excerpts — a command, a configuration line, a claim stated precisely. Not
   paragraphs. If an excerpt runs past a couple of sentences, it is too long.

2. **Say when a section is empty.** Write one short line saying so — "No errors
   found; the technical claims hold up" or "Nothing assumed beyond basic
   familiarity with the command line." A note that claims a video has no flaws
   is a claim, and a useful one. A note that silently omits the section is
   ambiguous, and a reader cannot tell it apart from a note that was cut short.

If the video published chapter markers, they are the author's own structure and
are better than anything you would infer — use them when organising Key
moments.

Write the sections and nothing else. No preamble, no title line, no closing
remarks: the heading and metadata lines are added around your output.
