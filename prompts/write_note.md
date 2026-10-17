---
name: write_note
version: "10"
stage: note
note: "v10 makes the marking rule hold, and takes the worked examples off a
  test subject. Audited on one note under v9: the record was faithful (19 of
  19 claims supported, 14 of 14 quotes exact, 14 of 14 key moments on time),
  but 14 statements of outside knowledge, arithmetic or extrapolation were
  unmarked, and 8 of 10 Leaves-out items carried no marker, in the section the
  prompt names as the one where this goes wrong most. Ask reads a note as what
  the video says, so an unmarked outside fact becomes an answer attributed to
  the video. The rule now comes with right and wrong pairs for the sections
  where it failed, and a second pass marks what still slips through (see
  `mark_note`). Every worked example was about one subject; a note on a video
  about that subject reused two of the example's terms outside markers. The
  examples are now about bicycles and cooking, each keeping its teaching point.
  v9 says what Verbatim is held to. The worker now checks every quote
  in it against the transcript and marks the ones it cannot find. Measured on
  the archived library before the change: 581 of 5,316 Verbatim quotes in notes
  of English videos (11%) and 423 of 673 in notes of other-language videos
  (63%, mostly translations) have no close match in the transcript, so the
  section now asks for the original wording with any translation in a marker.
  v8 drops the section count. The prompt said seven sections and
  listed eight from the commit that added `How current is it` (2032513); the
  list is now the only count, so the next section added cannot make it wrong.
  v7 passes TODAY. A model applied its training cutoff to a video's
  publication date and recorded as fact that content it had never heard of did
  not exist — calling it `not a real model` or `appears to be a hallucination`
  — and called the source `speculative or from a future/alternate timeline`.
  `How current is it` is mandatory, so every note makes an era judgement and a
  model that cannot place itself in time gets it wrong systematically. Same
  reasoning as rule 16 passing `today` into the territory validator.
  v5 adds the `How current is it` section. Measured need: of 24 LangChain
  videos kept in run 7da933b180fd, only 11 notes carried any era marker, and
  those only because the video happened to discuss its own version. A 2023
  pre-0.1 tutorial sat in the library described as current. The date was already
  in the prompt and nothing asked the note to use it."
---

You have read one video end to end. Write down what you understood, for a
reader who has not seen it and may never see it.

This is not a summary. A summary of a video about bicycle chains would say
"explains chain wear, covers lubricants and replacement" — that is a topic label.
It would not tell you that the speaker calls a steady rate of wear "exponential"
twice, contradicts himself four minutes apart about how often to measure, and
never mentions that a worn chain also wears out the sprockets, which is the
single most useful thing about that video.

Write what a careful reader would tell a colleague who asked: *is this worth
watching, and what does it actually say?*

VIDEO: {{ title }}
CHANNEL: {{ channel }}
DURATION: {{ duration }}
PUBLISHED: {{ upload_date }}
TODAY: {{ today }}
CAPTIONS: {{ caption_kind }}
{{ chapters }}

TRANSCRIPT (each line carries its start and end timestamp):
{{ transcript }}

---

Output exactly these sections, in this order, each introduced by its
heading on its own line, spelled exactly as written here:

## Claims
## Wrong or shaky
## Assumes you know
## How current is it
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
"The video never states what a chain costs per kilometre, but from its own
figures it is about two cents per hundred" is worth more than silence. What is
forbidden is that sentence being indistinguishable from something the speaker
said.

So: **if it did not come out of the transcript, wrap it in a marker.**

```
[note: from the figures given, a chain costs about two cents per hundred kilometres — the video never states this]
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

**The test, for every sentence: could a reader find this in the transcript?** If
the video said it, leave it plain. If you knew it, worked it out, inferred it or
compared it, mark it. Where one sentence does both, split it: the record plain,
the rest in the marker. These are the shapes that slip through:

* **Leaves out.** *Wrong:* "Never covers how often to oil the chain, which matters
  because a dry chain wears twice as fast." *Right:* "Never says how often to oil
  the chain. [note: a dry chain wears faster; that is my knowledge, not the
  video's]" The absence is record; why it matters is yours.
* **Assumes you know.** *Wrong:* "Assumes you know that sprockets are sold in
  sizes measured by tooth count." *Right:* "Uses 'sprocket' without defining it.
  [note: sizes are given by tooth count, which the video never explains]"
* **Wrong or shaky.** *Wrong:* "The proving time is too short, since dough needs at
  least an hour at room temperature." *Right:* "Proves the dough for twenty
  minutes (03:10). [note: most recipes allow an hour or more; the video gives no
  reason for the shorter time]"
* **How current is it.** *Wrong:* "These brake standards have not changed in a
  decade." *Right:* "The video gives no date signal. [note: I believe the brake
  standards it describes are long-settled; a reader should confirm]"
* **Who's talking, what they want.** *Wrong:* "About 2% of the runtime is
  promotional." *Right:* "The sponsor read runs from 14:10 to the end. [note:
  about 2% of the runtime by my count]" Percentages and durations you worked out
  are arithmetic.
* **Key moments and pacing.** *Wrong:* "Six minutes of theory before any
  practical step." *Right:* "Theory from 01:10 to 07:20, then the first
  practical step. [note: about six minutes, by subtraction]"

A note with many markers is not a weaker note. A marker tells the reader whose
knowledge a sentence carries; leaving it off tells them it was the video's.

**Claims** — the actual assertions the video makes, stated so they could be
agreed or disagreed with. "A chain should be replaced at half a percent of
stretch" is a claim. "Discusses chain wear" is not. Include the specifics: numbers,
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

**How current is it** — whether what this video teaches still holds.

You are given `PUBLISHED`. Use it. A video is not wrong because it is old, and
that is exactly what makes this hard: an obsolete tutorial is accurate about a
world that has moved, and its transcript never announces the fact. A reader who
cannot tell which era they are looking at will follow it anyway.

So, where the video teaches something version-sensitive — a library API, a tool,
a product or model name, a price, a limit, a practice — say which era it belongs to and
**how a reader can tell from the video itself**. The tells are concrete: a
version number said aloud or shown on screen, an import path, a class or method
name, a UI that no longer looks like that, a price quoted for a unit of use.
Name the tell, not just the conclusion.

Judge from the transcript and the date. **Do not judge from what you know about
the subject.** If you believe a named API is superseded, that belief is outside
knowledge and must be marked `[note: ...]` like any other — a reader has to be
able to tell "this video says it is using v0.1" from "I think this is old". If
the video gives no version signal at all, say that. *"Published 2023-11; the
transcript names no version and shows no import paths, so its era cannot be
established from the video"* is a complete and useful answer.

Where content is plainly time-bound — pricing, product names, library APIs, rate
limits — say so and say what a reader would need to re-check before relying on
it.

**`TODAY` is the present. Content published after your training data is expected,
not suspicious.** A video may name products, versions and prices you have never
heard of, and when it does, the thing that is out of date is your knowledge — not
the video. You are reading a real transcript of a real video published on
`PUBLISHED`, which may be long after anything you were trained on.

So where a video names something you do not recognise, the honest note says the
claim **could not be verified against your own knowledge**, and marks that as
outside knowledge like any other: *"[note: the product name given here postdates my
training data; I cannot confirm it, and a reader should check it against the
provider's current documentation]"*. That is a useful sentence and exactly what
this section is for.

**Do not call it a hallucination, a transcription error, speculative, fictional,
or from a future or alternate timeline.** Those are claims about the world that
you are not positioned to make from a training cutoff, and a note that makes them
tells a reader a real product does not exist. Not recognising a name is a fact
about you. It is never evidence about the video.

If nothing in the video is version-sensitive, write one line saying so. A
tutorial on a stable idea does not need a caveat invented for it.

**This section is never omitted.** "Nothing here is version-sensitive" and "this
video gives nothing to date it by" are both *findings*, and both belong under the
heading rather than in its absence — a reader who sees no section cannot tell
which of the two it was, or whether the section was simply forgotten.

Measured, which is why it is stated this plainly: three of four note failures
across two drains were this section missing, every one of them on a video with
essentially no era signal. The model read the section as inapplicable and left it
out. It is inapplicable *content* and a mandatory *heading* — write the sentence
that says so.

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

**Verbatim** — exact quotes worth preserving: commands, configuration, settings,
precise phrasings, specific numbers. These are what hand-written notes lose
first and what a reader is most likely to want quoted.

This is the one section that promises the speaker's own words, and every
quotation in it is checked against the transcript above after you write it. So:
copy each quote from the transcript lines, **in the language the speaker used**,
without tidying the grammar, completing a sentence, or rebuilding it from
memory. Put quotation marks around what is in the transcript and nothing else.
If the video is not in the language you are writing in, quote the original and
put your translation after it inside a marker:
`"laisser reposer la pâte une heure" [note: translation: let the dough rest for an
hour]`. Something worth keeping that you cannot find word for word — a
command shown on screen, a paraphrase — goes in without quotation marks.

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
