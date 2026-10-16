---
name: expand_queries
version: "2"
stage: search
note: "v2 aims the queries across every PART TO COVER, and writes a part that
  is a language or a market in that language. Checkpoint 1 measured a
  library-level language goal with no section of its own: it landed among
  the required items in 5 of 6 briefs, where no judge could act on it, and
  reached no search. The brief now carries such goals as parts, and this is
  where a part in another language becomes a query in it."
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

Spread the queries across the brief's PARTS TO COVER, so that every part has at
least one query aimed at it before any part gets a second. The library is meant
to cover every part, and a part no query reaches is a part nothing will find.

Where a part is a language or a market — "videos in Spanish as well as English",
"repairs as done in Brazil" — write at least one query for it in that language,
the words a person there would type ("reparar fuga de grifo", not a translation
of the brief's phrasing word for word). A query in English finds English videos,
so this is the only way such a part reaches the search.

Rules:
- Each query on its own line. Nothing else on the line.
- No numbering, no bullets, no quotes, no commentary before or after.
- Two to seven words each. These go to a video search engine, not a model —
  long sentences match nothing.
- No boolean operators, no site: filters, no punctuation tricks.
- Do not include anything the brief lists under EXCLUDED.
- Every query must be plausibly typed by a person. No keyword salad.
