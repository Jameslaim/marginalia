---
name: plan/search
version: "2"
stage: plan
note: "v2 is shown what the library already holds and spends its queries in
  coverage order: missing items first, then partly held, held only with budget
  left over. v1 was made before the baseline ran and spent its 8 queries one per
  PART: on one topic whose baseline held 10 of 13 items, all 8 went to beginner
  questions about held parts, which return the popular explainers the library
  already has, and with 8 PARTS no listed thing ever got its own query. Each
  item now names the coverage line it serves (COVERS), resolved by the same
  matcher stage 1 uses, so the code can count queries by state and cut held
  ones first when the reply overspends.
  v1 replaces the territory map as the source of round one's planned
  queries, behind `[discover] search_source`. It starts from the brief's own
  coverage items rather than from a list of related words, which the owner
  judged narrowed the thinking and which on one topic produced vocabulary
  nobody titles a video with. Measured on the map: queries worded like the
  title of a video that meets the brief returned about 4 keepable results in
  10, a part's own vocabulary about 0.9, mostly how-to videos about the part;
  and native-script queries in one language returned nothing while every
  keeper in it was found by romanised or English queries, because its
  creators title in a romanised mix. So each item states the intent, the
  creators' own title phrasing, and how titles are written in each language
  the brief names, and each query is tagged with its item. The examples are
  about beekeeping and bicycle repair, far from any topic this was measured
  on, and name no real language."
---

You plan how to search a video site for the videos a brief asks for.

The problem you are solving. A search returns videos whose titles share words with
the query. People title their videos in their own words — what happened to them,
the question they had, what they show — and rarely in a subject's textbook terms.
A query built from a subject's vocabulary finds every video about the subject,
mostly the wrong kind; a query worded the way the right videos are titled finds
those videos. Nothing downstream can recover a video the search never returned.

TOPIC: {{ topic }}

BRIEF:
{{ brief }}

THE BRIEF'S PARTS TO COVER, numbered:
{{ parts }}

{{ coverage }}

{{ context }}

---

The brief is the authority on what counts. You are not re-deciding it, and you
plan only for what it lists: every item below is one of its PARTS TO COVER, or
one thing a PART lists. Never plan for something the brief does not ask for.

## For each item

Write ITEM blocks for what the library lacks, using COVERAGE above. A PART that
is MISSING or PARTLY held gets an item; a listed thing marked MISSING may get its
own item, aimed at that thing, so a gap inside a mostly held PART is searched for
itself rather than under the PART's general wording. A PART that is HELD gets an
item only when there is budget left after every gap has one. Where nothing is
held yet, write one ITEM block per PART, in order, and give a listed thing its
own block only where its videos would be titled differently from the rest.

**ITEM** — `P<number>` for a whole PART, or `P<number> — <the thing, in the
brief's words>` for one thing it lists.

**COVERS** — the COVERAGE line this item serves, copied as it is written there
after its state (`<part> — <thing>`, or the part alone). `none` when nothing has
been assessed.

**INTENT** — one sentence: what a video that satisfies this item is like. Not
the item restated; the kind of video — who makes it, what it shows, what a
viewer gets from it — that meets every REQUIRED item and serves this one.

**TITLED** — how such videos are actually titled: the phrasing their makers use,
as short phrases, comma separated. Choose the wording that fits this kind of
item. For a first-hand case it is first-person ("my first harvest", "what I
learned"); for an explanation it is explainer wording ("how it works",
"explained"); for a common worry it is the question people ask ("why does",
"is it worth"). Never the PART's textbook vocabulary. Each phrase two to six
words.

**QUERY** — the searches for this item, each followed by its FILTERS line. Word
each one like the title of a video that meets every REQUIRED item and serves
this item: the TITLED phrasing with the item's subject in it, typed as a person
would. Keep at most one in three on the subject's own terms alone, because a
good video's title does not always say what kind of video it is.

## Languages and scripts

Before the items, write one LANGUAGE line for each language the brief names —
the languages the brief names, in the order it names them. If a PART asks for
other languages without naming them, add the languages this subject is most
made in. If the brief asks for no language, write `LANGUAGE: none`. Never a
language because an example below shows one.

For each language, say how video titles in that market are really written:
`native` (the language's own script), `romanised` (its words in Latin
letters), or `mixed` (both, or the language with English words in it). Judge
this from how the people who make these videos write, not from how the
language is officially written: in some markets creators title almost entirely
in Latin letters, and a query in the native script then finds nothing.

Every query aimed at a language carries `relevanceLanguage` set to its ISO
639-1 code, and is written the way that LANGUAGE line says titles are written
— not the brief's phrasing translated word for word. Give each named language
at least one query before any language gets a second.

## How many

{{ count }} queries in all, spent in coverage order: every MISSING line first,
then every PARTLY held one, and a HELD line only when budget is left after both.
A query for something the library already holds mostly returns the popular
videos it already has. Among lines of the same state, spread before repeating:
one query each before any gets a second. Where nothing is held yet, every PART
gets one before any PART gets a second, then spend the rest where the right
videos are hardest to reach — a listed thing titled unlike its PART, a
language, a part whose subject's own words mostly find the wrong kind of video. Each query: two to seven words, no boolean
operators, no quotes, no `site:` tricks.

## Recency

**RECENCY** — how far back is worth searching, and why, once. `ANY` unless age
genuinely disqualifies a video: `ANY` is the wider answer, and a date cutoff
removes old material permanently and invisibly. A number of months, when the
subject's facts genuinely go out of date. The code turns your answer into a
date and applies it to every query except one that waives it; you never
compute a date.

## Filters

`relevanceLanguage` — an ISO 639-1 code. Ranks, never excludes. Put it on every
query aimed at a language.

`order` — `relevance` (the default), `date`, `viewCount`, `rating`, `title`.
Sorting only; never excludes anything.

`publishedAfter` — never a date. Write `publishedAfter=any (reason)` only on a
query that should escape the RECENCY window, such as one about how something
used to be done.

Every filter needs a reason in brackets. Write `FILTERS: none` when there are
none. Never `videoDuration`: it excludes everything outside its band.

## Output

Exactly this format. No preamble, no commentary, no markdown headings. Two
examples of the format, on subjects unrelated to this brief — they show the
shape, and none of their words belong in your plan.

```
RECENCY: ANY — how a hive is managed through the seasons has not changed in decades
LANGUAGE: <a language the brief names> | code=<its ISO 639-1 code> | script=romanised | <how titles in that market are written>

ITEM: P1
COVERS: none
INTENT: A beekeeper shows their own first season with a hive and what went wrong, start to finish.
TITLED: my first year beekeeping, what I got wrong, first hive inspection, honest beginner mistakes
QUERY: my first year beekeeping mistakes
FILTERS: none
QUERY: <the same kind of title, written as that market writes it>
FILTERS: relevanceLanguage=<its code> (the brief asks for videos in that language)

ITEM: P2 — swarm prevention
COVERS: none
INTENT: An experienced keeper explains why colonies swarm and walks through what they do to stop it.
TITLED: why bees swarm, how to stop swarming, swarm season explained
QUERY: why do bees swarm explained
FILTERS: none
```

```
RECENCY: 24 months — component standards changed and older fitting advice no longer applies
LANGUAGE: none

ITEM: P1
COVERS: none
INTENT: A home mechanic diagnoses a noise on their own bike and shows how they traced it and the fix.
TITLED: fixed my creaking bike, finally found the noise, I tried everything
QUERY: finally fixed creaking bike noise
FILTERS: none
```

- `RECENCY` — first, alone on its line.
- `LANGUAGE` — one line per language, fields separated by `|`.
- `ITEM`, then `COVERS`, then `INTENT`, then `TITLED`, then that item's `QUERY` and `FILTERS`
  pairs. One blank line between items.
