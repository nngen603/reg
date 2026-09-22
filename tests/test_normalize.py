"""The grounding gate's matching layer."""

import pytest

from regextract.contract import MatchKind
from regextract.normalize import locate, normalize_ws, tighten, values_agree


def test_exact_match_returns_original_offsets():
    source = "ECAS Registration Certificate shall be valid for one year."
    result = locate("ECAS Registration Certificate shall be valid for one year.", source)
    assert result.found and result.kind is MatchKind.EXACT
    assert source[result.char_start:result.char_end].startswith("ECAS Registration")


def test_token_broken_across_lines_still_matches():
    """The case that makes normalisation load-bearing rather than cosmetic."""
    source = "Household appliances UAE.S IEC 60335-1, UAE.S 60335-\n2-13 apply."
    result = locate("UAE.S 60335-2-13", source)
    assert result.found and result.kind is MatchKind.EXACT
    assert "60335-\n2-13" in source[result.char_start:result.char_end]


def test_absent_quote_is_not_found():
    result = locate("a penalty of AED 50,000 applies", "The supplier shall renew annually.")
    assert not result.found
    assert result.kind is MatchKind.NONE


def test_fuzzy_is_a_last_resort_and_is_labelled():
    source = "The manufacturer / supplier are ultimately responsible for the product."
    result = locate("The manufacturer/supplier are ultimatly responsible for the product.", source)
    assert result.found and result.kind is MatchKind.FUZZY
    assert result.score >= 92.0


def test_fuzzy_threshold_is_respected():
    source = "The supplier shall renew the registration certificate annually."
    assert not locate("Completely unrelated sentence about microwave ovens", source).found


@pytest.mark.parametrize("left,right", [
    ("One Year", "one  year"),
    ("UAE / IEC 60335-1", "uae / iec 60335-1"),
    ("\u201cLow Voltage\u201d", '"Low Voltage"'),
])
def test_values_agree_after_normalisation(left, right):
    assert values_agree(left, right)


def test_values_that_differ_do_not_agree():
    assert not values_agree("supplier", "manufacturer")


def test_tighten_removes_all_whitespace():
    assert tighten("60335-\n2-13") == "60335-2-13"


def test_normalize_collapses_runs():
    assert normalize_ws("  a   b  ") == "a b"
