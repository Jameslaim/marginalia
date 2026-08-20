"""Every structure that crosses a module boundary.

One place, so that a boundary is a thing you can read rather than infer. If a
type is defined where it is produced, then `flow/` importing `brain/` to name a
verdict and `brain/` importing `providers/` to name a search result is how a
layered package quietly becomes a knot — each import individually reasonable,
the graph as a whole cyclic.

Everything here is data. Behaviour lives with the module that owns it, and the
methods that did come along are the ones that read a structure rather than act
on one: `Brief.intent` picks a line out of text it already holds,
`VideoRow.from_sqlite` and `note_is_current` read a row.

**Protocols and exception types stay where they are.** `TranscriptProvider`,
`SearchProvider`, `PermanentError` and `TransientError` remain in
`providers/base.py`, which imports its data types from here. A Protocol is the
shape of an implementation, not a structure in flight, and it belongs beside the
implementations it constrains.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, fields

__all__ = [
    "TranscriptLine",
    "TranscriptResult",
    "SearchResult",
    "Brief",
    "MetadataVerdict",
    "OpeningVerdict",
    "VideoRow",
    "RoundReport",
    "DiscoverRun",
]


# -- transcripts ------------------------------------------------------------


@dataclass
class TranscriptLine:
    start_ms: int
    end_ms: int
    text: str


@dataclass
class TranscriptResult:
    lines: list[TranscriptLine]
    language: str
    source: str  # "captions"
    title: str
    channel: str
    duration_s: int
    upload_date: str | None
    chapters: list[dict] = field(default_factory=list)  # [{"start_ms": int, "title": str}]
    # Which caption track was used: "manual" (author-written) or "auto"
    # (machine-generated). Recorded because manual captions are materially
    # better and a later upgrade pass needs to know which it has.
    caption_kind: str = "manual"


# -- search -----------------------------------------------------------------


@dataclass
class SearchResult:
    """One candidate video, as a search provider described it.

    Everything here is cheap metadata — the material stage 1 of relevance
    judging reads (M2b). No transcript text ever passes through this type.

    ``duration_s``, ``published_at`` and ``view_count`` are optional because
    providers differ in what they return: YouTube's ``search.list`` omits all
    three, and yt-dlp's flat extraction omits some.
    """

    video_id: str
    title: str
    channel: str
    description: str
    duration_s: int | None
    published_at: str | None
    view_count: int | None
    # Which provider produced this result. Set by the provider itself; the
    # report names it, and a mixed-provider result set must stay attributable.
    provider: str = ""


# -- judgement --------------------------------------------------------------


@dataclass(frozen=True)
class Brief:
    """The explicit statement of what is wanted, and the prompts behind it."""

    topic: str
    text: str
    version: str

    @property
    def intent(self) -> str:
        """The INTENT section on one line, for the report."""
        lines = self.text.splitlines()
        for index, line in enumerate(lines):
            if line.strip().upper().startswith("INTENT"):
                body = line.partition(":")[2].strip()
                if body:
                    return " ".join(body.split())
                for follower in lines[index + 1 :]:
                    if follower.strip():
                        return " ".join(follower.split())
        first = next((line for line in lines if line.strip()), "")
        return " ".join(first.split())


@dataclass(frozen=True)
class MetadataVerdict:
    video_id: str
    verdict: str
    reason: str
    parsed: bool = True


@dataclass(frozen=True)
class OpeningVerdict:
    video_id: str
    verdict: str
    reason: str
    concepts: list[str] = field(default_factory=list)
    parsed: bool = True


# -- ledger rows ------------------------------------------------------------


@dataclass(frozen=True)
class VideoRow:
    video_id: str
    status: str
    source: str | None
    caption_kind: str | None
    transcribed_at: str | None
    content_hash: str | None
    duplicate_of: str | None
    first_seen: str
    last_updated: str
    last_accessed: str | None
    attempts: int
    fail_kind: str | None
    provider: str | None
    note_version: str | None = None
    note_model: str | None = None
    noted_at: str | None = None

    @classmethod
    def from_sqlite(cls, row: sqlite3.Row) -> VideoRow:
        return cls(**{field.name: row[field.name] for field in fields(cls)})

    def note_is_current(self, version: str) -> bool:
        """Does this video carry a note written by the current prompt?

        Both halves matter. ``noted_at`` alone says a note was written once;
        ``note_version`` says which prompt wrote it, and that is what turns
        "which notes are stale?" into a query rather than a list somebody
        maintains by hand.
        """
        return self.noted_at is not None and self.note_version == version


# -- discovery runs ---------------------------------------------------------


@dataclass
class RoundReport:
    number: int
    queries: list[str]
    candidates: int = 0
    known: int = 0
    rejected_before: int = 0
    verdict_no: int = 0
    verdict_maybe: int = 0
    verdict_yes: int = 0
    flipped_to_no: int = 0
    stored: int = 0
    failed: int = 0
    concepts: list[str] = field(default_factory=list)
    from_threads: bool = False


@dataclass
class DiscoverRun:
    run_id: str
    topic: str
    brief: Brief
    model: str
    rounds: list[RoundReport] = field(default_factory=list)
    stored_ids: list[str] = field(default_factory=list)
    stopped_because: str = ""
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    dry_run: bool = False

    cost_usd: float = 0.0

    @property
    def candidates_seen(self) -> int:
        return sum(report.candidates for report in self.rounds)

    @property
    def judged(self) -> int:
        return sum(
            report.verdict_no + report.verdict_maybe + report.verdict_yes
            for report in self.rounds
        )
