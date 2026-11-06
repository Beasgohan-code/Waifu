import pytest
from pydantic import ValidationError

from summon.models import CharacterInput, GroupSettings, normalize_name, player_level


def test_normalization_is_unicode_aware_and_collapses_whitespace():
    assert normalize_name("  ＭｉｋＡ   星  ") == "mika 星"


def test_progression_is_monotonic_and_starts_at_one():
    assert [player_level(xp) for xp in (0, 99, 100, 399, 400)] == [1, 1, 2, 2, 3]


def test_character_artwork_must_be_https_or_local_art():
    data = dict(id="mika", name="Mika", category="Sky", rarity="rare")
    assert CharacterInput(**data, image_url="/art/mika.webp").image_url == "/art/mika.webp"
    with pytest.raises(ValidationError):
        CharacterInput(**data, image_url="javascript:alert(1)")
    with pytest.raises(ValidationError):
        CharacterInput(**data, image_url="/art/../../secret")
    with pytest.raises(ValidationError):
        CharacterInput(**data, image_url="https://user:password@example.com/image")


def test_group_configuration_bounds_protection_and_spawns():
    with pytest.raises(ValidationError):
        GroupSettings(spawn_every=1)
    with pytest.raises(ValidationError):
        GroupSettings(flood_limit=5000)
    assert GroupSettings().spawn_enabled is False


def test_group_settings_patch_validates_and_normalizes_keywords():
    from pydantic import ValidationError

    from summon.models import GroupSettingsUpdate

    assert GroupSettingsUpdate(blocked_keywords=["  SPAM  ", "spam"]).blocked_keywords == ["spam"]
    with pytest.raises(ValidationError):
        GroupSettingsUpdate(blocked_keywords=["x" * 65])
    with pytest.raises(ValidationError):
        GroupSettingsUpdate(blocked_keywords=["  "])
