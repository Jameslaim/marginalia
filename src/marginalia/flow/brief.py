"""A topic's approved brief: written once, confirmed by its owner, reused by every run.

Every `marg discover` used to write a fresh brief, so the PARTS and the things
they list changed from run to run of one topic: one run's harness part listed
routing and retries, the next fresh brief for the same topic named neither.
The baseline, the planner and gap-first fetching (rule 37) then judged "held"
and "missing" against whichever brief the run drew — coverage across runs of a
topic measured against a moving target, and things never in the owner's topic
chased as gaps.

So a brief can be written to review (`write_pending`), edited by hand, and
approved (`approve`); discovery then reuses the latest approved version. The
text is checked before it is stored (rule 30), stored in the ledger with its
digest and a version (rule 23), and every file here is written by `marg`
(rule 2).
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace

from ..brain.judge import Judge, brief_check, brief_parts, brief_sha256, normalise_brief
from ..brain.territory import parse_recency
from ..config import Config
from ..contracts import RECENCY_OWNER, Brief, BriefApproval, BriefStatus, PendingBrief
from ..core.ledger import Ledger, RecencyRow, normalise_topic
from ..core.ledger import now as ledger_now
from ..core.library import Library

log = logging.getLogger(__name__)

__all__ = [
    "BriefNotApproved",
    "BriefPendingExists",
    "approve",
    "brief_status",
    "run_brief",
    "write_pending",
]


class BriefNotApproved(RuntimeError):
    """`[discover] require_approved_brief` is on and the topic has none."""

    def __init__(self, topic: str) -> None:
        self.topic = topic
        quoted = json.dumps(topic, ensure_ascii=False)
        super().__init__(
            f"no approved brief for {quoted}, and [discover] require_approved_brief is on. "
            f"Write one with: marg brief {quoted} — edit the file it names — then "
            f"marg brief {quoted} --approve. Nothing was searched or recorded."
        )


def run_brief(config: Config, ledger: Ledger, judge: Judge, topic: str) -> Brief:
    """The brief a discovery run on ``topic`` uses.

    The topic's latest approved version when one exists — no write and no
    review call, and the same text every run on the topic is measured against.
    Otherwise a fresh brief, written and reviewed as it always was, unless
    `[discover] require_approved_brief` says to stop instead. Either way the
    text's digest travels on the brief, so the run row records it.
    """
    approved = ledger.approved_brief(topic)
    if approved is not None:
        log.info("brief: reusing approved brief v%d for %r", approved.version, approved.topic)
        return Brief(
            topic=topic,
            text=approved.brief,
            version=approved.prompt_version or "approved",
            approved_version=approved.version,
            sha256=approved.sha256,
        )
    if config.discover.require_approved_brief:
        raise BriefNotApproved(normalise_topic(topic))
    brief = judge.write_brief(topic, max_tokens=config.discover.brief_max_tokens)
    return replace(brief, sha256=brief_sha256(brief.text))


class BriefPendingExists(RuntimeError):
    """A pending brief is already on disk, and may carry the owner's edits."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(
            f"a pending brief already exists at {path}; approve it with --approve, or "
            "write a new one over it with --force (its edits are lost)"
        )


def write_pending(
    config: Config, library: Library, judge: Judge, topic: str, *, force: bool = False
) -> PendingBrief:
    """Write a brief for ``topic`` with the usual write and review, and save it
    as pending. Two model calls and no search. Refuses to replace a pending
    file unless ``force``: that file may hold an owner's edits."""
    key = normalise_topic(topic)
    path = library.pending_brief_path(key)
    if path.exists() and not force:
        raise BriefPendingExists(str(path))
    brief = judge.write_brief(topic, max_tokens=config.discover.brief_max_tokens)
    library.write_brief_file(path, brief.text)
    library.write_brief_file(
        library.pending_brief_meta_path(key),
        json.dumps(
            {
                "topic": key,
                "prompt_version": brief.version,
                "written_at": ledger_now(),
                "sha256": brief_sha256(brief.text),
                "outcome": brief.outcome,
            },
            ensure_ascii=False,
            indent=1,
        ),
    )
    log.info("brief: pending brief written for %r at %s", key, path)
    return PendingBrief(
        topic=key,
        path=str(path),
        text=brief.text,
        version=brief.version,
        parts=brief_parts(brief.text),
        draft_problems=brief.draft_problems,
        review_problem=brief.review_problem,
    )


def approve(ledger: Ledger, library: Library, topic: str) -> BriefApproval:
    """Check the pending brief and store it as the topic's next approved version.

    The text is checked first — the sections every brief needs, and rule 27's
    limits — and a text that fails is refused with the check that failed,
    storing nothing and leaving the pending file as it is. A text identical to
    the latest approved version makes no new version.
    """
    key = normalise_topic(topic)
    path = library.pending_brief_path(key)
    if not path.is_file():
        return BriefApproval(
            key, str(path), problem="no pending brief for this topic; write one with marg brief"
        )
    text = path.read_text(encoding="utf-8")
    problem = brief_check(text)
    if problem:
        log.warning("brief: approval refused for %r: %s", key, problem)
        return BriefApproval(key, str(path), problem=problem)
    sha256 = brief_sha256(text)
    latest = ledger.approved_brief(key)
    if latest is not None and latest.sha256 == sha256:
        return BriefApproval(
            key, str(path), version=latest.version, sha256=sha256, unchanged=True,
            approved_path=str(library.approved_brief_path(key, latest.version)),
        )
    stored = normalise_brief(text)
    row = ledger.approve_brief(key, stored, sha256, prompt_version=_prompt_version(library, key))
    copy = library.write_brief_file(library.approved_brief_path(key, row.version), stored)
    return BriefApproval(key, str(path), version=row.version, sha256=sha256, approved_path=str(copy))


def brief_status(ledger: Ledger, library: Library, topic: str) -> BriefStatus:
    key = normalise_topic(topic)
    path = library.pending_brief_path(key)
    versions = ledger.approved_briefs(key)
    pending = path.is_file()
    differs = pending and (
        not versions or brief_sha256(path.read_text(encoding="utf-8")) != versions[-1].sha256
    )
    return BriefStatus(
        topic=key,
        pending_path=str(path),
        pending=pending,
        pending_differs=differs,
        versions=[(row.version, row.sha256, row.approved_at) for row in versions],
    )


def _prompt_version(library: Library, key: str) -> str | None:
    """The write/review prompts the pending brief came from, if recorded."""
    try:
        meta = json.loads(library.pending_brief_meta_path(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = meta.get("prompt_version")
    return str(version) if version else None


OWNER_RECENCY_REASON = "set by the owner"


def set_owner_recency(ledger: Ledger, topic: str, value: str) -> tuple[RecencyRow | None, str]:
    """Record the owner's recency for the topic's latest approved brief version.

    ``value`` is ``ANY`` (or ``any time``), ``N months`` or ``N years``, read by
    the same parser as a model's RECENCY line and held to the same limits
    (rule 30): an unreadable value or one outside the usable window is refused
    with the reason, and nothing is stored. ``(row, problem)``.
    """
    approved = ledger.approved_brief(topic)
    if approved is None:
        return None, "no approved brief for this topic; approve one first (marg brief --approve)"
    text = " ".join(value.split())
    if text.lower().startswith("last "):
        text = text[5:]
    if not text:
        return None, "an empty recency; give ANY, N months or N years"
    recency, problem = parse_recency(f"{text} — {OWNER_RECENCY_REASON}")
    if problem:
        return None, f"{problem} ({value!r}); give ANY, N months or N years"
    row = ledger.set_brief_recency(
        approved.topic, approved.version, months=recency.months, reason=OWNER_RECENCY_REASON,
        samples=(), source=RECENCY_OWNER,
    )
    return row, ""
