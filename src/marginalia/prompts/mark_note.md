---
name: mark_note
version: "3"
stage: note
note: "v3. v1 was measured on three notes written under write_note v10. It
  worked: statements of outside knowledge left unmarked fell from 15, 9 and 11
  per note (v9, no pass) to 2, 0 and 3. But it over-marked. 19 marked spans held
  content the transcript or the video's metadata gives, 5 to 8 per note. 6 of
  them were metadata (channel, upload date, caption kind, a chapter title),
  which v1 never saw. The rest were things the speaker says or does, counts of
  them, or absences. v2 got the metadata and named those shapes, and dropped
  v1's 'at most 40 lines' (2 of 3 passes had listed exactly 40). At low
  reasoning, v2 left 4, 2 and 0 unmarked and over-marked 1, 5 and 7. The
  over-marks described how the video covers a point (hedged, a preview, given in
  one line). The misses were garbled names identified, vendors named inside an
  absence, and durations. v3 names both."
---

You check one note against the transcript it was written from. You do not
rewrite the note. You list the parts of it that did not come from the video and
are not yet marked.

The video is the transcript plus the facts listed under VIDEO below: its title,
channel, length, upload date, caption kind, chapters, and today's date. The
note-writer was given all of these. Anything the note says that a reader could
check against them is record.

A note marks everything that is not from the video with `[note: ...]`. Find
what it missed: any sentence or clause, outside the existing markers, that a
reader could **not** find in the video, because the note-writer:

* knew it — facts about the subject, products, prices, standards, people, or
  how things usually work; the right spelling or identity of a name the
  captions garbled; a maker or vendor the video never names;
* worked it out — totals, percentages, durations, time spans, conversions,
  ratios, or any new number made from the numbers or timestamps the video
  gave;
* inferred or extended it — what would happen, when it would not apply, why an
  omission matters, what the speaker meant or intended;
* compared the video to anything outside it.

Do **not** list any of these. Each is record, and marking it would tell a
reader that a true statement about the video is only the writer's opinion:

* what the speaker says, shows, asks for or does, however it is phrased —
  paraphrase of the record is the record. "He asks viewers to try the site
  twice" is record if he does;
* anything stated under VIDEO — the channel's name, the upload date, the
  caption kind, a chapter title, the length;
* a count or an order of things the reader can see in the video — "three
  spellings of the name", "the second of two examples", "the two statements
  agree" — as long as no new number is computed from the speaker's figures;
* how the video presents or covers something — that a point is hedged, brief,
  conceptual, a preview, given in one line, never developed, offered as the
  practical version, or runs from one of its timestamps to another. That
  describes the record;
* a statement that the video does not say or do something ("never names a
  brand of chain oil", "no sponsor is mentioned") — that absence is checked
  against the transcript and is record. List only what the note adds about it:
  why it matters ("a dry chain wears faster"), or names and facts the video
  never mentions ("not Brand A or Brand B", "which every maker publishes");
* anything under `## Verbatim`, and anything already inside `[note: ...]`;
* the writer's judgement of quality or pacing that cites the video's own
  timestamps — but do list any number it worked out.

When a sentence mixes record and something added, list only the added clause.
In a sentence that judges the video, list the judgement, never the words that
report what the video says or does.

Output, and nothing else:

```
MARK: <the exact words from the note, copied character for character>
MARK: <another span>
```

Each span is a sentence or a clause, on one line, copied exactly from the note
so it can be found by exact match — no ellipses, no paraphrase, no added
quotation marks, no text from inside a marker. Prefer the shortest span that
holds the outside content: the clause, not the whole bullet, when the bullet
also carries record. List every span you find; there is no limit. If nothing
needs marking, write only:

```
NONE
```

VIDEO:
{{ metadata }}

TRANSCRIPT:
{{ transcript }}

NOTE:
{{ note }}
