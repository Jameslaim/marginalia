---
name: expand_queries
version: "1"
stage: search
---

You turn a research brief into a set of video search queries.

A single query finds a single corner of a subject. People title videos in
wildly different ways — technical jargon, plain language, problem statements,
product names — and a video can be exactly what the brief wants while sharing
no words with it. Varied phrasings are the only defence against that.

BRIEF:
{{ brief }}

{{ extra }}

Write {{ count }} search queries. Vary them deliberately across these angles,
rather than rephrasing one idea {{ count }} times:

- the precise technical term a practitioner would use
- the plain-language phrasing someone would type who does not know that term
- the problem or symptom that leads people to this subject
- a specific mechanism, parameter, or sub-component named in the brief
- an adjacent subtopic that good coverage would have to touch
- the comparison or decision framing ("X vs Y", "when to use X")

Rules:
- Each query on its own line. Nothing else on the line.
- No numbering, no bullets, no quotes, no commentary before or after.
- Two to seven words each. These go to a video search engine, not a model —
  long sentences match nothing.
- No boolean operators, no site: filters, no punctuation tricks.
- Do not include anything the brief lists under EXCLUDED.
- Every query must be plausibly typed by a person. No keyword salad.
