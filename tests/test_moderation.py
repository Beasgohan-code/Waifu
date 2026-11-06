from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from summon.services.moderation import (
    PermissionDenied,
    require_group_admin,
    require_moderation_rights,
)


def member(status, **rights):
    return SimpleNamespace(status=status, **rights)


async def test_group_admin_is_checked_live_and_required_right_is_enforced():
    bot = SimpleNamespace(
        get_chat_member=AsyncMock(return_value=member("administrator", can_restrict_members=True))
    )
    await require_group_admin(bot, -100, 42, "can_restrict_members")
    bot.get_chat_member.assert_awaited_once_with(-100, 42)
    bot.get_chat_member.return_value = member("administrator", can_restrict_members=False)
    with pytest.raises(PermissionDenied):
        await require_group_admin(bot, -100, 42, "can_restrict_members")
    bot.get_chat_member.return_value = member("member")
    with pytest.raises(PermissionDenied):
        await require_group_admin(bot, -100, 42, "can_restrict_members")


async def test_owner_has_rights_but_cannot_target_an_admin():
    bot = SimpleNamespace(
        id=99,
        get_chat_member=AsyncMock(
            side_effect=[
                member("creator"),
                member("administrator", can_restrict_members=True),
                member("administrator"),
            ]
        ),
    )
    with pytest.raises(PermissionDenied, match="administrators"):
        await require_moderation_rights(bot, -100, 1, 2)


async def test_bot_must_have_actual_restriction_permission():
    bot = SimpleNamespace(
        id=99,
        get_chat_member=AsyncMock(
            side_effect=[member("creator"), member("administrator", can_restrict_members=False)]
        ),
    )
    with pytest.raises(PermissionDenied, match="bot"):
        await require_moderation_rights(bot, -100, 1, 2)


async def test_self_and_bot_target_are_rejected_before_telegram_call():
    bot = SimpleNamespace(id=99, get_chat_member=AsyncMock())
    with pytest.raises(PermissionDenied):
        await require_moderation_rights(bot, -100, 1, 1)
    with pytest.raises(PermissionDenied):
        await require_moderation_rights(bot, -100, 1, 99)
    bot.get_chat_member.assert_not_awaited()
