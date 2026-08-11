"""Provider error classification.

Getting this wrong is silent in both directions: a permanent failure marked
transient is retried forever, and a transient failure marked permanent loses a
video with no way to notice. The label is also what a skill reads, so it must
stay short and stable while yt-dlp's wording changes underneath it.

No network, no yt-dlp — `_classify` is a pure function of an exception.
"""

from __future__ import annotations

import pytest

from marginalia.providers.base import PermanentError, TransientError
from marginalia.providers.ytdlp import _classify

# Real messages, as yt-dlp emits them.
PERMANENT = [
    ("ERROR: [youtube] aaaaaaaaaaa: Video unavailable", "unavailable"),
    ("ERROR: [youtube] xxx: Private video. Sign in if you've been granted access", "private"),
    ("ERROR: [youtube] xxx: This video is private", "private"),
    ("The uploader has not made this video available in your country", "region blocked"),
    ("Video unavailable. The uploader has not made this video available in your country",
     "region blocked"),
    ("This video is available to this channel's members on level: Supporter", "members only"),
    ("This video has been removed by the uploader", "removed"),
    ("This video has been removed for violating YouTube's Terms of Service", "removed"),
    ("This video is no longer available because the account associated with this video "
     "has been terminated", "account terminated"),
    ("Sign in to confirm your age. This video may be inappropriate for some users",
     "age restricted"),
    ("ERROR: [youtube:truncated_id] xxx: Incomplete YouTube ID xxx", "not found"),
]

TRANSIENT = [
    ("ERROR: unable to download video data: HTTP Error 429: Too Many Requests", "rate limited"),
    ("Sign in to confirm you're not a bot. Use --cookies-from-browser", "bot check"),
    ("Unable to download API page: [WinError 10061] No connection could be made because "
     "the target machine actively refused it", "connection refused"),
    ("[Errno 111] Connection refused", "connection refused"),
    ("Unable to download webpage: The read operation timed out", "timeout"),
    ("<urlopen error [Errno -2] Name or service not known>", "dns failure"),
    ("[Errno -3] Temporary failure in name resolution", "dns failure"),
    ("certificate verify failed: unable to get local issuer certificate", "tls error"),
    ("Unable to download webpage: HTTP Error 503: Service Unavailable", "server error"),
    ("ERROR: [youtube] xxx: Failed to extract any player response", "extraction failed"),
]


@pytest.mark.parametrize("message,label", PERMANENT)
def test_permanent_failures(message: str, label: str) -> None:
    error = _classify(RuntimeError(message))
    assert isinstance(error, PermanentError), f"{message!r} must not be retried"
    assert str(error) == label


@pytest.mark.parametrize("message,label", TRANSIENT)
def test_transient_failures(message: str, label: str) -> None:
    error = _classify(RuntimeError(message))
    assert isinstance(error, TransientError), f"{message!r} must be retried"
    assert str(error) == label


@pytest.mark.parametrize("message,label", PERMANENT + TRANSIENT)
def test_labels_are_short_and_stable(message: str, label: str) -> None:
    """The label goes into a one-line report a skill parses."""
    error = _classify(RuntimeError(message))
    assert len(str(error)) <= 20
    assert "\n" not in str(error)
    # Never a severed sentence: no fragment of the raw message survives.
    assert message[:40].lower() not in str(error).lower()


def test_unrecognised_errors_default_to_transient() -> None:
    """Never retrying something temporary is a silent permanent loss;
    retrying something permanent costs one wasted request."""
    error = _classify(ValueError("something nobody has seen before"))
    assert isinstance(error, TransientError)
    assert str(error) == "unclassified (ValueError)"


def test_unrecognised_errors_name_their_class_for_triage() -> None:
    class WeirdProviderProblem(Exception):
        pass

    assert str(_classify(WeirdProviderProblem("?"))) == "unclassified (WeirdProviderProblem)"


def test_a_wrapped_cause_is_classified() -> None:
    """yt-dlp wraps the real error; the outer message alone says nothing."""
    try:
        try:
            raise OSError("[WinError 10061] the target machine actively refused it")
        except OSError as cause:
            raise RuntimeError("Unable to download API page") from cause
    except RuntimeError as exc:
        error = _classify(exc)
    assert isinstance(error, TransientError)
    assert str(error) == "connection refused"


def test_specific_patterns_beat_general_ones() -> None:
    """'Video unavailable' prefixes many messages; the specific reason wins."""
    message = (
        "Video unavailable. This video contains content from XYZ, "
        "who has blocked it in your country on copyright grounds"
    )
    assert str(_classify(RuntimeError(message))) == "region blocked"


def test_the_full_text_reaches_the_debug_log(caplog: pytest.LogCaptureFixture) -> None:
    """Detail is not lost, only kept out of the report line."""
    import logging

    with caplog.at_level(logging.DEBUG, logger="marginalia.providers.ytdlp"):
        _classify(RuntimeError("ERROR: [youtube] xxx: Video unavailable, sorry"))
    assert "Video unavailable, sorry" in caplog.text
