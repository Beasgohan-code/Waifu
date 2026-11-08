"""Selection is uniform within the selected, nonempty rarity bucket."""

from types import SimpleNamespace

from summon.rarities import RARITY_WEIGHTS
from summon.repositories.catalog import CatalogRepository


class DeterministicRng:
    def __init__(self, rarities, offsets):
        self.rarities = iter(rarities)
        self.offsets = iter(offsets)
        self.requested = []

    def choices(self, names, *, weights, k):
        self.requested.append((list(names), list(weights)))
        chosen = next(self.rarities)
        assert chosen in names
        return [chosen]

    def randrange(self, stop):
        offset = next(self.offsets)
        assert 0 <= offset < stop
        return offset


class FakeCharacters:
    def __init__(self, rows):
        self.rows = rows
        self.find_calls = []

    def matches(self, row, query):
        rarity = query["rarity"]
        accepted = rarity["$in"] if isinstance(rarity, dict) else [rarity]
        return row["active"] and row["rarity"] in accepted

    async def count_documents(self, query):
        return len([row for row in self.rows if self.matches(row, query)])

    async def find_one(self, query, *, sort, skip):
        self.find_calls.append((query, sort, skip))
        rows = sorted(
            [row for row in self.rows if self.matches(row, query)],
            key=lambda x: x["_id"],
        )
        return rows[skip] if skip < len(rows) else None


async def test_each_character_gets_one_uniform_offset_regardless_of_random_key():
    characters = FakeCharacters(
        [
            {"_id": "a", "rarity": "common", "active": True, "random_key": 0.001},
            {"_id": "b", "rarity": "common", "active": True, "random_key": 0.002},
            {"_id": "c", "rarity": "common", "active": True, "random_key": 0.999},
        ]
    )
    repo = CatalogRepository(SimpleNamespace(characters=characters))
    repo.rng = DeterministicRng(["common"] * 3, [0, 1, 2])
    assert [(await repo.choose())["_id"] for _ in range(3)] == ["a", "b", "c"]
    assert all(call[1] == [("_id", 1)] for call in characters.find_calls)
    assert all("random_key" not in call[0] for call in characters.find_calls)


async def test_empty_rarity_is_removed_and_remaining_weights_are_preserved():
    characters = FakeCharacters([{"_id": "a", "rarity": "rare", "active": True}])
    repo = CatalogRepository(SimpleNamespace(characters=characters))
    repo.rng = DeterministicRng(["legendary", "rare"], [0])
    assert (await repo.choose())["_id"] == "a"
    names, weights = repo.rng.requested[1]
    assert "legendary" not in names
    assert dict(zip(names, weights, strict=True)) == {
        name: weight for name, weight in RARITY_WEIGHTS.items() if name != "legendary"
    }


async def test_empty_catalog_has_no_spawn():
    repo = CatalogRepository(SimpleNamespace(characters=FakeCharacters([])))
    repo.rng = DeterministicRng(list(RARITY_WEIGHTS), [])
    assert await repo.choose() is None


async def test_legacy_epic_remains_spawnable_in_special_edition_pool():
    rows = [
        {"_id": "a", "rarity": "epic", "active": True},
        {"_id": "b", "rarity": "special-edition", "active": True},
    ]
    characters = FakeCharacters(rows)
    repo = CatalogRepository(SimpleNamespace(characters=characters))
    repo.rng = DeterministicRng(["special-edition", "special-edition"], [0, 1])
    assert (await repo.choose())["_id"] == "a"
    assert (await repo.choose())["_id"] == "b"
    assert rows[0]["rarity"] == "epic"  # Selection never rewrites existing records.
    assert characters.find_calls[0][0]["rarity"] == {"$in": ["special-edition", "epic"]}
