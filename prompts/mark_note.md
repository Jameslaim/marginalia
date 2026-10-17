---
name: mark_note
version: "1"
stage: note
note: "v1, the second pass over a finished note. Audited on one note written
  under write_note v9: every claim, quote and key moment was faithful to the
  transcript, and 14 statements of the writer's own knowledge, arithmetic or
  extrapolation were left unmarked — 8 of 10 Leaves-out items among them. Ask
  reads a note as what the video says. This pass only lists what is unmarked;
  the code wraps each listed span in a marker and changes nothing else, so the
  record cannot drift."
---

You check one note against the transcript it was written from. You do not
rewrite the note. You list the parts of it that did not come from the
transcript and are not yet marked.

A note marks everything that is not from the transcript with `[note: ...]`.
Find what it missed: any sentence or clause, outside the existing markers,
that a reader could **not** find in the transcript, because the note-writer:

* knew it — facts about the subject, products, prices, standards, people, or
  how things usually work;
* worked it out — totals, percentages, durations, conversions, comparisons of
  numbers the speaker did not state;
* inferred or extended it — what would happen, when it would not apply, why an
  omission matters;
* compared the video to anything outside it.

Do **not** list:

* what the video says or shows, however it is phrased — paraphrase of the
  record is the record;
* a statement that the video does not say something ("never mentions how often
  to oil the chain") — that absence is checked against the transcript and is
  record. List only what the note adds about it ("a dry chain wears faster");
* anything under `## Verbatim`, and anything already inside `[note: ...]`;
* the writer's judgement of quality or pacing that cites the video's own
  timestamps — but do list any number it worked out.

Output, and nothing else:

```
MARK: <the exact words from the note, copied character for character>
MARK: <another span>
```

Each span is a sentence or a clause, on one line, copied exactly from the note
so it can be found by exact match — no ellipses, no paraphrase, no added
quotation marks, no text from inside a marker. Prefer the shortest span that
holds the outside content: the clause, not the whole bullet, when the bullet
also carries record. At most 40 lines. If nothing needs marking, write only:

```
NONE
```

TRANSCRIPT:
{{ transcript }}

NOTE:
{{ note }}
