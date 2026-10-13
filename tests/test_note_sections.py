"""One section splitter for every reader of a note (R2 F2, REVIEW C20).

The validator, the Verbatim check, the answer checker's Verbatim fallback, the
selector's summary and the timestamp snapper each found sections their own way.
A `# install the dependencies` line inside Verbatim ended the section — so the
note "had no Verbatim" and failed validation every run, and a quote after it
went unchecked — while `### Verbatim` passed validation and was invisible to the
answer side. These tests run the same notes through every reader.
"""

from __future__ import annotations

import pytest

from marginalia.ask import summarise
from marginalia.contracts import LoadedNote
from marginalia.enrich.note import REQUIRED_SECTIONS, check_verbatim, missing_sections
from marginalia.quotes import note_sections, verbatim_of

TRANSCRIPT = "[00:00:01.000 --> 00:00:05.000] we always cache the system prompt first\n"


def _note(verbatim: str, *, heading: str = "## Verbatim", claims: str = "- a claim") -> str:
    parts = []
    for name in REQUIRED_SECTIONS:
        if name == "Verbatim":
            parts.append(f"{heading}\n{verbatim}\n")
        elif name == "Claims":
            parts.append(f"## Claims\n{claims}\n")
        else:
            parts.append(f"## {name}\n- something\n")
    return "\n".join(parts)


def test_a_shell_comment_opening_verbatim_is_content_not_a_heading() -> None:
    note = _note('# install the dependencies\n- "we always cache the system prompt first" [00:00:01]')

    assert missing_sections(note) == [], "Verbatim is not empty"
    assert "install the dependencies" in verbatim_of(note)


def test_a_quote_after_a_comment_line_is_still_checked() -> None:
    """A fabricated quote after a `#` line went unchecked: (1, 0)."""
    note = _note('# a comment\n- "this sentence was never said by anyone"')

    _, checked, unfound = check_verbatim(note, TRANSCRIPT)

    assert (checked, unfound) == (1, 1)


@pytest.mark.parametrize(
    "heading", ["## Verbatim", "### Verbatim", "## Verbatim:", "## **Verbatim**", "## verbatim"]
)
def test_every_spelling_the_validator_accepts_the_answer_side_finds(heading: str) -> None:
    note = _note('- "we always cache the system prompt first"', heading=heading)

    assert missing_sections(note) == []
    assert "we always cache" in verbatim_of(note)


def test_a_heading_inside_a_code_fence_is_content() -> None:
    note = _note('```\n## Claims\nnot a heading\n```\n- "we always cache the system prompt first"')

    assert "not a heading" in verbatim_of(note)
    assert note_sections(note)["Claims"] == "- a claim"


def test_an_unknown_level_two_heading_ends_the_section_before_it() -> None:
    """`##` is the format's section level: an extra section's text is not the
    previous section's — it must not be quote-checked as Verbatim."""
    note = _note('- "we always cache the system prompt first"\n## Extra remarks\nnot verbatim')

    assert "not verbatim" not in verbatim_of(note)
    _, checked, _ = check_verbatim(note, TRANSCRIPT)
    assert checked == 1


def test_a_claims_line_starting_with_a_hash_is_a_claim() -> None:
    note = _note("ok", claims="#1 the system prompt is cached first")

    assert "Claims" not in missing_sections(note)


def test_the_selector_summary_reads_the_same_claims_section() -> None:
    note_text = "# Title\n20260101 · Chan\n\n" + _note("ok", claims="- the real claim").replace(
        "## Claims", "### Claims"
    )
    summary = summarise(LoadedNote("aaaaaaaaaaa", "A title", note_text))

    assert summary.gist == "the real claim"


# -- shapes the validator accepted before the splitter, which must still pass ------
# (independent review F4)


def test_an_unclosed_code_fence_does_not_swallow_the_sections_after_it() -> None:
    note = _note("ok").replace("## Leaves out\n", "```\n## Leaves out\n")
    assert missing_sections(note) == []


def test_a_section_repeated_with_an_empty_first_copy_still_validates() -> None:
    note = _note("ok").replace("## Claims\n- a claim\n", "## Claims\n\n## Claims\n- a claim\n")
    assert missing_sections(note) == []


def test_an_indented_heading_still_counts() -> None:
    note = _note("ok").replace("## Verbatim", "    ## Verbatim")
    assert missing_sections(note) == [] and verbatim_of(note) == "ok"


def test_inline_triple_backticks_are_not_a_fence() -> None:
    note = _note('```pip install x```\n- "we always cache the system prompt first"')
    assert "we always cache" in verbatim_of(note)
    assert note_sections(note)["Claims"] == "- a claim"
