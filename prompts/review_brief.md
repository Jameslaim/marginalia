---
name: review_brief
version: "2"
stage: brief
note: "v2 corrects the claim that whatever the reviewer returns is used. A
  reply under eighty characters has been discarded in favour of the draft
  since the check was written alongside v1, so the sentence was never true."
---

You review a research brief for a video library and return an improved one.

Return the REVISED BRIEF ITSELF, not a critique of it. Nothing downstream reads
commentary — whatever you output is what every later judgement is checked
against, unless it is under eighty characters, in which case it is discarded
and the unreviewed draft is used instead. A list of suggestions here is a
brief that never gets improved.

TOPIC: {{ topic }}

BRIEF UNDER REVIEW:
{{ brief }}

Look for these failures specifically, because they are the ones that quietly
ruin a library:

- Criteria too vague to act on. A judge seeing only metadata cannot apply
  "high quality" or "well explained". Replace them with observable ones.
- A missing EXCLUDED section, or one that misses the obvious near misses. This
  is the most common gap. What will a keyword search drag in that looks right
  and is not?
- An INTENT that silently narrows the topic to a sub-question.
- MUST CONTAIN items that are really preferences, or ALSO WANTED items that
  are really requirements.
- Edge cases stated without an answer. "What about conference talks?" is not
  useful; "conference talks count if they show configuration" is.
- Criteria that would exclude a video for being introductory or advanced when
  the topic did not ask for a level.

Output the complete revised brief using the same five headings, in the same
order, with no preamble:

INTENT
MUST CONTAIN
ALSO WANTED
EXCLUDED
EDGE CASES

If a section was already sound, carry it through unchanged rather than
rewording it for its own sake. Keep everything the original got right.
Plain text only.
