"""Small helpers for Wzgram messages, commands, and Telegram identities."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from wzgram.types import CallbackQuery, Message

from summon.auth import TelegramUser


@dataclass(frozen=True, slots=True)
class CommandInput:
    command: str
    args: str | None


def command_input(message: Message) -> CommandInput:
    """Preserve the original argument string after Wzgram's command filter matches."""
    text = str(message.text or message.caption or "")
    parts = text.split(maxsplit=1)
    name = (message.command or [""])[0]
    return CommandInput(command=name, args=parts[1] if len(parts) > 1 else None)


def callback_data(callback: CallbackQuery) -> str:
    data = callback.data
    if isinstance(data, bytes):
        try:
            return data.decode("utf-8", "strict")
        except UnicodeDecodeError:
            return ""
    return data if isinstance(data, str) else ""


def _user(message: Message) -> TelegramUser | None:
    user = message.from_user
    if (
        not message.chat
        or not user
        or user.is_bot
        or message.sender_chat
        or not 0 < user.id < 2**53
    ):
        return None
    return TelegramUser(
        id=user.id,
        first_name=(user.first_name or "Collector")[:128],
        last_name=user.last_name[:128] if user.last_name else None,
        username=user.username[:64] if user.username else None,
    )


def _group(message: Message) -> bool:
    chat = message.chat
    kind: Any = getattr(chat, "type", None)
    return bool(chat and getattr(kind, "value", kind) in {"group", "supergroup", "forum"})


def _duration(value: str) -> int | None:
    match = re.fullmatch(r"(\d{1,5})([mhd])", value.lower())
    if not match:
        return None
    amount = int(match.group(1)) * {"m": 60, "h": 3600, "d": 86400}[match.group(2)]
    return amount if 60 <= amount <= 366 * 86400 else None
