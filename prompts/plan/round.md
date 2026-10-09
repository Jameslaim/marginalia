---
name: plan_round
version: "4"
stage: plan
note: "v4 makes two statements true. Only KEPT satisfying anything was
  written before the library baseline existed, and contradicted the line
  that lets ALREADY IN THE LIBRARY satisfy an item once the code added it.
  The date window going on every query was never true, since a waiver or a
  topic asking for material of any age keeps it off."
---

You have just finished one round of searching for videos against a research
brief. Decide what the next round should look for.

BRIEF:
{{ brief }}

{{ territory }}

{{ round }}

Rounds remaining after this one: {{ rounds_left }}

---

## The one rule that matters most

Everything under KEPT and REJECTED above is **another model's judgement**, not a
fact about the world. A rejection reason says what a judge decided about a video.
It does not say what exists on YouTube, and it does not say what is findable.

**The brief is the authority.**

Your primary question is: *what does the brief ask for that has not been found?*

It is **not**: *why do I keep rejecting things?*

This distinction has a cost attached, and it is not hypothetical. In an earlier
version of this system the judge had a bias against short videos, and twelve of
twenty-five rejections in one round cited runtime. A planner reading that would
conclude "short videos keep getting rejected, so stop looking for them" — which
sounds like learning, and would have permanently removed an entire class of
video from every future search because of a bug in a different component.

**Consistency is what a systematic error looks like from the outside.** If a
striking share of the rejections share one reason, that is more likely to be
information about the judge than about the subject. Say so if you see it, in the
RECOMMENDATION reason. Do not redesign the search around it.

In particular, never propose a search strategy based on video length, view
count, channel, or production quality. Those are not properties the brief asks
about, and a filter on any of them removes content permanently and invisibly.

## Read what was kept, not only what was rejected

Coverage is a claim about what the library now holds against the brief, and you
cannot make it from failures. Work through the brief's MUST CONTAIN and ALSO
WANTED items one at a time and ask, for each: *did anything kept this round
actually satisfy this?*

- A part satisfied by a kept video is `YES`, and name the video.
- A part listed under ALREADY IN THE LIBRARY BEFORE THIS RUN is `YES` too, and
  say which note covers it.
- A part with nothing against either is `NO`, and say what is missing.

**The library is older than this run, and most of it is invisible above.** Every
list you are shown covers only what this run newly fetched. Videos the library
already held were dropped before they could become candidates — that is the gate
working, not an absence — so they appear under ALREADY IN THE LIBRARY and
nowhere else. Treat that section as evidence of exactly the same weight as KEPT.

Marking one of those a gap is the specific mistake this section exists to
prevent: a real run declared retrieval scoring uncovered while a note written the
previous day held the exact formula, and the next round spent five of its six
queries finding it a second time.

**Only videos under KEPT can satisfy anything this run found**, alongside the
parts of the brief under ALREADY IN THE LIBRARY. A video under KEPT BUT NOT
ANSWERABLE was stored and will never be noted — either its transcript was too
damaged to read, or it is past the note worker's length ceiling — so no question
will ever be answered from it. The two causes differ and the consequence does
not. Its title may look like exactly what a part of the brief asked for, and
that is precisely the trap: the title is not the content, and here no content
will ever be available. Treat that part of the brief as still unmet, and if the
same subject has nothing answerable against it, say so in the gap and search for
it again.

A gap must be **specific**. "More depth needed" is not a gap. "Nothing found
that shows cache expiry or TTL configuration" is a gap — it names a thing that
was asked for and was not found, which is something a query can be written from.

## Writing the next queries

Write queries aimed at the gaps you just named. Each one should be traceable:
FROM says which gap or which unexplained concept it came from.

Sources worth drawing on, in rough order of value:

1. **A named gap** — the part of the brief nothing has satisfied.
2. **Concepts mentioned but unexplained** in kept videos. A kept video that says
   "we use a scratchpad for this" and moves on has written a query for you.
3. **Vocabulary in the titles of videos that were kept.** What the good ones
   call themselves is what other good ones are likely to call themselves.
4. **A query that returned nothing, rephrased.** Zero results is a fact about
   the phrasing, not about the subject.

Do not simply restate the brief. Round one already searched the brief's own
words and the territory map's; a query indistinguishable from those has already
been run and will be discarded as a duplicate.

Each query: two to seven words, plausibly typed by a person, no operators.

### Filters

Same four as before, and the same rules. `order` sorts and is free to use.
`relevanceLanguage` ranks and is safe.

**`publishedAfter` is applied for you.** When the map reasoned a window, the
code derives the date and puts it on every query you write here, unless the
query waives it or the user's topic asks for material of any age. Do not
compute one. The single thing worth writing is `publishedAfter=any (reason)`,
on a query that should escape the window — a gap that is specifically about
earlier practice, where the window is what has been hiding the answer. Give the reason:
this is the one way to search wider than the map decided.

**`videoDuration` is not available to you.** It excludes by length, which is
exactly the redirect the rule above forbids. It will be refused.

Every filter needs a reason in brackets.

## Revising the map

The territory map was written before anything was searched, so it can be wrong.
You may propose changes.

**Additions are cheap.** A concept the map missed, a tool or technique named in
the kept videos, an alternative term the good results keep using. Propose these
freely — the worst case is one query whose results are visible.

**Removals are not.** Removing a term narrows every future search, and a class
of video that stops being searched for is one nobody will notice the absence of.
So a removal must name the specific queries that carried the term and what came
back. Quote the query text exactly as it appears under QUERIES RUN. A removal
without that evidence will be refused, and at most three are accepted per round.

"This term feels off topic" is not evidence. "The query `semantic caching
vector db` returned ten results and every one was rejected as a different
subject" is.

## Recommending a stop

Say `STOP` when the brief's MUST CONTAIN items are covered by what has been kept
and further searching would be looking for something already found.

Say `CONTINUE` when named gaps remain.

Be honest in both directions. Stopping early because a round was quiet leaves
the brief unsatisfied and looks like success; continuing with nothing specific
to look for spends money on rephrasings. The budget stops the run on its own —
you are saying whether the *brief* is satisfied, not whether the run is long
enough.

## Output

Exactly this format. No preamble, no commentary, no markdown headings.

```
COVERAGE: shows API configuration or code | YES | two kept videos walk through cache_control breakpoints
COVERAGE: cache expiry and TTL behaviour | NO | nothing kept mentions expiry at all
COVERAGE: comparison across providers | NO | every kept video covers one provider only

REVISE: add vocabulary: TTL
WHY: three kept videos use this term and the map does not have it

REVISE: remove vocabulary: semantic cache
WHY: it is pulling in a different subject entirely
EVIDENCE: prompt caching vs semantic caching returned 10 results, all rejected at stage 1

QUERY: prompt cache ttl expiry
FROM: gap — cache expiry and TTL behaviour
FILTERS: none

QUERY: anthropic vs openai caching compared
FROM: gap — comparison across providers
FILTERS: order=date (provider behaviour changed this year)

RECOMMENDATION: CONTINUE — two MUST CONTAIN items still have nothing against them
```

- `COVERAGE` — one line per part of the brief you assessed, in the form
  `part | YES or NO | detail`. At least three lines. The detail is read by a
  person later; make it specific.
- `REVISE` — `add` or `remove`, then `concept` or `vocabulary`, then the term.
  `WHY` on the next line. `EVIDENCE` as well, for every removal.
- `QUERY` — the search string alone. `FROM` names the gap or concept behind it.
  `FILTERS` is `none` or semicolon-separated `name=value (reason)`.
- `RECOMMENDATION` — `CONTINUE` or `STOP`, a dash, then one sentence.

Write up to {{ count }} queries. Fewer is fine if there are fewer real gaps —
padding the list with rephrasings of one idea wastes a round.
