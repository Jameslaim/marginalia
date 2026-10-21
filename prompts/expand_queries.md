---
name: expand_queries
version: "5"
stage: search
note: "v5 is told what is already held and aims at what is missing. It wrote
  from the brief alone, seeded from round two with threads — concepts from
  the round's keepers — so when a round kept mostly two well-covered parts,
  the threads were those parts' vocabulary: about 12 of 18 expansion queries
  in two rounds aimed at parts the library held well, three at one sub-topic,
  and 17 of 25 keepers went to parts already covered. It now gets a COVERAGE
  SO FAR block (the baseline, then each round's plan), aims most queries at
  missing things, uses a thread only where it serves one, puts at most two
  queries on one aim, and names each query's aim after ` | ` so the code can
  count and cap it.
  v4 words a query like the title of a video that meets REQUIRED, as
  well as spreading the queries across the PARTS.
  Checkpoint 3 measured topic 2's first round: 4 queries worded like
  the videos REQUIRED asks for (case-study or real-case wording)
  returned about 4 keepable results in 10 (8, 4, 3 and 2), and the other 13,
  mostly a part's own vocabulary, about 0.9 (0 to 2); part vocabulary
  returned how-to videos about the part. A small sample, and the best query
  was also the only one in its language, so the effect is moderate.
  v3 stops naming a real language or market in its example. At
  checkpoint 2 the language the example named appeared in 2 of 3 topic-2
  expansions and 6 of 6 maps, including where the brief named another
  language first. The prompt now states which languages to take and in
  what order, and the example names none.
  v2 aims the queries across every PART TO COVER, and writes a part that
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

**Aim at what is missing.** The COVERAGE SO FAR block below the brief says what
the library and this run already hold: each part HELD, PARTLY HELD with its
missing things, or MISSING. A query aimed at something held finds a second copy
of it; the search is for the rest.

- Aim most queries at missing things — the missing things of a PARTLY HELD
  part, and the MISSING parts — so that every missing thing has a query before
  any gets a second. When nothing has been assessed, spread the queries across
  every PART TO COVER the same way.
- At most two queries on any one aim.
- Do not aim a query at a HELD part.

THREADS, when there are any, are concepts that videos already kept mentioned
without explaining: words the brief never typed, which is what they are for.
They come from what was kept, so most belong to parts already held. Use a
thread only where it serves a missing thing, and never let threads take over
the list.

Word each query like the title of a video that meets every REQUIRED item, not
only like the part it aims at. A part says what a video is about; REQUIRED OF
EVERY VIDEO says what kind of video it must be. A query made only of a part's
own vocabulary finds every video on that subject, and most of them are the
kind REQUIRED rules out. If a cycling brief requires "a rider's own measured
results", `saddle height adjustment` finds how-to videos, and `changed my
saddle height results` finds the riders. Put the words that kind of video
carries in its title into most queries; a video can meet REQUIRED with a title
that never says so, so keep some queries on the subject's words alone.

Where a part is a language or a market — "videos in a second language as well
as English", "repairs as done in a named market" — write at least one query for
it in that language, the words a person there would type, not a translation of
the brief's phrasing word for word, and worded like the videos REQUIRED asks
for. A query in English finds English videos, so
this is the only way such a part reaches the search.

Which languages, in this order:

- the languages the brief names, in the order it names them;
- one query in each before any language gets a second;
- if a part asks for other languages without naming them, the languages this
  subject is most made in.

Never a language because an example mentioned it. An example shows the format;
it says nothing about which languages this subject is made in.

Rules:
- Each query on its own line, then ` | ` and its aim: the missing thing or part
  it is for, in the brief's or the coverage block's own words, or
  `thread: <the concept>`. Nothing else on the line.
- No numbering, no bullets, no quotes, no commentary before or after.
- Two to seven words each. These go to a video search engine, not a model —
  long sentences match nothing.
- No boolean operators, no site: filters, no punctuation tricks.
- Do not include anything the brief lists under EXCLUDED.
- Every query must be plausibly typed by a person. No keyword salad.
