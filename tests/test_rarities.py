from typing import get_args

from summon.database import COLLECTION_SCHEMAS
from summon.models import CharacterInput, Rarity
from summon.rarities import RARITIES, RARITY_VALUES, parse_rarity, rarity_label


def test_reference_tiers_and_legacy_value_are_consistent_across_validation_and_schema():
    assert len(RARITIES) == 18
    assert [tier.id for tier in RARITIES] == list(range(1, 19))
    assert tuple(get_args(Rarity)) == RARITY_VALUES
    enum = COLLECTION_SCHEMAS["characters"]["properties"]["rarity"]["enum"]
    assert set(enum) == set(RARITY_VALUES)
    assert set(RARITY_VALUES) == {tier.value for tier in RARITIES} | {"epic"}
    for tier in RARITIES:
        assert parse_rarity(str(tier.id)) == tier.value
        assert parse_rarity(tier.label) == tier.value
        assert rarity_label(tier.value) == tier.label
        assert (
            CharacterInput(
                id="test",
                name="Test",
                category="Series",
                rarity=tier.value,
                image_url="https://example.com/test.jpg",
            ).rarity
            == tier.value
        )
    assert parse_rarity("Epic") == "epic"
    assert parse_rarity("19") is None


def test_amv_database_initialization_weight_is_recorded():
    assert RARITIES[13].value == "amv-edition"
    assert RARITIES[13].weight == 10
