"""Reference rarity IDs, display names, and base spawn weights.

The legacy Epic value is retained as stored data. It shares the Special Edition
spawn bucket rather than adding a nineteenth reference weight or rewriting IDs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

Rarity = Literal[
    "common",
    "rare",
    "special-edition",
    "legendary",
    "mythic-edition",
    "valentine-edition",
    "summer-edition",
    "rainy-edition",
    "halloween-edition",
    "christmas-edition",
    "winter-edition",
    "new-year-edition",
    "festival-edition",
    "amv-edition",
    "event-edition",
    "celestial-edition",
    "luxury-edition",
    "limited-edition",
    "epic",
]


@dataclass(frozen=True, slots=True)
class RarityDefinition:
    id: int
    value: Rarity
    label: str
    weight: int


# These are defaults from Summon-bot's database initialization, not a claim
# about any running instance's customized settings. Its separate admin fallback
# specifies 100 for AMV, while the initialization path specifies 10.
RARITIES: tuple[RarityDefinition, ...] = (
    RarityDefinition(1, "common", "Common", 4500),
    RarityDefinition(2, "rare", "Rare", 2500),
    RarityDefinition(3, "special-edition", "Special Edition", 1200),
    RarityDefinition(4, "legendary", "Legendary", 600),
    RarityDefinition(5, "mythic-edition", "Mythic Edition", 300),
    RarityDefinition(6, "valentine-edition", "Valentine Edition", 50),
    RarityDefinition(7, "summer-edition", "Summer Edition", 50),
    RarityDefinition(8, "rainy-edition", "Rainy Edition", 50),
    RarityDefinition(9, "halloween-edition", "Halloween Edition", 50),
    RarityDefinition(10, "christmas-edition", "Christmas Edition", 50),
    RarityDefinition(11, "winter-edition", "Winter Edition", 50),
    RarityDefinition(12, "new-year-edition", "New Year Edition", 50),
    RarityDefinition(13, "festival-edition", "Festival Edition", 40),
    RarityDefinition(14, "amv-edition", "AMV Edition", 10),
    RarityDefinition(15, "event-edition", "Event Edition", 100),
    RarityDefinition(16, "celestial-edition", "Celestial Edition", 40),
    RarityDefinition(17, "luxury-edition", "Luxury Edition", 30),
    RarityDefinition(18, "limited-edition", "Limited Edition", 20),
)

RARITY_VALUES = get_args(Rarity)
RARITY_WEIGHTS = {rarity.value: rarity.weight for rarity in RARITIES}
_LABELS: dict[str, str] = {str(rarity.value): rarity.label for rarity in RARITIES} | {
    "epic": "Epic (legacy)"
}


def rarity_label(value: str) -> str:
    return _LABELS.get(value, value.replace("-", " ").title())


def parse_rarity(value: str) -> Rarity | None:
    """Accept reference IDs, canonical slugs, labels, and the legacy value."""
    normalized = " ".join(value.strip().casefold().replace("_", " ").replace("-", " ").split())
    if normalized == "epic":
        return "epic"
    for rarity in RARITIES:
        if normalized in (str(rarity.id), rarity.value.replace("-", " "), rarity.label.casefold()):
            return rarity.value
    return None


def spawn_values(value: str) -> tuple[str, ...]:
    """Select old Epic and Special Edition together without changing either record."""
    return ("special-edition", "epic") if value == "special-edition" else (value,)
