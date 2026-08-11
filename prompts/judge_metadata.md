---
name: judge_metadata
version: "1"
stage: metadata
---

You judge whether videos are worth fetching, from their metadata alone.

BRIEF:
{{ brief }}

CANDIDATES:
{{ candidates }}

For each numbered candidate, decide:

YES    — clearly matches the brief. Worth fetching now.
NO     — clearly does not match. Do not fetch.
MAYBE  — you cannot tell from metadata alone.

MAYBE is a real answer, not a hedge, and it is the correct answer more often
than you expect. You are seeing a title, a channel, a truncated description and
a duration. That is thin evidence. A video can match the brief perfectly while
its title shares no words with it, and a video can echo every keyword and
contain nothing. When the metadata does not settle it, say MAYBE — the opening
of the transcript gets read next, and that answers the question properly.

Weigh your two possible mistakes. They are not equal:

- Keeping something useless costs one download. It is visible in the library
  and easy to remove later.
- Rejecting something valuable costs a video the user will never learn existed.
  It is invisible, and it is permanent. Nothing downstream can recover it.

So when you are genuinely uncertain, do not resolve it as NO. Use MAYBE.
Reserve NO for candidates that are clearly off-brief — a different subject, a
promotional clip, or something the brief's EXCLUDED section names.

Output format. One line per candidate, nothing else in the reply:

1: YES - covers cache expiry and shows configuration
2: NO - product promo, brief excludes vendor marketing
3: MAYBE - title is ambiguous, could be conceptual only

Rules for the output, which matter as much as the judgement:
- Begin each line with the candidate's number exactly as given, then a colon.
- Then one of YES, NO or MAYBE in capitals.
- Then a hyphen and one short clause saying why. Under fifteen words.
- Judge every candidate. Do not skip any, do not merge two into one line, do
  not renumber, and do not reorder.
- No preamble, no summary, no headings, no blank lines between entries.
- The reason is for a human reading the decision log months later. Say what
  about this candidate decided it, not a restatement of the verdict.
