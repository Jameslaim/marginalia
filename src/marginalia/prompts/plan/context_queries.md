---
name: plan/context_queries
version: "1"
stage: plan
note: "v1. Writes the few web searches a run makes before it plans (step 4),
  aimed at what the library lacks. Measured on a live round-one comparison: one
  topic's missing items found no keeper from either source, because the brief
  and the coverage say what is wanted and not the words today's creators use
  for it. These searches look those words up. The code checks every line: the
  aim must be one of the numbered gaps, GENERAL at most once, or a MARKET the
  brief names; the order of the gaps is kept; the count is capped. Examples are
  about home brewing, far from any topic this was measured on."
---

You write a few web searches whose results will tell a video search planner how
a subject is named and titled today: current program and product names, terms
people use, who makes such videos, and which markets and languages they come
from. You are not searching for videos.

TOPIC: {{ topic }}

WHAT THE BRIEF WANTS:
{{ intent }}

WHAT THE BRIEF EXCLUDES:
{{ excluded }}

THE GAPS, most important first:
{{ gaps }}

---

Write at most {{ count }} searches, in the order of the gaps: the first gaps
first. Each search finds pages that would name the gap's subject the way the
people who make videos about it name it — a program's current name, a term
of art, a common phrasing. At most one search may be GENERAL: how this subject
as a whole is named and titled today. A search may be a MARKET search instead,
naming one of the languages or markets the brief itself names, to learn how
that market names the subject.

Never write a search aimed at anything the brief excludes.

Each search: 3 to 12 words, plain words a person would type into a web search,
no quotes, no operators.

Exactly this format, one line each, nothing else:

```
QUERY: G1 | what new home brewers call a stuck fermentation
QUERY: G2 | homebrew kit names sold this year
QUERY: GENERAL | how home brewing videos are titled
QUERY: MARKET <a market the brief names> | home brewing terms used in that market
```
