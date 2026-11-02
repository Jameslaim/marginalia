"""A missing thing is a name, never evidence.

Measured: a baseline wrote video ids and a paraphrase after a missing thing's
dash, and they went on into the plan's items and every query's origin. Cut
where the reply is parsed, where calls are resolved, and where a stored
baseline is read back. Subjects are invented.
"""

from __future__ import annotations

from marginalia.brain.coverage import names_only, resolve_coverage
from marginalia.brain.planner import _parse_coverage
from marginalia.contracts import CoverageItem, LibraryCoverage

BRIEF_TEXT = (
    "INTENT\nKeep a small flock of backyard hens.\n\n"
    "REQUIRED OF EVERY VIDEO\n- shows real hens\n\n"
    "PARTS TO COVER\n"
    "- Coop design: ventilation, roosting bars\n"
    "- Hen health: mites, egg binding\n\n"
    "EXCLUDED\n- adverts for coops\n"
)


def test_b2_a_missing_thing_is_a_name_only_where_the_reply_is_parsed() -> None:
    item = _parse_coverage(
        "Hen health | PARTLY | held: mites — abcdefghijk (dusting shown) | "
        "missing: egg binding — abcdefghijk and bcdefghijkl (warm baths)"
    )
    assert item.missing == ("egg binding",)
    assert item.held == ("mites — abcdefghijk (dusting shown)",), "a held thing keeps its evidence"


def test_b2_resolved_and_reused_coverage_carries_names_only() -> None:
    calls = [
        LibraryCoverage(items=[CoverageItem("Hen health", False, "", partly=True, held=("mites",),
                                            missing=("egg binding — abcdefghijk (warm baths)",))]),
        LibraryCoverage(items=[CoverageItem("Hen health", False, "", partly=True, held=("mites",),
                                            missing=("egg binding",))]),
    ]
    resolved, _ = resolve_coverage(calls, BRIEF_TEXT)
    assert resolved.items[0].missing == ("egg binding",)
    # A baseline stored before this fix is read back the same way: no new key.
    stored = LibraryCoverage(items=[CoverageItem("Hen health", False, "",
                                                 missing=("egg binding — abcdefghijk (warm baths)",))])
    assert names_only(stored).items[0].missing == ("egg binding",)
