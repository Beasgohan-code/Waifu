"""Group settings, notes, and permission-checked moderation commands."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pymongo.errors import PyMongoError
from wzgram import Client, filters
from wzgram.errors import RPCError
from wzgram.types import Message

from summon.bot.presentation import Card, Emoji
from summon.bot.telegram_helpers import _duration, _group, _user, command_input
from summon.group_notes import NoteConflict, NoteStorageError
from summon.models import GroupSettings
from summon.services.group_notes import GroupNotesService
from summon.services.moderation import ModerationService, PermissionDenied


def register_group_commands(
    app: Client, store: Any, respond: Any, require_group_admin: Any
) -> None:
    @app.on_message(filters.incoming & filters.command(["manage", "savegroup", "reggroup"]))
    async def manage(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user or not _group(message):
            await respond(
                message,
                Card(
                    "Manage a group",
                    "Use /manage in a group where you are an administrator.",
                    emoji=Emoji.SHIELD,
                ),
            )
            return
        try:
            await require_group_admin(app, message.chat.id, user.id)
        except PermissionDenied as exc:
            await respond(
                message, Card("Admin access required", str(exc), emoji=Emoji.WARNING), private=True
            )
            return
        except (RPCError, OSError):
            await respond(
                message,
                Card("Cannot verify access", "Current group permissions are unavailable."),
                private=True,
            )
            return
        now = datetime.now(UTC)
        group = await store.groups.ensure(message.chat.id, message.chat.title or "Group", now)
        await store.groups.link_admin(message.chat.id, user.id, now)
        current = GroupSettings.model_validate(group["settings"])
        await respond(
            message,
            Card(
                "Group connected",
                (
                    "Open SUMMON privately to manage this group. "
                    "Use /spawns on to enable character appearances."
                ),
                emoji=Emoji.ADMIN_SHIELD,
                fields=(
                    ("Summons", "On" if current.spawn_enabled else "Off"),
                    ("Frequency", f"Every {current.spawn_every} messages"),
                ),
                field_icons=(Emoji.SUMMON_BRAND, Emoji.GEAR),
            ),
            private=True,
        )

    @app.on_message(filters.incoming & filters.command(["spawns", "changetime"]))
    async def spawns(_client: Client, message: Message) -> None:
        command = command_input(message)
        user = _user(message)
        if not user or not _group(message):
            return
        try:
            await require_group_admin(app, message.chat.id, user.id, "can_change_info")
        except PermissionDenied as exc:
            await respond(
                message, Card("Admin access required", str(exc), emoji=Emoji.WARNING), private=True
            )
            return
        except (RPCError, OSError):
            await respond(
                message,
                Card("Cannot verify access", "Current group permissions are unavailable."),
                private=True,
            )
            return
        parts = (command.args or "").lower().split()
        if getattr(command, "command", "") == "changetime" and len(parts) == 1:
            parts = ["every", parts[0]]
        patch: dict[str, bool | int]
        if parts in (["on"], ["off"]):
            patch = {"spawn_enabled": parts[0] == "on"}
        elif (
            len(parts) == 2
            and parts[0] == "every"
            and parts[1].isdigit()
            and 20 <= int(parts[1]) <= 10000
        ):
            patch = {"spawn_every": int(parts[1])}
        else:
            await respond(
                message,
                Card(
                    "Configure summons",
                    "Use /spawns on, /spawns off, or /spawns every 80 (20–10000).",
                    emoji=Emoji.GEAR,
                ),
                private=True,
            )
            return
        now = datetime.now(UTC)
        await store.groups.ensure(message.chat.id, message.chat.title or "Group", now)
        await store.groups.link_admin(message.chat.id, user.id, now)
        group = await store.groups.update_settings(message.chat.id, user.id, patch, now)
        await respond(
            message,
            Card(
                "Summon settings saved",
                (
                    f"Summons {'on' if group['settings']['spawn_enabled'] else 'off'} · "
                    f"every {group['settings']['spawn_every']} messages."
                ),
                emoji=Emoji.CHECK,
            ),
            private=True,
        )

    @app.on_message(filters.incoming & filters.command(["checkspawn"]))
    async def check_spawn(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user or not _group(message):
            await respond(
                message, Card("Summon status", "Use /checkspawn in a group."), private=True
            )
            return
        try:
            if not await store.limiter.allow(
                f"group:{message.chat.id}:spawn-status", limit=30, seconds=60
            ):
                await respond(message, Card("Please slow down", "Try again shortly."), private=True)
                return
            group = await store.groups.get(message.chat.id)
        except PyMongoError:
            await respond(
                message, Card("Summon status unavailable", "Try again later."), private=True
            )
            return
        if not group:
            await respond(
                message,
                Card("Summon status", "An administrator can enable summons with /spawns on."),
                private=True,
            )
            return
        config = GroupSettings.model_validate(group["settings"])
        count = max(0, int(group.get("message_count", 0)))
        await respond(
            message,
            Card(
                "Summon status",
                "Cooldown and an existing active summon can delay the next appearance.",
                emoji=Emoji.SPARK,
                fields=(
                    ("Summons", "On" if config.spawn_enabled else "Off"),
                    ("Activity", f"{min(count, config.spawn_every)} / {config.spawn_every}"),
                    ("Messages remaining", str(max(0, config.spawn_every - count))),
                ),
                field_icons=(Emoji.SUMMON_BRAND, Emoji.STATS, Emoji.HOURGLASS),
            ),
            private=True,
        )

    @app.on_message(filters.incoming & filters.command(["rules"]))
    async def rules(_client: Client, message: Message) -> None:
        if not _group(message):
            return
        group = await store.groups.get(message.chat.id)
        rules_text = group["settings"].get("rules", "") if group else ""
        await respond(
            message,
            Card(
                "Group rules",
                rules_text or "This group has not set its rules yet.",
                emoji=Emoji.RULES,
            ),
        )

    @app.on_message(filters.incoming & filters.command(["setrules"]))
    async def set_rules(_client: Client, message: Message) -> None:
        command = command_input(message)
        user = _user(message)
        if not user or not _group(message):
            return
        try:
            await require_group_admin(app, message.chat.id, user.id, "can_change_info")
        except PermissionDenied as exc:
            await respond(
                message, Card("Admin access required", str(exc), emoji=Emoji.WARNING), private=True
            )
            return
        except (RPCError, OSError):
            await respond(
                message,
                Card("Cannot verify access", "Current group permissions are unavailable."),
                private=True,
            )
            return
        text = (command.args or "").strip()
        if not text or len(text) > 3000:
            await respond(
                message,
                Card(
                    "Set the rules",
                    "Use /setrules followed by up to 3000 characters.",
                    emoji=Emoji.RULES,
                ),
                private=True,
            )
            return
        now = datetime.now(UTC)
        await store.groups.ensure(message.chat.id, message.chat.title or "Group", now)
        await store.groups.update_settings(message.chat.id, user.id, {"rules": text}, now)
        await respond(
            message,
            Card("Rules saved", "Members can read them with /rules.", emoji=Emoji.CHECK),
            private=True,
        )

    async def notes_rate_allowed(
        message: Message, user_id: int, action: str, *, public: bool = False
    ) -> bool:
        try:
            allowed = await store.limiter.allow(
                f"user:{user_id}:notes_{action}", limit=12 if action == "read" else 20, seconds=60
            )
            if allowed and public:
                allowed = await store.limiter.allow(
                    f"group:{message.chat.id}:notes_public", limit=40, seconds=60
                )
        except PyMongoError:
            await respond(
                message,
                Card("Notes unavailable", "Storage is unavailable; try again later."),
                private=True,
            )
            return False
        if not allowed:
            await respond(
                message,
                Card("Please slow down", "Too many note requests. Try again shortly."),
                private=True,
            )
        return bool(allowed)

    async def note_error(message: Message, exc: Exception, *, writing: bool = False) -> None:
        if isinstance(exc, (PermissionDenied, ValueError, NoteConflict)):
            text = str(exc)
        elif isinstance(exc, LookupError):
            text = (
                "The note or connected group was not found. "
                "An admin can connect the group with /manage."
            )
        elif isinstance(exc, NoteStorageError):
            text = "Stored notes need operator review."
        elif isinstance(exc, (RPCError, OSError)):
            text = "Could not verify current group access."
        elif isinstance(exc, PyMongoError) and writing:
            text = "Could not confirm the change. Review notes before retrying."
        else:
            text = "Could not read notes; try again later."
        await respond(message, Card("Notes unavailable", text, emoji=Emoji.WARNING), private=True)

    @app.on_message(filters.incoming & filters.command(["notes"]))
    async def list_notes(_client: Client, message: Message) -> None:
        user = _user(message)
        if not user:
            return
        if not _group(message):
            await respond(
                message,
                Card(
                    "Group notes", "Use /notes in the group you want to explore.", emoji=Emoji.NOTE
                ),
            )
            return
        if not await notes_rate_allowed(message, user.id, "read", public=True):
            return
        try:
            names = await GroupNotesService(app, store.notes).list_for_member(
                message.chat.id, user.id
            )
        except (
            PermissionDenied,
            ValueError,
            LookupError,
            NoteStorageError,
            RPCError,
            OSError,
            PyMongoError,
        ) as exc:
            await note_error(message, exc)
            return
        text = (
            "\n".join(f"/note {name}" for name in names)
            if names
            else "No notes yet. A group admin can add one."
        )
        await respond(message, Card("Group notes", text, emoji=Emoji.NOTE), standard=True)

    @app.on_message(filters.incoming & filters.command(["note"]))
    async def show_note(_client: Client, message: Message) -> None:
        command = command_input(message)
        user = _user(message)
        if not user:
            return
        if not _group(message):
            await respond(
                message,
                Card("Group notes", "Use /note <name> in the relevant group.", emoji=Emoji.NOTE),
            )
            return
        name = (command.args or "").strip().lower()
        if not await notes_rate_allowed(message, user.id, "read", public=True):
            return
        try:
            note = await GroupNotesService(app, store.notes).get_for_member(
                message.chat.id, user.id, name
            )
        except (
            PermissionDenied,
            ValueError,
            LookupError,
            NoteStorageError,
            RPCError,
            OSError,
            PyMongoError,
        ) as exc:
            await note_error(message, exc)
            return
        # Preserve plain-text line breaks and escape all user-authored markup.
        await respond(
            message,
            Card(f"Note · {note['name']}", note["content"], emoji=Emoji.NOTE),
            standard=True,
        )

    @app.on_message(filters.incoming & filters.command(["noteinfo"]))
    async def note_version(_client: Client, message: Message) -> None:
        command = command_input(message)
        user = _user(message)
        if not user or not _group(message):
            return
        name = (command.args or "").strip().lower()
        if not await notes_rate_allowed(message, user.id, "read"):
            return
        try:
            note = await GroupNotesService(app, store.notes).get_for_admin(
                message.chat.id, user.id, name
            )
        except (
            PermissionDenied,
            ValueError,
            LookupError,
            NoteStorageError,
            RPCError,
            OSError,
            PyMongoError,
        ) as exc:
            await note_error(message, exc)
            return
        await respond(
            message,
            Card(
                f"Note revision · {name}",
                "Use this revision with /editnote or /delnote. Notes are public in group messages.",
                emoji=Emoji.NOTE,
                fields=(("Revision", note["revision"]),),
                field_icons=(Emoji.PRIVATE,),
            ),
            private=True,
        )

    @app.on_message(filters.incoming & filters.command(["savenote"]))
    async def save_note(_client: Client, message: Message) -> None:
        command = command_input(message)
        user = _user(message)
        if not user or not _group(message):
            return
        parts = (command.args or "").split(maxsplit=1)
        if len(parts) != 2:
            await respond(
                message,
                Card(
                    "Create a note",
                    "Use /savenote <name> <text>. Notes are public to the group.",
                    emoji=Emoji.NOTE,
                ),
                private=True,
            )
            return
        if not await notes_rate_allowed(message, user.id, "write"):
            return
        try:
            await GroupNotesService(app, store.notes).save(
                message.chat.id, user.id, parts[0].lower(), parts[1], None, datetime.now(UTC)
            )
        except (
            PermissionDenied,
            ValueError,
            LookupError,
            NoteConflict,
            NoteStorageError,
            RPCError,
            OSError,
            PyMongoError,
        ) as exc:
            await note_error(message, exc, writing=True)
            return
        await respond(
            message,
            Card(
                "Note created",
                "Members can read it with /note. Use /noteinfo to edit or remove it.",
                emoji=Emoji.DONE,
            ),
            private=True,
        )

    @app.on_message(filters.incoming & filters.command(["editnote"]))
    async def edit_note(_client: Client, message: Message) -> None:
        command = command_input(message)
        user = _user(message)
        if not user or not _group(message):
            return
        parts = (command.args or "").split(maxsplit=2)
        if len(parts) != 3:
            await respond(
                message,
                Card(
                    "Edit a note",
                    "Use /editnote <name> <revision> <text>. "
                    "Get the current revision with /noteinfo <name>.",
                    emoji=Emoji.NOTE_EDIT,
                ),
                private=True,
            )
            return
        if not await notes_rate_allowed(message, user.id, "write"):
            return
        try:
            await GroupNotesService(app, store.notes).save(
                message.chat.id, user.id, parts[0].lower(), parts[2], parts[1], datetime.now(UTC)
            )
        except (
            PermissionDenied,
            ValueError,
            LookupError,
            NoteConflict,
            NoteStorageError,
            RPCError,
            OSError,
            PyMongoError,
        ) as exc:
            await note_error(message, exc, writing=True)
            return
        await respond(
            message,
            Card("Note updated", "Members can read the new text with /note.", emoji=Emoji.DONE),
            private=True,
        )

    @app.on_message(filters.incoming & filters.command(["delnote"]))
    async def delete_note(_client: Client, message: Message) -> None:
        command = command_input(message)
        user = _user(message)
        if not user or not _group(message):
            return
        parts = (command.args or "").split()
        if len(parts) != 3 or not re.fullmatch(r"[a-f0-9]{32}", parts[1]) or parts[2] != "confirm":
            await respond(
                message,
                Card(
                    "Confirm note removal",
                    "Use /delnote <name> <revision> confirm. "
                    "Get the current revision with /noteinfo <name>.",
                    emoji=Emoji.NOTE_DELETE,
                ),
                private=True,
            )
            return
        if not await notes_rate_allowed(message, user.id, "write"):
            return
        try:
            await GroupNotesService(app, store.notes).delete(
                message.chat.id, user.id, parts[0].lower(), parts[1], datetime.now(UTC)
            )
        except (
            PermissionDenied,
            ValueError,
            LookupError,
            NoteConflict,
            NoteStorageError,
            RPCError,
            OSError,
            PyMongoError,
        ) as exc:
            await note_error(message, exc, writing=True)
            return
        await respond(
            message,
            Card(
                "Note removed",
                "The removal is recorded in the group activity log.",
                emoji=Emoji.DONE,
            ),
            private=True,
        )

    @app.on_message(
        filters.incoming & filters.command(["ban", "unban", "mute", "unmute", "kick", "warn"])
    )
    async def moderate(_client: Client, message: Message) -> None:
        command = command_input(message)
        user = _user(message)
        reply = message.reply_to_message
        target = reply.from_user if reply and not reply.sender_chat else None
        if not user or not _group(message) or not target or target.is_bot:
            await respond(
                message,
                Card(
                    "Choose a member",
                    "Reply to a member's message with the moderation command.",
                    emoji=Emoji.SHIELD,
                ),
                private=True,
            )
            return
        parts = (command.args or "").split()
        duration = _duration(parts[0]) if parts and command.command in {"ban", "mute"} else None
        reason = " ".join(parts[1:] if duration else parts)
        try:
            result = await ModerationService(app, store.groups).act(
                message.chat.id,
                user.id,
                target.id,
                command.command,
                reason,
                datetime.now(UTC),
                duration,
            )
        except (PermissionDenied, ValueError) as exc:
            await respond(
                message, Card("Action not allowed", str(exc), emoji=Emoji.WARNING), private=True
            )
            return
        except (RPCError, OSError, PyMongoError):
            # A network or storage failure may follow an applied Telegram action.
            # Do not retry or assert success; let the administrator inspect it.
            await respond(
                message,
                Card("Result uncertain", "Review Telegram and the action log before retrying."),
                private=True,
            )
            return
        suffix = (
            f" Warnings: {result['warning_count']}."
            if result.get("warning_count") is not None
            else ""
        )
        await respond(
            message,
            Card(
                "Moderation recorded",
                f"{command.command.title()} applied to {target.first_name}.{suffix}",
                emoji=Emoji.ADMIN_SHIELD,
            ),
        )

    async def warning_rate_allowed(message: Message, actor_id: int, action: str) -> bool:
        try:
            allowed = await store.limiter.allow(
                f"user:{actor_id}:{action}",
                limit=30 if action == "warning_review" else 20,
                seconds=60,
            )
        except PyMongoError:
            await respond(
                message,
                Card("Warnings unavailable", "Storage is unavailable; try the review later."),
                private=True,
            )
            return False
        if not allowed:
            await respond(
                message,
                Card("Please slow down", "Too many warning requests. Try again shortly."),
                private=True,
            )
        return bool(allowed)

    def warning_reply_target(message: Message) -> Any | None:
        reply = message.reply_to_message
        if not reply or getattr(reply, "sender_chat", None):
            return None
        target = reply.from_user
        return target if target and not target.is_bot else None

    @app.on_message(filters.incoming & filters.command(["warnings"]))
    async def review_warnings(_client: Client, message: Message) -> None:
        command = command_input(message)
        user = _user(message)
        target = warning_reply_target(message)
        if not user or not _group(message) or not target:
            await respond(
                message,
                Card("Choose a member", "Reply to a member's message with /warnings."),
                private=True,
            )
            return
        before = (command.args or "").strip() or None
        if before is not None and not re.fullmatch(r"[a-f0-9]{32}", before):
            await respond(
                message, Card("Invalid cursor", "Use the cursor from /warnings."), private=True
            )
            return
        if not await warning_rate_allowed(message, user.id, "warning_review"):
            return
        try:
            page = await ModerationService(app, store.groups).list_warnings(
                message.chat.id, user.id, target.id, before=before, limit=5
            )
        except (PermissionDenied, ValueError, LookupError) as exc:
            await respond(message, Card("Cannot review warnings", str(exc)), private=True)
            return
        except (RPCError, OSError, PyMongoError):
            await respond(
                message,
                Card("Warnings unavailable", "Could not verify access or read warning records."),
                private=True,
            )
            return
        fields = tuple(
            (
                f"ID {item['id']}",
                f"{item.get('reason') or 'No reason provided.'}\n"
                f"Issued by {item['actor_id']} · {item['created_at'].isoformat()}",
            )
            for item in page["items"]
        )
        cursor = page.get("next_cursor")
        details = (
            ("Older warnings", f"Reply to the member with /warnings {cursor}.") if cursor else None
        )
        await respond(
            message,
            Card(
                "Active warnings",
                f"Member {target.id} has {page['active_count']} active warnings. "
                "No automatic penalty is applied.",
                emoji=Emoji.ALERT,
                fields=fields,
                details=details,
            ),
            private=True,
        )

    @app.on_message(filters.incoming & filters.command(["unwarn"]))
    async def unwarn(_client: Client, message: Message) -> None:
        command = command_input(message)
        user = _user(message)
        target = warning_reply_target(message)
        if not user or not _group(message) or not target:
            await respond(
                message,
                Card("Choose a member", "Reply to the warned member's message with /unwarn."),
                private=True,
            )
            return
        parts = (command.args or "").split(maxsplit=2)
        if len(parts) < 2 or not re.fullmatch(r"[a-f0-9]{32}", parts[0]) or parts[1] != "confirm":
            await respond(
                message,
                Card("Confirm the correction", "Reply with /unwarn <warning ID> confirm [reason]."),
                private=True,
            )
            return
        if not await warning_rate_allowed(message, user.id, "warning_revoke"):
            return
        try:
            await ModerationService(app, store.groups).revoke_warning(
                message.chat.id,
                user.id,
                target.id,
                parts[0],
                parts[2] if len(parts) > 2 else "",
                datetime.now(UTC),
            )
        except (PermissionDenied, ValueError, LookupError) as exc:
            await respond(message, Card("Cannot revoke warning", str(exc)), private=True)
            return
        except (RPCError, OSError):
            await respond(
                message,
                Card("Cannot verify access", "Current group permissions are unavailable."),
                private=True,
            )
            return
        except PyMongoError:
            await respond(
                message,
                Card(
                    "Result uncertain",
                    "Could not confirm the correction. Review warnings before retrying.",
                ),
                private=True,
            )
            return
        await respond(
            message,
            Card(
                "Warning revoked",
                "The correction is recorded; Telegram restrictions were not changed.",
                emoji=Emoji.CHECK,
            ),
            private=True,
        )
