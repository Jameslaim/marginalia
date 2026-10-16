---
name: map_territory
version: "5"
stage: territory
note: "v5 aims the queries across every PART TO COVER, and for a part that is
  a language or a market requires a query in that language with
  relevanceLanguage set. Checkpoint 1 measured a library-level language goal
  that had no section of its own and reached no search; the brief now
  carries it as a part, and this is where it becomes one.
  v4 replaces a vendor name in the example vocabulary:
  rule 7 says no prompt names a model family, and the test meant to hold
  that scanned only the top level of prompts/, so this file was never
  checked.
  v3 corrects the claim that a reasoned window goes on every main query.
  A per-query waiver and a topic that asks for material of any age both keep
  it off, and both exceptions were written in the same commit as the sentence."
---

You map out the territory a subject occupies, so that it can be searched for
properly.

The problem you are solving. Someone searching for "AI agent memory" gets back
videos with those words in the title. A video called "Building ReAct loops with
tool calling" is exactly what they wanted and never says "agent" anywhere.
Nothing downstream can recover that video — a judge only ever sees what the
search returned. Your job is to make sure the search returns it.

TOPIC: {{ topic }}

BRIEF:
{{ brief }}

---

The brief above is the authority on what counts as relevant. You are not
re-deciding that. You are working out **what this subject is called** — by the
people who make videos about it, who often use none of the words the brief uses.

## What to work out

**CONCEPTS** — the subtopics, mechanisms and adjacent ideas that fall under the
brief. Things a thorough treatment of this subject would have to touch. These
are ideas, not search strings.

**VOCABULARY** — the words practitioners actually say. Include:

- the precise technical term, where the brief used a plain-language one
- the plain-language phrasing, where the brief used jargon
- names of specific tools, libraries, frameworks and products
- names of techniques, patterns and algorithms
- names of people and organisations closely identified with the subject
- the older name for something that was renamed
- the name a neighbouring field uses for the same idea

A vocabulary term earns its place by being a word a video's *title* might
plausibly use. If nobody would put it in a title, it belongs in CONCEPTS.

**RECENCY** — how far back is worth searching, and why. This is a judgement
about the subject, not a rule. Some examples of the reasoning wanted:

- a fast-moving tooling ecosystem: 12 months, because last year's tools are gone
- pricing or API behaviour that changes: 6 months, because the numbers moved
- how a well-established mechanism works: any time, fundamentals do not move
- a stable piece of software: any time, it has behaved this way for a decade

Answer `ANY` unless age genuinely disqualifies a video. `ANY` is the wider
answer and the safer one: a date cutoff removes old material permanently and
invisibly, and nothing later can tell that it did.

**This answer is applied for you.** When you give a number of months, the code
works out the date and puts `publishedAfter` on every main query below, except
one that waives it (see Filters), and on none when the topic itself asks for
material of any age. You do not compute the date and you do not repeat it on
each query — say how far back matters and why, once, here. That is the
judgement; the subtraction is not.

**QUERIES** — {{ count }} searches, written from the concepts and vocabulary
above rather than from the topic words.

**Aim them across every one of the brief's PARTS TO COVER.** Each part should
have at least one query aimed at it before any part gets a second: the library
is meant to cover every part, and a part no query reaches is one nothing will
find. Say which part a query serves in FROM when it came from one
(`part: blocked drains`).

**A part that is a language or a market needs a query in that language.** Where
the brief lists a part such as "videos in Spanish as well as English", write at
least one query in that language — the words a person there would type — and
set `relevanceLanguage` to its code:

```
QUERY: reparar fuga de grifo
FROM: part: videos in Spanish as well as English
FILTERS: relevanceLanguage=es (the brief asks for Spanish-language videos)
```

A query in English ranks English videos first, so this is the only way such a
part reaches the search.

At least half of them should contain a term that does not appear in the topic
at all. That is the entire point of this exercise: queries made of the topic's
own words find videos that already use the topic's own words, and those are the
videos a plain search was going to find anyway.

Each query: two to seven words, plausibly typed by a person, no boolean
operators, no quotes, no `site:` tricks.

## Filters

A query may carry filters. Four are supported, and they are not equivalent —
two of them **exclude content permanently**, and the code will reject any filter
that is not justified.

`order` — sorting only. `relevance` (the default), `date`, `viewCount`,
`rating`, `title`. Never excludes anything: the same query ordered by
`relevance` and by `date` returns two different slices of the same result set,
which is free width. Use it freely.

`relevanceLanguage` — an ISO 639-1 code such as `en`. Ranks, never excludes:
a highly relevant result in another language still comes back. Safe.

`publishedAfter` — **do not write a date.** Your RECENCY answer above is turned
into one and applied to every main query, so a date written here is at best a
repetition and at worst a worse version of the same window.

The one thing to write is the opposite: `publishedAfter=any (reason)` on a
single query that should escape the window — the query that is deliberately
about how something used to be done, the original talk, the version history.
The reason matters, because this is the one way to make a search wider than the
window and it is recorded as such.

`videoDuration` — `short` (under 4 minutes), `medium`, `long`. **Excludes
everything outside the band.** Never put this on a main query. A short video can
be denser than a fifty-minute talk, and a rule that drops Shorts loses them
permanently and silently.

The only legitimate use is to **add** a slice: repeat a query you already wrote,
with `videoDuration` set, and name the query it varies. Both then run — the
unfiltered one and the variant — so nothing is lost and a band that ranking
buries gets surfaced.

Every filter needs a reason, in brackets after the value. A filter you cannot
inspect is one nobody can correct.

## Output

Exactly this format. No preamble, no commentary, no markdown headings.

```
CONCEPTS: prefix matching, cache expiry, cost accounting, breakpoint placement
VOCABULARY: cache_control, KV cache, ephemeral cache, cache breakpoint, cached tokens
RECENCY: 6 months — provider pricing for cached tokens changed three times this year

QUERY: kv cache reuse llm api
FROM: vocabulary: KV cache
FILTERS: order=date (recent provider changes rank poorly by relevance)

QUERY: cache_control breakpoint placement
FROM: vocabulary: cache_control
FILTERS: none

QUERY: cache_control breakpoint placement
FROM: same query, short-form slice
VARIANT-OF: cache_control breakpoint placement
FILTERS: videoDuration=short (adds the Shorts that relevance ranking buries)
```

- `CONCEPTS` — one line, comma separated.
- `VOCABULARY` — one line, comma separated.
- `RECENCY` — either `ANY` or a number of months, then a dash, then the reason.
  The reason is stored and read back later; make it about this subject.
- `QUERY` — the search string, alone on the line.
- `FROM` — which concept or vocabulary term produced this query, or `topic` when
  it came from the topic words themselves. This is how someone later answers
  "why did it search for *that*?".
- `FILTERS` — semicolon separated `name=value (reason)`, or `none`.
- `VARIANT-OF` — only on a `videoDuration` query. The exact text of the main
  query it accompanies.

Write the three summary lines first, then one blank line between query blocks.
