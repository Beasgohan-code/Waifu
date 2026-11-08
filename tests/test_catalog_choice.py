"""Selection is uniform within the selected, nonempty rarity bucket."""

from types import SimpleNamespace

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

    async def count_documents(self, query):
        return len([row for row in self.rows if row["active"] and row["rarity"] == query["rarity"]])

    async def find_one(self, query, *, sort, skip):
        self.find_calls.append((query, sort, skip))
        rows = sorted(
            [row for row in self.rows if row["active"] and row["rarity"] == query["rarity"]],
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
    assert dict(zip(names, weights, strict=True)) == {"common": 55, "rare": 25, "epic": 15}


async def test_empty_catalog_has_no_spawn():
    repo = CatalogRepository(SimpleNamespace(characters=FakeCharacters([])))
    repo.rng = DeterministicRng(["common", "rare", "epic", "legendary"], [])
    assert await repo.choose() is None
