"""Opt-in, insert-only sample catalogue seed; never migrates or edits ownership."""

from __future__ import annotations

import argparse
import asyncio
import secrets
from datetime import UTC, datetime
from typing import Any

from summon.config import Settings
from summon.database import connect_database
from summon.models import CharacterInput, normalize_name

CONFIRMATION = "INSERT-SAMPLE"
SAMPLE_CHARACTERS = (
    CharacterInput(
        id="aiko",
        name="Aiko Tsukino",
        category="Moonlit Chronicles",
        rarity="legendary",
        image_url="/art/aiko.webp",
        description="A quiet swordswoman protecting stories hidden between worlds.",
    ),
    CharacterInput(
        id="ren",
        name="Ren Aster",
        category="The Starlit Path",
        rarity="epic",
        image_url="/art/ren.webp",
        description="A wandering sky mage collecting lost constellations.",
    ),
    CharacterInput(
        id="mira",
        name="Mira Evergreen",
        category="Whispers of the Grove",
        rarity="rare",
        image_url="/art/mira.webp",
        description="An inventive alchemist with a soft spot for forgotten forests.",
    ),
)


async def seed_characters(database: Any) -> dict[str, int]:
    inserted = 0
    rng = secrets.SystemRandom()
    now = datetime.now(UTC)
    for character in SAMPLE_CHARACTERS:
        document = character.model_dump(exclude={"id"})
        document.update(
            name_key=normalize_name(character.name), random_key=rng.random(), created_at=now
        )
        # Only inserts are allowed; existing catalog and every ownership record remain untouched.
        result = await database.characters.update_one(
            {"_id": character.id},
            {"$setOnInsert": document},
            upsert=True,
        )
        inserted += int(result.upserted_id is not None)
    return {"inserted": inserted, "already_present": len(SAMPLE_CHARACTERS) - inserted}


async def apply_seed() -> dict[str, int]:
    client, database = await connect_database(Settings())
    try:
        return await seed_characters(database)
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="insert missing sample characters")
    parser.add_argument("--confirm", help=f"must be {CONFIRMATION} when applying")
    args = parser.parse_args()
    if args.apply and args.confirm != CONFIRMATION:
        parser.error(f"--apply requires --confirm {CONFIRMATION}")
    print("Sample IDs:", ", ".join(character.id for character in SAMPLE_CHARACTERS))
    if not args.apply:
        print("Plan only: no connection or writes. Existing records are never overwritten.")
        return
    result = asyncio.run(apply_seed())
    print(f"Inserted {result['inserted']}; already present {result['already_present']}.")


if __name__ == "__main__":
    main()
