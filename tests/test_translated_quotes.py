"""A quote of a translation is labelled as one.

Measured: one answer quoted three English strings that were a note's own
translations of Verbatim quotes in another language (`[note: translation: ...]`,
the shape `write_note.md` asks for). They were in the note and not in the
transcript, so all three were labelled "not spoken" — false for the reader: the
speaker said it, in another language. Now they are `[translated]`, counted on
their own, and every other kind of quote is decided exactly as before.
"""

from __future__ import annotations

from pathlib import Path

from marginalia.ask import MARK_TRANSLATED, check_grounding, load_notes, mark_unverified
from marginalia.contracts import Answer
from marginalia.enrich.note import untranslated_foreign_verbatim
from marginalia.quotes import dominant_script, verbatim_translations

from test_ask import _cue, build

A = "dQw4w9WgXcQ"
SPOKEN = "laisser reposer la pâte une heure avant de la cuire"
TRANSLATION = "let the dough rest for an hour before baking it"
NOTE = (
    "## Claims\n\n- the dough rests, and the note says so in its own words\n\n"
    "## Verbatim\n\n"
    f'- "{SPOKEN}" (00:00) [note: translation: {TRANSLATION}]\n'
)


def _grounding(tmp_path: Path, text: str):
    config, library = build(tmp_path, {A: NOTE}, {A: _cue(SPOKEN)})
    return check_grounding(
        text, load_notes(library), [A], transcripts={A: library.read_transcript(A)}
    )


def test_a_quote_of_a_verbatim_translation_is_labelled_translated(tmp_path) -> None:
    grounding = _grounding(tmp_path, f'The cook says "{TRANSLATION}" [{A} 00:00].')

    assert [q.text for q in grounding.translated_quotes] == [TRANSLATION]
    assert grounding.unspoken_quotes == ()
    assert grounding.unverified_speech == () and grounding.unverified_phrases == ()


def test_the_marker_in_the_answer_says_translated(tmp_path) -> None:
    text = f'The cook says "{TRANSLATION}" [{A} 00:00].'
    grounding = _grounding(tmp_path, text)
    answer = Answer(question="q", text=text, version="1", model="m",
                    translated_quotes=list(grounding.translated_quotes))

    assert f'"{TRANSLATION}" {MARK_TRANSLATED}' in mark_unverified(answer)


def test_a_quote_of_the_original_is_still_found_in_the_transcript(tmp_path) -> None:
    grounding = _grounding(tmp_path, f'The cook says "{SPOKEN}" [{A} 00:00].')

    assert (grounding.quotes_checked, grounding.quotes_verified) == (1, 1)
    assert grounding.translated_quotes == ()


def test_the_note_s_own_prose_quoted_is_still_not_spoken(tmp_path) -> None:
    """The order of decision for every other kind of quote is unchanged."""
    prose = "the note says so in its own words"
    grounding = _grounding(tmp_path, f'Reportedly "{prose}" [{A} 00:00].')

    assert [q.text for q in grounding.unspoken_quotes] == [prose]
    assert grounding.translated_quotes == ()


def test_only_verbatim_translation_markers_count_as_translations() -> None:
    note = NOTE.replace("## Claims\n\n", "## Claims\n\n- [note: translation: elsewhere]\n")

    assert verbatim_translations(note) == TRANSLATION


def test_dominant_script_names_the_writing_system() -> None:
    assert dominant_script("laisser reposer") == "LATIN"
    assert dominant_script("اللوجيت هو ببساطة") == "ARABIC"
    assert dominant_script("टोकन क्या होता है") == "DEVANAGARI"
    assert dominant_script("12:30 —") == ""


def test_a_verbatim_line_offering_a_translation_as_speech_is_counted() -> None:
    transcript = _cue("اللوجيت هو ببساطة درجة غير مطبعة")
    note = '## Verbatim\n\n- "the logit is simply an unnormalised score" (00:00)\n'

    assert untranslated_foreign_verbatim(note, transcript) == 1


def test_a_translated_or_spoken_line_is_not_counted() -> None:
    transcript = _cue("اللوجيت هو ببساطة درجة غير مطبعة and then some english words here")
    translated = (
        '## Verbatim\n\n- "اللوجيت هو ببساطة درجة غير مطبعة" (00:00) '
        "[note: translation: the logit is simply an unnormalised score]\n"
    )
    mixed = '## Verbatim\n\n- "and then some english words here" (00:00)\n'

    assert untranslated_foreign_verbatim(translated, transcript) == 0
    assert untranslated_foreign_verbatim(mixed, transcript) == 0, "said, in the speaker's mix"


def test_the_prompt_s_shape_checks_the_original_and_labels_the_english(tmp_path) -> None:
    """answer.md v9: the original as said, then the marker's English after
    "in translation". The check finds the first in the transcript and labels
    the second [translated] — the two halves of the shape, each its own way."""
    text = f'The cook says "{SPOKEN}" — in translation, "{TRANSLATION}" [{A} 00:00].'

    grounding = _grounding(tmp_path, text)

    assert (grounding.quotes_checked, grounding.quotes_verified) == (2, 1)
    assert [q.text for q in grounding.translated_quotes] == [TRANSLATION]
    assert grounding.unspoken_quotes == ()
    assert grounding.unverified_speech == () and grounding.unverified_phrases == ()
