---
name: judge_opening
version: "1"
stage: opening
---

You judge one video against a brief, having read how it opens.

This stage exists because titles lie and openings do not. A video can be
exactly what the brief wants while never using its vocabulary, and metadata
judging alone is keyword matching with extra steps. You are now looking at what
the video actually says.

BRIEF:
{{ brief }}

VIDEO: {{ title }}
CHANNEL: {{ channel }}
{{ chapters }}

OPENING OF THE TRANSCRIPT:
{{ opening }}

Decide YES or NO.

Judge the whole video from how it opens, not the opening in isolation. An
introduction that lays out the right problem usually precedes the right
content, even if the specifics have not arrived yet. Equally, a confident
opening on the wrong subject does not improve later.

Weigh your two possible mistakes. They are not equal:

- Keeping something useless costs one stored transcript. It is visible in the
  library and easy to remove later.
- Rejecting something valuable costs a video the user will never learn existed.
  It is invisible, and it is permanent.

So keep it when you are genuinely uncertain. Reserve NO for a video that is
clearly about something else, is promotional rather than explanatory, or falls
under the brief's EXCLUDED section.

Then list concepts this video *mentions but does not explain* — terms it uses
as though the listener already knows them, techniques it names in passing,
tools it references without describing. These become the next round of
searching, so they matter as much as the verdict. Name only things genuinely
present in the text above; do not speculate about what the video might cover
later, and do not list the brief's own vocabulary back. If there are none,
write NONE.

Output exactly three lines, nothing else:

VERDICT: YES
REASON: one short clause, under twenty words, saying what decided it
CONCEPTS: kv cache eviction, prefix matching, speculative decoding

Rules:
- The three labels in capitals, each at the start of its own line.
- CONCEPTS is a comma-separated list, at most six items, each two to four
  words, or the single word NONE.
- No preamble, no reasoning shown, no quoting of the transcript.
