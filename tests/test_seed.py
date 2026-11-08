from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from summon.seed import seed_characters


async def test_seeding_only_inserts_missing_characters_and_never_touches_inventory(monkeypatch):
    async def fake_transaction(_client, callback):
        return await callback("session")

    monkeypatch.setattr("summon.seed.transaction", fake_transaction)
    numbers = SimpleNamespace(
        ensure_initialized=AsyncMock(), allocate=AsyncMock(side_effect=[1, 3])
    )
    monkeypatch.setattr("summon.seed.CharacterNumbers", lambda *_: numbers)
    db = SimpleNamespace(
        client=object(),
        characters=SimpleNamespace(
            find_one=AsyncMock(side_effect=[None, {"_id": "ren"}, None]),
            update_one=AsyncMock(
                side_effect=[
                    SimpleNamespace(upserted_id="aiko"),
                    SimpleNamespace(upserted_id="mira"),
                ]
            ),
        ),
        inventory=SimpleNamespace(update_one=AsyncMock(), delete_many=AsyncMock()),
    )
    result = await seed_characters(db)
    assert result == {"inserted": 2, "already_present": 1}
    assert db.characters.update_one.await_count == 2
    assert numbers.allocate.await_count == 2
    numbers.ensure_initialized.assert_awaited_once()
    for call in db.characters.update_one.await_args_list:
        assert set(call.args[1]) == {"$setOnInsert"}
        assert call.kwargs["upsert"] is True
        assert call.kwargs["session"] == "session"
        assert isinstance(call.args[1]["$setOnInsert"]["number"], int)
    db.inventory.update_one.assert_not_awaited()
    db.inventory.delete_many.assert_not_awaited()


def test_apply_requires_exact_confirmation(monkeypatch, capsys):
    import sys

    from summon.seed import main

    monkeypatch.setattr(sys, "argv", ["summon-seed", "--apply"])
    with pytest.raises(SystemExit):
        main()
    assert "confirm" in capsys.readouterr().err.lower()
