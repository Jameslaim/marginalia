"""One acquisition path: `flow/acquire.py`, and every caller of it.

Ingest, discovery and refetch each carried their own fetch, with three
policies: ingest walked `providers.transcript_order`, the other two used its
first entry and recorded that entry as provenance whoever answered; discovery
counted a captionless video as a fetch failure; a typo in a provider name
condemned the video for good; an unclassified provider error left a `pending`
row that nothing retried. These tests pin the one policy, and pin it through
each production caller rather than only the helper.

Synthetic data and temp libraries only; providers are fakes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import make_config
from marginalia.config import ConfigError
from marginalia.contracts import (
    ACQUIRE_FAILED_PERMANENT,
    ACQUIRE_FAILED_TRANSIENT,
    ACQUIRE_NO_CAPTIONS,
    ACQUIRED,
    RECOVERED,
    STATUS_FAILED,
    STATUS_HAS_TRANSCRIPT,
    STATUS_NEEDS_ASR,
    STATUS_PENDING,
    TranscriptLine,
    TranscriptResult,
)
from marginalia.core.ledger import FAIL_PERMANENT, FAIL_TRANSIENT, Ledger
from marginalia.core.library import Library
from marginalia.flow.acquire import acquire, transcript_providers
from marginalia.providers.base import PermanentError, TransientError

A = "aaaaaaaaaaa"


def _result() -> TranscriptResult:
    return TranscriptResult(
        lines=[TranscriptLine(0, 2000, "a synthetic line")],
        language="en", source="captions", title="a title", channel="c",
        duration_s=60, upload_date="20260101", caption_kind="auto",
    )


class Scripted:
    def __init__(self, name: str, outcome) -> None:
        self.name = name
        self.outcome = outcome
        self.asked = 0

    def fetch(self, video_id: str):
        self.asked += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


@pytest.fixture()
def ledger(tmp_path: Path):
    with Ledger(tmp_path / "ledger.db") as ledger:
        yield ledger


# -- the policy ---------------------------------------------------------------


def test_the_chain_is_walked_and_the_answering_provider_is_named(ledger) -> None:
    first = Scripted("first", TransientError("throttled"))
    second = Scripted("second", _result())

    got = acquire(ledger, A, [first, second])

    assert got.outcome == ACQUIRED and got.provider == "second"
    assert ledger.get(A).status == STATUS_PENDING, "the caller stores; the row waits"


def test_a_permanent_failure_ends_the_walk(ledger) -> None:
    first = Scripted("first", PermanentError("video removed"))
    second = Scripted("second", _result())

    got = acquire(ledger, A, [first, second])

    assert got.outcome == ACQUIRE_FAILED_PERMANENT
    assert second.asked == 0
    row = ledger.get(A)
    assert (row.status, row.fail_kind, row.provider) == (STATUS_FAILED, FAIL_PERMANENT, "first")


def test_a_transient_failure_outranks_no_captions(ledger) -> None:
    got = acquire(ledger, A, [Scripted("a", None), Scripted("b", TransientError("timeout"))])

    assert got.outcome == ACQUIRE_FAILED_TRANSIENT
    assert (ledger.get(A).status, ledger.get(A).fail_kind) == (STATUS_FAILED, FAIL_TRANSIENT)


def test_no_captions_everywhere_is_a_route(ledger) -> None:
    got = acquire(ledger, A, [Scripted("a", None)])

    assert got.outcome == ACQUIRE_NO_CAPTIONS
    assert ledger.get(A).status == STATUS_NEEDS_ASR


def test_an_unclassified_provider_error_is_retryable_not_a_stranded_pending(ledger) -> None:
    """It used to leave `pending` — the crash marker — which nothing retried."""
    got = acquire(ledger, A, [Scripted("buggy", RuntimeError("provider bug with text"))])

    assert got.outcome == ACQUIRE_FAILED_TRANSIENT
    assert "RuntimeError" in got.detail and "provider bug" not in got.detail
    assert (ledger.get(A).status, ledger.get(A).fail_kind) == (STATUS_FAILED, FAIL_TRANSIENT)


def test_an_interrupt_still_leaves_pending(ledger) -> None:
    """A real interruption is what `pending` exists to record (rule 6)."""
    with pytest.raises(KeyboardInterrupt):
        acquire(ledger, A, [Scripted("a", KeyboardInterrupt())])
    assert ledger.get(A).status == STATUS_PENDING


def test_an_unknown_provider_name_is_a_config_error_and_writes_nothing(tmp_path: Path) -> None:
    config = make_config(tmp_path, transcript_order=("ytdpl",))

    with pytest.raises(ConfigError, match="ytdpl"):
        transcript_providers(config)


# -- through ingest -------------------------------------------------------------


def _ingest_env(tmp_path: Path, monkeypatch, order: str = '["ytdlp"]') -> Library:
    library = tmp_path / "library"
    config = tmp_path / "config.toml"
    config.write_text(
        f'[library]\npath = "{library.as_posix()}"\n'
        f"[providers]\ntranscript_order = {order}\n"
        '[llm]\nbase_url = "https://example.invalid/v1"\nmodel = "test-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MARGINALIA_CONFIG", str(config))
    return Library(library)


def test_a_provider_typo_does_not_condemn_the_video(tmp_path: Path, monkeypatch) -> None:
    """R3 probe S3: the typo marked the video failed/permanent, and after the
    config was fixed `marg ingest` skipped it for good."""
    from marginalia.cli import EXIT_OK, EXIT_USAGE, main

    library = _ingest_env(tmp_path, monkeypatch, order='["ytdpl"]')
    assert main(["ingest", A]) == EXIT_USAGE
    with Ledger(library.ledger_path) as ledger:
        assert ledger.get(A) is None, "no row: nothing was attempted"

    _ingest_env(tmp_path, monkeypatch)
    monkeypatch.setattr("marginalia.flow.acquire.get_provider", lambda _n: Scripted("ytdlp", _result()))
    assert main(["ingest", A]) == EXIT_OK
    with Ledger(library.ledger_path) as ledger:
        assert ledger.get(A).status == STATUS_HAS_TRANSCRIPT


def test_ingest_refuses_an_untracked_transcript_without_fetching(tmp_path: Path, monkeypatch) -> None:
    """R3 probe S13: bytes on disk with no row made ingest fetch anyway, die on
    `FileExistsError` and leave `pending` behind."""
    from marginalia.cli import EXIT_FAILED, main

    library = _ingest_env(tmp_path, monkeypatch)
    library.ensure()
    library.write_transcript(A, [TranscriptLine(0, 1000, "written by hand")])
    provider = Scripted("ytdlp", _result())
    monkeypatch.setattr("marginalia.flow.acquire.get_provider", lambda _n: provider)

    assert main(["ingest", A]) == EXIT_FAILED
    assert provider.asked == 0
    with Ledger(library.ledger_path) as ledger:
        assert ledger.get(A) is None


def test_ingest_records_the_provider_that_answered(tmp_path: Path, monkeypatch) -> None:
    from marginalia.cli import EXIT_OK, main

    library = _ingest_env(tmp_path, monkeypatch, order='["first", "second"]')
    chain = {"first": Scripted("first", TransientError("x")), "second": Scripted("second", _result())}
    monkeypatch.setattr("marginalia.flow.acquire.get_provider", lambda name: chain[name])

    assert main(["ingest", A]) == EXIT_OK
    with Ledger(library.ledger_path) as ledger:
        assert ledger.get(A).provider == "second"
    assert library.read_meta(A)["provider"] == "second"


# -- through refetch ------------------------------------------------------------


def _refetch_env(tmp_path: Path):
    config = make_config(tmp_path, transcript_order=("first", "second"))
    library = Library(config.library_path)
    library.ensure()
    return config, Ledger(config.library_path / "ledger.db"), library


def test_refetch_walks_the_whole_chain(tmp_path: Path, monkeypatch) -> None:
    """Refetch used `transcript_order[0]` alone."""
    from marginalia.flow.refetch import refetch

    config, ledger, library = _refetch_env(tmp_path)
    ledger.mark_pending(A)
    ledger.mark_failed(A, FAIL_TRANSIENT)
    chain = {"first": Scripted("first", TransientError("x")), "second": Scripted("second", _result())}
    monkeypatch.setattr("marginalia.flow.acquire.get_provider", lambda name: chain[name])

    run = refetch(config, ledger, library, sleep=lambda _: None)

    assert [r.outcome for r in run.results] == [RECOVERED]
    assert ledger.get(A).provider == "second"
    ledger.close()


def test_refetch_retries_an_interrupted_fetch_but_not_one_in_flight(tmp_path: Path, monkeypatch) -> None:
    """R3 probes S1/S2: an interrupted fetch left `pending`, refetch read only
    `failed/transient`, and the video was stranded until a manual re-ingest."""
    from marginalia.flow.refetch import refetch

    config, ledger, library = _refetch_env(tmp_path)
    stale, fresh = "sssssssssss", "fffffffffff"
    ledger.mark_pending(stale)
    ledger.mark_pending(fresh)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    ledger._conn.execute("UPDATE videos SET last_updated = ? WHERE video_id = ?", (old, stale))
    ledger._conn.commit()
    monkeypatch.setattr("marginalia.flow.acquire.get_provider", lambda name: Scripted(name, _result()))

    run = refetch(config, ledger, library, sleep=lambda _: None)

    assert [r.video_id for r in run.results] == [stale]
    assert ledger.get(stale).status == STATUS_HAS_TRANSCRIPT
    assert ledger.get(fresh).status == STATUS_PENDING
    ledger.close()
