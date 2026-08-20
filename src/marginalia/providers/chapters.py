"""Chapters, read out of a description before anything is downloaded.

A creator who writes chapters puts them in the description as timestamped
lines::

    00:00 Intro
    04:20 Setting up your key
    14:20 cache_control breakpoints

That is a table of contents — the closest thing to a summary that exists before
a single byte of transcript is fetched — and stage 1 was throwing it away and
judging from the raw blob instead. yt-dlp already parses chapters, but only
*after* the download, which is exactly the expensive step this is meant to
inform.

**Here rather than in `brain/` because it is provider normalisation.** Both
search providers hand back a description and both must produce the same
`SearchResult`, in the same way that `youtube_api.py` unescapes titles yt-dlp
leaves alone. A judge receives chapters; it never learns where they came from.

The hard part is not the parsing but the *refusing*. Descriptions are full of
things that look like timestamps — a runtime, a citation, "we go live at 8:00"
— and a false chapter list is worse than none, because it reads as a confident
table of contents for a video that never had one. So three rules, all of which
must hold before a single line is treated as a chapter:

1. **Several in a row.** One timestamped line is a mention; four are a listing.
2. **Starting at or near zero.** A real table of contents begins at the
   beginning — YouTube itself will not render one otherwise. A run starting at
   4:20 is a reference to a moment, not a description of the whole.
3. **Ascending.** A table of contents goes forwards.
"""

from __future__ import annotations

import re

__all__ = ["parse_description_chapters", "MIN_CHAPTERS", "NEAR_ZERO_MS"]

# One mention is not a listing. Four is YouTube's own floor for rendering
# chapters (a first at 0:00 plus three more); matching it means we agree with
# the thing the creator was actually writing for.
MIN_CHAPTERS = 4

# "At or near zero". YouTube demands exactly 0:00, which is stricter than
# creators are — `00:01 Intro` is common and obviously a chapter list. Half a
# minute is loose enough for that and nowhere near loose enough to admit a run
# that starts mid-video.
NEAR_ZERO_MS = 30_000

# A leading timestamp, optionally wrapped, optionally followed by a separator.
#
# Anchored at the start on purpose: YouTube reads chapters from the front of the
# line, and a trailing timestamp ("...more on this at 4:20") is a reference to a
# moment rather than the name of one. The colon is what keeps `1. Introduction`
# and `2024 was a big year` out — a bare number is never a timestamp here.
_LINE = re.compile(
    r"""^\s*
        [\[(<]?\s*                          # optional bracket
        (?:(?P<h>\d{1,2}):)?                # optional hours
        (?P<m>\d{1,2}):(?P<s>[0-5]\d)       # minutes:seconds, seconds real
        \s*[\])>]?                          # closing bracket
        (?P<sep>\s*[-–—:|·•]+\s*|\s+)       # separator, or just space
        (?P<title>\S.*?)\s*$                # a title, and it must exist
    """,
    re.VERBOSE,
)

# `00:00 - 01:45 Intro`. The separator is already eaten by `_LINE`, so what is
# left on the front of the title is the *end* of a range. Stripped only when it
# is actually later than the start — a chapter genuinely titled "3:15 explained"
# keeps its name, and only a real range is trimmed.
_RANGE_END = re.compile(
    r"""^(?:(?P<h>\d{1,2}):)?(?P<m>\d{1,2}):(?P<s>[0-5]\d)
        \s*[-–—:|·•]*\s*(?P<rest>.*)$""",
    re.VERBOSE,
)


def _timestamp_ms(match: re.Match[str]) -> int:
    hours = int(match.group("h") or 0)
    minutes = int(match.group("m"))
    seconds = int(match.group("s"))
    return ((hours * 60 + minutes) * 60 + seconds) * 1000


def parse_description_chapters(description: str | None) -> list[dict]:
    """Chapters as ``[{"start_ms": int, "title": str}]``, or ``[]``.

    The shape matches `TranscriptResult.chapters`, so stage 1 and stage 2 read
    chapters the same way and nothing downstream needs to know which produced
    them.

    Returns ``[]`` for anything it is not confident about, and that is the
    intended bias: a missing table of contents costs the judge some context it
    never had before, while a fabricated one is a confident lie about a video's
    structure.
    """
    if not description:
        return []

    runs: list[list[dict]] = []
    current: list[dict] = []

    for raw in description.splitlines():
        line = raw.strip()
        if not line:
            # A blank line inside a listing is formatting, not a break. A
            # blank line is the one thing that does not end a run.
            continue

        match = _LINE.match(line)
        if match is None:
            if current:
                runs.append(current)
                current = []
            continue

        start_ms = _timestamp_ms(match)
        title = " ".join(match.group("title").split())

        range_end = _RANGE_END.match(title)
        if range_end is not None and _timestamp_ms(range_end) > start_ms:
            title = " ".join(range_end.group("rest").split())

        if not title:
            if current:
                runs.append(current)
                current = []
            continue

        current.append({"start_ms": start_ms, "title": title})

    if current:
        runs.append(current)

    for run in runs:
        if _is_a_listing(run):
            return run
    return []


def _is_a_listing(run: list[dict]) -> bool:
    """All three rules, in the order that rejects fastest."""
    if len(run) < MIN_CHAPTERS:
        return False
    if run[0]["start_ms"] > NEAR_ZERO_MS:
        return False
    times = [chapter["start_ms"] for chapter in run]
    return all(earlier < later for earlier, later in zip(times, times[1:]))
