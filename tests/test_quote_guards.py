"""Quote checking: stricter or wider, never re-calibrated.

The loose-match thresholds (0.6 / 0.5 for answers, 0.8 for a note's Verbatim)
were calibrated against the archived library, and nothing here moves them. These
guards only refuse a match the scorer should never have granted, or let the
checks see quotes and text they could not see before (R2 F5, F7, F15, F16).
"""

from __future__ import annotations

import unicodedata

from marginalia.ask import check_grounding
from marginalia.contracts import LoadedNote
from marginalia.quotes import VERBATIM_UNFOUND, Words, closeness, find_quotes, flatten

SAID = (
    "and what we found is that the cached prefix is always invalidated the moment "
    "anything in the system prompt changes even by a single character which surprised us"
)


def test_a_quote_that_reverses_the_source_is_not_loosely_quoted() -> None:
    """R2 F5: "never" for "always" scored 0.85 — "loosely quoted"."""
    flipped = (
        "the cached prefix is never invalidated the moment anything in the system "
        "prompt changes even by a single character"
    )
    assert closeness(flipped, Words(SAID)) < 0.5


def test_an_inserted_not_is_not_loosely_quoted() -> None:
    inserted = (
        "the cached prefix is not always invalidated the moment anything in the system "
        "prompt changes even by a single character"
    )
    assert closeness(inserted, Words(SAID)) < 0.5


def test_a_negation_in_the_following_context_does_not_disqualify_a_quote() -> None:
    """Only words that differ count — not the window's trailing context."""
    hay = Words("the prefix is cached for five minutes not longer and then it expires")
    assert closeness("the prefix is cached for five minutes", hay) >= 0.6


def test_a_one_word_caption_fix_is_still_loose() -> None:
    """The guard must not take away what the loose match is for."""
    fixed = (
        "the cached prefix is always invalidated the moment anything in the system "
        "prompt changes even by a single character"
    ).replace("cached", "cashed")
    assert closeness(fixed, Words(SAID)) >= 0.6


def test_composed_and_decomposed_text_match_exactly() -> None:
    """R2 F16: NFC against NFD failed the exact test (Spanish, Vietnamese)."""
    composed = unicodedata.normalize("NFC", "la configuración está aquí")
    decomposed = unicodedata.normalize("NFD", "la configuración está aquí")
    assert composed != decomposed
    assert flatten(composed) == flatten(decomposed)


def test_quotes_in_other_languages_styles_are_found() -> None:
    """R2 F7: «», „“ and 「」 were invisible — not checked, not counted."""
    text = "Er sagt „das ist immer so gewesen“ und « ceci est toujours vrai » et 「これは常に正しい」"
    assert [m.group(1).strip() for m in find_quotes(text)] == [
        "das ist immer so gewesen",
        "ceci est toujours vrai",
        "これは常に正しい",
    ]


def test_an_apostrophe_is_not_a_quotation_mark() -> None:
    assert list(find_quotes("it’s the speaker’s view that it’s fine")) == []


def test_a_guillemet_quote_in_an_answer_is_checked() -> None:
    note = LoadedNote("aaaaaaaaaaa", "t", "## Verbatim\n- « the prefix is cached first »\n")
    grounding = check_grounding(
        "Il dit « something nobody ever said at all » [aaaaaaaaaaa 00:01].",
        [note],
        ["aaaaaaaaaaa"],
        transcripts={"aaaaaaaaaaa": "[00:00:01.000 --> 00:00:02.000] the prefix is cached first\n"},
    )
    assert grounding.quotes_checked == 1 and grounding.quotes_verified == 0


def test_a_verbatim_line_marked_unfound_does_not_verify_an_answer() -> None:
    """R2 F15: with no transcript on disk the check falls back to Verbatim, and a
    line the note worker marked as not in the transcript still verified."""
    line = f'- "the prefix is cached for exactly ninety seconds" {VERBATIM_UNFOUND}'
    note = LoadedNote("aaaaaaaaaaa", "t", f"## Claims\n- x\n\n## Verbatim\n{line}\n")
    grounding = check_grounding(
        'He says "the prefix is cached for exactly ninety seconds" [aaaaaaaaaaa 00:01].',
        [note],
        ["aaaaaaaaaaa"],
        transcripts={},
    )
    assert grounding.quotes_verified == 0
