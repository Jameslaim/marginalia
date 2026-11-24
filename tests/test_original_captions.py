"""The transcript is the spoken language's own track, and the language is clean.

Measured on the library: 17 of 177 stored transcripts were machine translations
of English speech into Arabic. yt-dlp reported each video's language as `ar`,
its only audio track was English marked "original", and the automatic table —
which lists translations into about 180 languages — had an `ar` entry, so the
reported language chose the translation. Five more stored a track id as the
language (`en-eEY6OEpapPo`).

The fixtures mirror those listings in shape. Caption text is invented.
"""

from __future__ import annotations

import sys
import types

import pytest

from marginalia.contracts import (
    ACQUIRE_TRANSLATED_ONLY,
    TRANSLATED_ONLY,
)
from marginalia.providers.base import TranslatedOnly
from marginalia.providers.ytdlp import YtDlpProvider, clean_language, spoken_language


def _fake_yt_dlp(info: dict, bodies: dict[str, bytes]) -> types.ModuleType:
    class YoutubeDL:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            return info

        def urlopen(self, url):
            module.opened.append(url)
            return types.SimpleNamespace(read=lambda: bodies[url])

    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = YoutubeDL
    module.opened = []
    return module


def _vtt(text: str) -> bytes:
    return f"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n{text}\n".encode("utf-8")


def _audio(language: str, note: str = "medium") -> dict:
    return {"acodec": "opus", "vcodec": "none", "language": language, "format_note": note}


def _tracks(*keys: str) -> dict:
    return {key: [{"url": key, "ext": "vtt"}] for key in keys}


# The measured failure: reported `ar`, English audio marked original, an
# automatic table with originals in many languages and translations into more.
OLD_FAILURE = {
    "language": "ar",
    "formats": [_audio("en-US", "English (US) original (default), low"),
                _audio("en-US", "English (US) original (default), medium")],
    "subtitles": {},
    "automatic_captions": _tracks("ar", "ar-orig", "de-DE-orig", "en", "en-orig", "fr", "hi-orig"),
    "title": "An English explainer", "duration": 300,
}
BODIES = {key: _vtt(f"text of the {key} track") for key in
          ("ar", "ar-orig", "de-DE-orig", "en", "en-orig", "fr", "hi-orig", "hi", "en-eEY6OEpapPo", "fr-orig")}


def _fetch(info, monkeypatch):
    fake = _fake_yt_dlp(info, BODIES)
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    return YtDlpProvider().fetch("aaaaaaaaaaa"), fake


def test_the_old_failure_now_takes_the_spoken_languages_original_track(monkeypatch) -> None:
    result, fake = _fetch(OLD_FAILURE, monkeypatch)

    assert fake.opened == ["en-orig"], "never the reported language's translation"
    assert (result.language, result.caption_track, result.caption_kind) == ("en", "en-orig", "auto")


def test_the_spoken_language_is_read_from_the_audio_not_the_reported_language() -> None:
    assert spoken_language(OLD_FAILURE, OLD_FAILURE["automatic_captions"]) == "en-US"
    single = {"formats": [_audio("en"), _audio("en")]}
    assert spoken_language(single, {}) == "en"
    only_original = {"formats": [{"acodec": "opus", "vcodec": "none"}]}
    assert spoken_language(only_original, _tracks("hi-orig", "en", "fr")) == "hi"
    assert spoken_language({"language": "ar", "formats": []}, _tracks("ar", "en")) == ""


def test_an_original_manual_track_is_preferred_over_a_translated_one(monkeypatch) -> None:
    info = {
        "language": "en",  # reported wrongly again
        "formats": [_audio("hi", "Hindi original (default)")],
        "subtitles": _tracks("en", "hi"),  # a human English translation, and the original
        "automatic_captions": _tracks("hi-orig", "en"),
        "title": "t", "duration": 60,
    }
    result, fake = _fetch(info, monkeypatch)
    assert fake.opened == ["hi"]
    assert (result.language, result.caption_kind) == ("hi", "manual")


def test_a_translated_manual_track_never_beats_the_original_speech(monkeypatch) -> None:
    info = {
        "formats": [_audio("hi", "Hindi original (default)")],
        "subtitles": _tracks("en"),
        "automatic_captions": _tracks("hi-orig", "en"),
        "title": "t", "duration": 60,
    }
    result, fake = _fetch(info, monkeypatch)
    assert fake.opened == ["hi-orig"]
    assert result.language == "hi"


def test_translated_tracks_only_is_unavailable_not_stored(monkeypatch) -> None:
    info = {
        "formats": [_audio("en", "English original (default)")],
        # A manual track in another language and no original-speech track at
        # all: every track is a translation (rule 40). An `-orig` track in any
        # language would be used instead — see the label-disagreement tests.
        "subtitles": _tracks("fr"),
        "automatic_captions": _tracks("de", "es"),
        "title": "t", "duration": 60,
    }
    monkeypatch.setitem(sys.modules, "yt_dlp", _fake_yt_dlp(info, BODIES))
    with pytest.raises(TranslatedOnly) as raised:
        YtDlpProvider().fetch("aaaaaaaaaaa")
    assert raised.value.original == "en"
    assert "fr" in raised.value.offered


def test_one_original_track_in_another_language_beats_the_audio_label(monkeypatch) -> None:
    """Measured: audio labelled English, one original-speech track, in Hindi.
    The track is YouTube's recognition of the speech, so it is used, and the
    disagreement is counted on the result."""
    info = {
        "language": "hi",
        "formats": [_audio("en-US", "English (US) original (default)")],
        "subtitles": {},
        "automatic_captions": _tracks("hi-orig", "hi", "en", "ar"),
        "title": "t", "duration": 60,
    }
    result, fake = _fetch(info, monkeypatch)
    assert fake.opened == ["hi-orig"]
    assert (result.language, result.caption_track, result.audio_label) == ("hi", "hi-orig", "en-US")


def test_an_agreeing_label_records_no_disagreement(monkeypatch) -> None:
    result, _ = _fetch(OLD_FAILURE, monkeypatch)
    assert result.audio_label == ""


def test_several_original_tracks_none_in_the_audio_language_take_the_reported_one(monkeypatch) -> None:
    info = {
        "language": "hi",
        "formats": [_audio("en-US", "English (US) original (default)")],
        "subtitles": {},
        "automatic_captions": _tracks("hi-orig", "fr-orig", "en"),
        "title": "t", "duration": 60,
    }
    result, fake = _fetch(info, monkeypatch)
    assert fake.opened == ["hi-orig"] and result.audio_label == "en-US"


def test_the_saved_listing_for_the_measured_disagreement_is_now_stored() -> None:
    """Synthetic listing reproducing the audio-label vs auto_orig disagreement offline."""
    import json
    from pathlib import Path

    fixture = Path(__file__).resolve().parent / "fixtures" / "caption_disagreement.json"
    row = json.loads(fixture.read_text(encoding="utf-8"))
    info = {
        "language": row["reported_language"],
        "formats": [_audio(lang, note) for lang, note in row["audio"]],
        "subtitles": _tracks(*row["manual"]),
        "automatic_captions": _tracks(*row["auto_orig"], "en", "ar"),
    }
    picked = YtDlpProvider()._candidate_tracks(info)
    assert [(key, kind) for key, kind, _ in picked] == [("hi-orig", "auto")]


def test_a_named_manual_track_stores_a_clean_language_and_keeps_its_track(monkeypatch) -> None:
    info = {
        "language": "en",
        "formats": [_audio("en")],
        "subtitles": _tracks("en-eEY6OEpapPo"),
        "automatic_captions": _tracks("en-orig", "ar"),
        "title": "t", "duration": 60,
    }
    result, _ = _fetch(info, monkeypatch)
    assert (result.language, result.caption_track, result.caption_kind) == ("en", "en-eEY6OEpapPo", "manual")


@pytest.mark.parametrize(("key", "code"), [
    ("en-orig", "en"), ("en-eEY6OEpapPo", "en"), ("en-j3PyPqV-e1s", "en"),
    ("de-DE-orig", "de-DE"), ("es-419", "es-419"), ("zh-Hans", "zh-Hans"), ("pt-br", "pt-BR"),
])
def test_a_language_code_is_clean(key, code) -> None:
    assert clean_language(key) == code


def test_with_no_spoken_language_the_old_preferences_apply(monkeypatch) -> None:
    info = {"language": "en", "subtitles": {}, "automatic_captions": _tracks("en", "fr"),
            "title": "t", "duration": 60}
    result, fake = _fetch(info, monkeypatch)
    assert fake.opened == ["en"] and result.language == "en"


# -- every fetch path: acquire, then the counters ----------------------------------


class _Translating:
    name = "ytdlp"

    def fetch(self, video_id):
        raise TranslatedOnly("en", ["fr"])


def test_acquire_routes_translated_only_to_asr_and_names_it(tmp_path) -> None:
    from marginalia.core.ledger import Ledger
    from marginalia.flow.acquire import acquire

    with Ledger(tmp_path / "ledger.db") as ledger:
        acquisition = acquire(ledger, "aaaaaaaaaaa", [_Translating()])
        row = ledger.get("aaaaaaaaaaa")

    assert acquisition.outcome == ACQUIRE_TRANSLATED_ONLY
    assert "only translated captions" in acquisition.detail
    assert row.status == "needs_asr"


def test_refetch_has_its_own_word_for_it() -> None:
    from marginalia.contracts import Acquisition
    from marginalia.flow.refetch import _outcome

    outcome, detail = _outcome(Acquisition(ACQUIRE_TRANSLATED_ONLY, provider="ytdlp",
                                           detail="only translated captions (spoken language en)"))
    assert outcome == TRANSLATED_ONLY and "spoken language en" in detail


def test_store_records_the_track_beside_the_clean_language(tmp_path) -> None:
    import json

    from marginalia.contracts import TranscriptLine, TranscriptResult
    from marginalia.core.ledger import Ledger
    from marginalia.core.library import Library
    from marginalia.flow.store import store_transcript

    library = Library(tmp_path / "library")
    library.ensure()
    result = TranscriptResult(
        lines=[TranscriptLine(0, 1000, "invented line")], language="en", source="captions",
        title="t", channel="c", duration_s=60, upload_date=None, caption_kind="manual",
        caption_track="en-eEY6OEpapPo",
    )
    with Ledger(library.ledger_path) as ledger:
        ledger.mark_pending("aaaaaaaaaaa")
        store_transcript(library, ledger, "aaaaaaaaaaa", result, provider="ytdlp")
    meta = json.loads((library.video_dir("aaaaaaaaaaa") / "meta.json").read_text(encoding="utf-8"))
    assert (meta["language"], meta["caption_track"]) == ("en", "en-eEY6OEpapPo")
