---
name: write_note
version: "2"
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

**Leaves out** — the most valuable section. Objections never addressed.
Questions raised and dropped. The obvious follow-up never taken. Where the
video stops short. Costs, failure modes, or "when is this *not* worth it"
questions that go unanswered. Someone searching for the answer to a question
this video fails to ask should still be led here.

**Key moments** — timestamps worth jumping to, each with what happens there.
Pacing is signal: if eleven minutes go to setup and ninety seconds to the most
actionable content, say so. If a transcript has conspicuous timing gaps between
lines, an edit was made there; note it where it is striking, but do not
speculate about what was cut.

**Who's talking, what they want** — the speaker, their affiliation, what is
being sold, whether there is a sponsor, and roughly what share of the runtime
is promotional. State it neutrally. A good video with a sponsor is still a good
video, and the reader can weigh it themselves.

Ground this in the video and the metadata above. If something you know about
the speaker from elsewhere genuinely changes how the video should be read, you
may say it — but mark it as outside knowledge in the sentence itself, so a
reader can tell what the video showed from what you brought to it.

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
