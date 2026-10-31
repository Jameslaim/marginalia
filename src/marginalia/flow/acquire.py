"""Acquiring a transcript: the one path every acquirer takes.

Ingest, discovery and refetch each used to carry their own copy of this, with
three different policies. Ingest walked `providers.transcript_order`;
discovery and refetch used only its first entry, and both recorded that entry
as provenance whichever provider actually answered. Discovery also counted a
video with no caption track as a fetch *failure*, while the other two called it
a route. Rule 4 promises that adding a transcript provider is adding one file;
with three copies it was true for ingest alone.

The policy, once:

* **The ledger row first** (rule 6). `mark_pending` precedes every network call,
  so a crash leaves `pending`, which is the state that means a fetch died.
* **A permanent failure ends the walk.** It is a claim about the video —
  deleted, private, region-locked — and asking the next provider about a video
  that no longer exists only spends time.
* **A transient failure outranks "no captions".** If any provider errored, it
  is not yet known that captions are absent, so the video stays retryable.
* **A provider that raises something it did not classify is transient.** It is
  a bug in that provider, and the video is not the thing that is wrong. The row
  used to stay `pending` — the crash marker — where nothing retried it.
* **An unknown provider name is the configuration's fault**, and is raised
  before any row is written: it used to condemn the video as `failed/permanent`,
  after which fixing the typo still skipped it forever.

Storing is `flow/store.py`'s, and stays there.
"""

from __future__ import annotations

import logging
from typing import Sequence

from ..config import Config, ConfigError
from ..contracts import (
    ACQUIRE_FAILED_PERMANENT,
    ACQUIRE_FAILED_TRANSIENT,
    ACQUIRE_NO_CAPTIONS,
    ACQUIRE_TRANSLATED_ONLY,
    ACQUIRED,
    Acquisition,
)
from ..core.ledger import FAIL_PERMANENT, FAIL_TRANSIENT, Ledger
from ..providers import PermanentError, TransientError, get_provider
from ..providers.base import TranscriptProvider, TranslatedOnly

log = logging.getLogger(__name__)

__all__ = ["acquire", "transcript_providers"]

# A provider's failure text is its own classification of an error *shape*
# (see `providers/ytdlp.py`), never a page or a transcript, but it is still
# bounded before it reaches a report.
_DETAIL_LIMIT = 200


def transcript_providers(config: Config) -> list[TranscriptProvider]:
    """The configured chain, resolved before anything is written.

    Raises `ConfigError` for a name no provider answers to: that is a mistake
    in the configuration file and must be reported as one, not recorded as a
    fact about whichever video happened to be first.
    """
    providers: list[TranscriptProvider] = []
    for name in config.transcript_order:
        try:
            providers.append(get_provider(name))
        except KeyError:
            raise ConfigError(
                f"unknown transcript provider {name!r} in providers.transcript_order "
                f"({config.path})"
            ) from None
    if not providers:
        raise ConfigError(f"providers.transcript_order is empty ({config.path})")
    return providers


def acquire(
    ledger: Ledger, video_id: str, providers: Sequence[TranscriptProvider]
) -> Acquisition:
    """Try each provider in order. The row is written before the first call.

    On ``ACQUIRED`` the row is still `pending` and the caller stores the
    transcript; every other outcome has been written to the ledger here.
    """
    if not providers:
        raise ValueError("acquire needs at least one provider")
    ledger.mark_pending(video_id)

    transient: tuple[str, str] | None = None
    no_captions_from: str | None = None
    translated: tuple[str, str] | None = None

    for provider in providers:
        try:
            result = provider.fetch(video_id)
        except PermanentError as exc:
            detail = _bounded(exc)
            log.info("acquire: %s permanently unavailable via %s (%s)", video_id, provider.name, detail)
            ledger.mark_failed(video_id, FAIL_PERMANENT, provider=provider.name)
            return Acquisition(ACQUIRE_FAILED_PERMANENT, provider=provider.name, detail=detail)
        except TranslatedOnly as exc:
            # Another provider may have the spoken language's own track.
            translated = (provider.name, _bounded(exc))
            log.info("acquire: %s has only translated captions via %s (%s)",
                     video_id, provider.name, translated[1])
            continue
        except TransientError as exc:
            transient = (provider.name, _bounded(exc))
            log.info("acquire: %s failed transiently via %s (%s)", video_id, provider.name, transient[1])
            continue
        except Exception as exc:  # noqa: BLE001 - an unclassified provider error is retryable
            # The class name only: an unclassified message is unknown text.
            transient = (provider.name, f"unclassified provider error ({type(exc).__name__})")
            log.warning("acquire: %s via %s raised %s", video_id, provider.name, transient[1])
            continue

        if result is None:
            no_captions_from = provider.name
            log.info("acquire: %s has no captions via %s", video_id, provider.name)
            continue
        return Acquisition(ACQUIRED, result=result, provider=provider.name)

    if transient is not None:
        name, detail = transient
        ledger.mark_failed(video_id, FAIL_TRANSIENT, provider=name)
        return Acquisition(ACQUIRE_FAILED_TRANSIENT, provider=name, detail=detail)

    if translated is not None:
        # Routed as a video with no captions is, and named apart from one.
        name, detail = translated
        ledger.mark_needs_asr(video_id, provider=name)
        return Acquisition(ACQUIRE_TRANSLATED_ONLY, provider=name, detail=detail)

    # No captions is a route, not a failure: M4's ASR queue.
    ledger.mark_needs_asr(video_id, provider=no_captions_from)
    return Acquisition(ACQUIRE_NO_CAPTIONS, provider=no_captions_from, detail="no caption track")


def _bounded(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return text[:_DETAIL_LIMIT] if text else type(exc).__name__
