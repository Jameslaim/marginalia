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


import pytest  # noqa: E402


@pytest.mark.parametrize(
    ("quote", "source"),
    [
        ("we don't cache anything until the second call", "we do not cache anything until the second call"),
        ("you can't change the prefix after the first request", "you cannot change the prefix after the first request"),
        ("and they won't invalidate it for five minutes", "and they will not invalidate it for five minutes"),
    ],
)
def test_a_contraction_is_not_a_reversal(quote: str, source: str) -> None:
    """Found by the independent review: one negation for one, tokenised
    differently, was scored 0 — a faithful quote marked unfound. The guard now
    counts negations on each side."""
    assert closeness(quote, Words(source)) >= 0.6


# -- negation scope (second independent review, finding A) ----------------------
#
# Counting negations over the whole quote let one move between clauses: "the
# cache is not cleared and the model is reloaded" against a source saying the
# cache *is* cleared and the model is *not* reloaded holds one "not" on each
# side, and scored 0.92 — loosely quoted in an answer, found in a note.

MOVED_SOURCE = "the cache is cleared and the model is not reloaded each time"
MOVED_QUOTE = "the cache is not cleared and the model is reloaded each time"


def _cue(text: str) -> str:
    return f"[00:00:01.000 --> 00:00:05.000] {text}\n"


@pytest.mark.parametrize(
    ("quote", "source"),
    [
        (MOVED_QUOTE, MOVED_SOURCE),
        # The same move, written as a contraction on one side.
        (
            "it doesn't work the first time and it is cached after that",
            "it does work the first time and it isn't cached after that",
        ),
        # A different negation word, moved the same way.
        (
            "we never cache the prefix and we refresh it on every call",
            "we cache the prefix and we never refresh it on every call",
        ),
    ],
)
def test_a_negation_moved_to_another_clause_is_not_loosely_quoted(quote: str, source: str) -> None:
    assert closeness(quote, Words(source)) < 0.6


def test_a_transposition_around_a_negation_is_a_known_limit() -> None:
    """Recorded, not fixed: "not always" against "always not" aligns the two
    "not"s and moves "always" instead, so no negation word differs. Word order
    is outside what this guard can name; the earlier presence-based guard
    missed it too. If this starts failing, the guard got stronger — update it."""
    quote = "the prefix is not always cached for the whole five minutes"
    source = "the prefix is always not cached for the whole five minutes"
    assert closeness(quote, Words(source)) >= 0.6


def test_a_moved_negation_is_found_in_a_long_transcript_too() -> None:
    """Past `_EXHAUSTIVE_MAX` words the scorer picks candidate passages by vote;
    the guard must hold on that path as well."""
    filler = " ".join(f"word{n}" for n in range(1_200))
    hay = Words(f"{filler} {MOVED_SOURCE} {filler}")
    assert closeness(MOVED_QUOTE, hay) < 0.6


def test_a_moved_negation_in_an_answer_is_unverified_not_loose() -> None:
    note = LoadedNote("aaaaaaaaaaa", "t", "## Claims\n- x\n\n## Verbatim\n- \"something else\"\n")
    grounding = check_grounding(
        f'He says "{MOVED_QUOTE}" [aaaaaaaaaaa 00:01].',
        [note],
        ["aaaaaaaaaaa"],
        transcripts={"aaaaaaaaaaa": _cue(MOVED_SOURCE)},
    )
    assert grounding.quotes_checked == 1
    assert [quote.loose for quote in grounding.unverified_speech] == [False]


def test_a_moved_negation_in_a_note_is_marked_unfound() -> None:
    from marginalia.enrich.note import check_verbatim

    _, checked, unfound = check_verbatim(f'## Verbatim\n- "{MOVED_QUOTE}"\n', _cue(MOVED_SOURCE))
    assert (checked, unfound) == (1, 1)


@pytest.mark.parametrize(
    ("quote", "source"),
    [
        # A contraction in a quote that also has a caption fix elsewhere.
        (
            "we don't cache anything until the second call to the model arrives",
            "we do not cash anything until the second call to the model arrives",
        ),
        # Two contractions, one per clause, each faithful.
        (
            "it isn't cached and the model won't reload it each time",
            "it is not cached and the model will not reload it each time",
        ),
    ],
)
def test_faithful_contractions_in_either_clause_are_still_loose(quote: str, source: str) -> None:
    """The control for the scope check: a negation that stays in its own place,
    however it is spelled, is not a reversal."""
    assert closeness(quote, Words(source)) >= 0.6


def test_a_faithful_contraction_in_an_answer_is_loosely_quoted() -> None:
    quote = "it isn't cached and the model won't reload it each time"
    note = LoadedNote("aaaaaaaaaaa", "t", "## Claims\n- x\n\n## Verbatim\n- \"something else\"\n")
    grounding = check_grounding(
        f'He says "{quote}" [aaaaaaaaaaa 00:01].',
        [note],
        ["aaaaaaaaaaa"],
        transcripts={"aaaaaaaaaaa": _cue("it is not cached and the model will not reload it each time")},
    )
    assert [q.loose for q in grounding.unverified_speech] == [True]


# -- pairing before length (F2) --------------------------------------------
#
# A quote under four characters used to fail at its opening mark, and the scan
# restarted at its closing mark: from there on the prose between quotations was
# read as quotations, and ask put its labels inside quotation marks.


def test_a_short_quote_does_not_shift_the_later_pairs_on_its_line() -> None:
    text = (
        'He calls it "it", then says "the cache is warm" and later '
        '"the prefix is reused" twice.'
    )
    assert [m.group(1) for m in find_quotes(text)] == [
        "the cache is warm",
        "the prefix is reused",
    ]


def test_curly_and_straight_marks_mixed_pair_in_order() -> None:
    text = '“no” he says, then “the cache is warm" and "the prefix is reused”.'
    assert [m.group(1) for m in find_quotes(text)] == [
        "the cache is warm",
        "the prefix is reused",
    ]


def test_an_apostrophe_inside_a_quote_is_part_of_it() -> None:
    text = 'A word: "it\'s". Then "the model\'s cache isn’t warm" and "that’s all".'
    assert [m.group(1) for m in find_quotes(text)] == [
        "it's",
        "the model's cache isn’t warm",
        "that’s all",
    ]


def test_a_stray_mark_shifts_nothing_beyond_its_own_line() -> None:
    text = 'one stray " mark here\n"the cache is warm" on the next line'
    assert [m.group(1) for m in find_quotes(text)] == ["the cache is warm"]


def test_a_short_low_high_quote_does_not_open_a_pair_with_its_closing_mark() -> None:
    text = 'Er sagt „ja“ und dann "the cache is warm" und "the prefix is reused".'
    assert [m.group(1) for m in find_quotes(text)] == [
        "the cache is warm",
        "the prefix is reused",
    ]


def test_ask_labels_land_after_the_quote_after_a_short_one() -> None:
    """The measured shape: a short quote, then quotes ask labels."""
    note = LoadedNote("aaaaaaaaaaa", "t", "## Claims\n- x\n")
    text = (
        'He uses "it" for the model, says "the cache was invalidated by a single '
        'character" and "nothing like this was ever said in the video" [aaaaaaaaaaa 00:01].'
    )
    grounding = check_grounding(
        text,
        [note],
        ["aaaaaaaaaaa"],
        transcripts={"aaaaaaaaaaa": _cue("the cache was invalidated by a single character")},
    )
    assert grounding.quotes_checked == 2
    assert [q.text for q in grounding.unverified_speech] == [
        "nothing like this was ever said in the video"
    ]
