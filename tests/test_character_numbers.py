"""Stable numeric aliases do not replace immutable catalog and inventory IDs."""

import pytest

from summon.repositories.character_numbers import (
    CharacterNumberingUnavailable,
    numeric_slug,
    plan_numbers,
)


def test_number_plan_uses_stable_order_and_preserves_numeric_legacy_slugs():
    documents = [{"_id": "aiko"}, {"_id": "03"}, {"_id": "ren"}, {"_id": "mira"}]
    assert plan_numbers(documents) == [("aiko", 1), ("03", 3), ("ren", 2), ("mira", 4)]
    assert documents == [{"_id": "aiko"}, {"_id": "03"}, {"_id": "ren"}, {"_id": "mira"}]


def test_number_plan_preserves_previously_assigned_numbers():
    assert plan_numbers([{"_id": "a", "number": 7}, {"_id": "b"}]) == [("a", 7), ("b", 1)]


@pytest.mark.parametrize(
    "documents",
    [
        [{"_id": "1"}, {"_id": "01"}],
        [{"_id": "x", "number": 1}, {"_id": "01"}],
        [{"_id": "1", "number": 2}],
        [{"_id": "x", "number": None}],
        [{"_id": "x", "number": True}],
        [{"_id": "x", "number": 0}],
        [{"_id": 12}],
    ],
)
def test_ambiguous_or_invalid_legacy_records_fail_before_any_assignment(documents):
    with pytest.raises(CharacterNumberingUnavailable):
        plan_numbers(documents)


def test_numeric_lookup_accepts_leading_zeros_but_bounds_inputs():
    assert numeric_slug("01") == numeric_slug("1") == 1
    assert numeric_slug("0") is None
    assert numeric_slug("-1") is None
    assert numeric_slug("aiko") is None
    assert numeric_slug("9" * 64) is None
