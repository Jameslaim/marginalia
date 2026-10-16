---
name: review_brief
version: "4"
stage: brief
note: "v4 follows write_brief v3: six sections, the old required-items
  heading split into REQUIRED OF EVERY VIDEO and PARTS TO COVER, and the limits restated here.
  Checkpoint 1 measured the review breaking the limits in 12 of 12 briefs
  while every draft kept them, reaching 16 EXCLUDED and 15 EDGE CASES items
  and growing a brief up to 2.2 times. v3 never restated the limits and its
  checklist only added, so a review now merges or cuts instead of appending.
  It also looks for requirements that are really parts, parts that are really
  requirements, and criteria that need the picture, which no judge sees.
  v3 states the check the code now makes. A reply is used only if it was
  not cut off and carries INTENT, the required-items section and EXCLUDED; v2 described the
  old eighty-character test, which let a truncated review or a critique become
  the brief.
  v2 corrects the claim that whatever the reviewer returns is used."
---

You review a research brief for a video library and return an improved one.

Return the REVISED BRIEF ITSELF, not a critique of it. Nothing downstream reads
commentary — whatever you output is what every later judgement is checked
against. A reply without the INTENT, REQUIRED OF EVERY VIDEO, PARTS TO COVER and
EXCLUDED sections is discarded and the unreviewed draft is used instead, so a
list of suggestions here is a brief that never gets improved.

TOPIC: {{ topic }}

BRIEF UNDER REVIEW:
{{ brief }}

The brief has six sections, and each has a limit:

INTENT — one or two sentences: the reading of the topic that was chosen.
REQUIRED OF EVERY VIDEO — one to four items. Conditions every relevant video
  meets, all of them. Only what makes a video belong in this library at all.
PARTS TO COVER — two to eight items. The parts of the subject. One video
  qualifies by covering at least one part; the library as a whole should cover
  every part. Goals only the whole library can meet, such as a language or a
  market, are parts.
ALSO WANTED — three to six items. Useful but not required; these break ties.
EXCLUDED — three to six items. The near misses a keyword search will drag in.
EDGE CASES — two to four items, each with the answer to apply.

A video is relevant when it meets every REQUIRED item, covers at least one
PART, and hits no EXCLUDED item.

Look for these failures specifically, because they are the ones that quietly
ruin a library:

- REQUIRED items that are really parts. If a good video could cover one of
  them and not the others, they are parts, and leaving them under REQUIRED
  means a video must cover the whole subject at once to be kept. "Covers
  fixing a leaking tap" and "covers clearing a blocked drain" are two parts of
  home plumbing, not two requirements.
- Parts that are really requirements. If a video lacking one would not belong
  in the library at all, it is a requirement, and leaving it under PARTS lets
  a video that meets none of the real conditions in. "The repair is explained
  step by step, not just shown finished" is a requirement.
- Criteria that need the picture. Nothing in this pipeline sees a video's
  images: one judge reads the title and description, the other the
  transcript. A criterion that asks for something shown on screen — a
  screenshot, a gauge reading, a finished result — can never be checked.
  Rewrite it as what would be said or written, or remove it.
- Criteria too vague to act on. A judge seeing only metadata cannot apply
  "high quality" or "well explained". Replace them with observable ones.
- A missing EXCLUDED section, or one that misses the obvious near misses. What
  will a keyword search drag in that looks right and is not?
- An INTENT that silently narrows the topic to a sub-question.
- ALSO WANTED items that are really requirements or parts.
- Edge cases stated without an answer. "What about conference talks?" is not
  useful; "conference talks count if they show configuration" is.
- Criteria that would exclude a video for being introductory or advanced when
  the topic did not ask for a level.

**Stay inside every limit.** When an improvement would take a section past its
limit, merge two items or cut the weakest one — never append. A longer brief is
not a better one: every judge reads all of it, and each extra item is another
way for a good video to fail.

Output the complete revised brief using the same six headings, in the same
order, with no preamble:

INTENT
REQUIRED OF EVERY VIDEO
PARTS TO COVER
ALSO WANTED
EXCLUDED
EDGE CASES

If a section was already sound, carry it through unchanged rather than
rewording it for its own sake. Keep everything the original got right.
Plain text only.
