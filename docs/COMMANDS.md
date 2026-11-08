# Telegram commands

The bot uses aiogram as its only update ingress. The menu lists common commands; aliases and admin commands may be typed even when not shown. The group must receive relevant messages, and automatic spawns start **off**. Group administrators can connect it with `/manage`; privacy mode and bot permissions affect what Telegram delivers.

| Command | Scope and behavior |
| --- | --- |
| `/start` | Welcome; in private chat, shows the Mini App button if an HTTPS URL is configured. |
| `/help` (`/commands`) | Collection, exchange and group guide. |
| `/summon <name>` (`/guess`) | Guess the current group spawn. Answers are normalized; guesses are rate-limited and only one claim can succeed. |
| `/collection` (`/inventory`) | Personal inventory summary, first 12 unique characters. Full collection is in the Mini App. |
| `/profile` | Personal points, level, collection count and catch count. |
| `/daily` | Claim daily points/XP once per UTC date. |
| `/top` (`/leaderboard`) | Public catch-count leaderboard. |
| Reply `/trade <your-character-id> <their-character-id>` | Propose a two-party trade in a group. The sender must own their offer; the recipient's ownership is checked privately on acceptance. Only the recipient may accept; either participant may cancel before the 15-minute expiry. |
| Reply `/gift <your-character-id>` | Offer one owned character in a group; only the recipient can accept, and either participant can cancel before expiry. |
| `/manage` | A current group administrator links the group to their Mini App; this link never replaces future permission checks. |
| `/spawns on`, `/spawns off`, `/spawns every <20–10000>` | Change group spawn settings; requires `can_change_info`. |
| `/rules` | Read the group rules. |
| `/setrules <text>` | Set group rules (up to 3000 characters); requires `can_change_info`. |
| `/notes`, `/note <name>` | List public note names or post one note into this group. Each request checks current Telegram membership; note responses are visible to anyone who can see the group. |
| `/savenote <name> <text>` | Create a new public text note; requires current `can_change_info`. This never overwrites an existing note. Use `/manage` first to connect the group. |
| `/noteinfo <name>` | Privately request the current revision of a note; requires current `can_change_info`. The response uses ephemeral/DM fallback. |
| `/editnote <name> <revision> <text>` | Replace a note only if the revision from `/noteinfo` still matches; requires current `can_change_info`. |
| `/delnote <name> <revision> confirm` | Permanently remove a matching note; requires current `can_change_info`, its complete current revision, and the literal `confirm`. |
| Reply `/warn`, `/ban`, `/unban`, `/mute`, `/unmute`, `/kick` | Group moderation; actor and bot must have current restrict rights. Admins, the actor and the bot are excluded as targets. An optional reason follows the command; for ban/mute, a duration such as `30m`, `2h`, or `7d` may precede the reason. |
| Reply `/warnings [cursor]` | Review up to five active warnings for the replied-to member. Use the returned cursor to request older entries; the actor's current `can_restrict_members` right is checked for every page. Responses use ephemeral/DM fallback, but delivery is not guaranteed. |
| Reply `/unwarn <warning-id> confirm [reason]` | Correct one warning for the replied-to member. The literal `confirm` is required; use the complete ID from `/warnings`. Current actor restrict rights are checked again. The warning is retained and the correction is audited; no Telegram restriction is changed. |

### Public notes

A group can store at most 30 notes, each containing 1–1200 Unicode characters of plain text. Names use 1–32 lowercase ASCII letters, digits, `_` or `-`; bot commands normalize names to lowercase. `/notes` lists names, and `/note <name>` posts escaped text without executing HTML or template substitutions. Notes themselves have no automatic triggers, attachments, imports or secret/private storage; auto replies are configured separately. Telegram clients may still recognize URLs in text. **Anyone who can see group messages may see, copy or forward a posted note; never store secrets or private information.** A typed `/savenote` or `/editnote` command and its text can also be visible in the group. Anonymous/channel senders and bots are not accepted.

A human requester must still be a member when `/notes` or `/note` is checked. Telegram only guarantees [`getChatMember` for other users when the bot is a group administrator](https://core.telegram.org/bots/api#getchatmember); failed membership lookups fail closed, so grant the minimum appropriate role and test in a dedicated group. In the Mini App use **Groups → Notes** to create, edit or explicitly confirm deletion; API reads and mutations require a live `can_change_info` check. Group links and note revision IDs are not authorization. Permission can change between Telegram's check and the MongoDB transaction.

Creation requires an absent name. Edits and deletions require the revision currently displayed by `/noteinfo` or loaded by the Mini App; stale revisions are rejected, rather than overwriting another admin's change. If a write times out, its result may be uncertain: review notes and activity before retrying. The Mini App preserves a copyable in-memory draft on conflicts or uncertain writes and asks before common navigation discards it; browsers/Telegram webviews may not reliably warn before closing or reloading, and drafts are not persisted. Deletion removes the current note text permanently from SUMMON; audit records retain only name, actor, action, revision and timestamp, not the text. Backups require separate operator authorization. Preview notes are synthetic and all writes are disabled.

Note requests use shared fixed one-minute buckets: the bot permits up to 12 note reads per user and 40 public note reads per group, the API permits 30 reads per user, and writes are limited to 20 per user. Bot and API user buckets are shared; counts can burst across a bucket boundary. Limits and Telegram delivery remain unverified live.

### Public auto replies

Use **Groups → Auto replies** in the Mini App after `/manage`. Admins need current `can_change_info` for every read and change; there are no bot commands for editing these rules. A group can store 20 rules with distinct normalized exact triggers, a plain-text reply and an enabled flag. New rules start disabled. Triggers use 2–64 Unicode code points after server-side NFKC, casefolding and whitespace normalization; replies allow 1–800 code points. Names use the same safe lowercase ASCII slug rules as notes. No regex, partial matching, attachments or template substitutions are supported.

**Triggers and replies are public: never include credentials or private information.** Telegram must deliver ordinary group messages to the bot; privacy mode and privileges affect this. Replies are attempted only for new human-written text after protection, never for commands, forwards, edits, bots, anonymous senders or ephemeral messages. They are escaped, posted to the same topic, and limited to three per user and 15 per group per fixed minute. Fixed-window boundary bursts are possible. A persisted 48-hour claim reduces duplicate attempts on replay, but replies can be suppressed or lost and are not guaranteed. A message already being processed can use the previous rule version after an admin changes it.

The Mini App requires explicit deletion confirmation and guards in-memory drafts on common navigation; browser or Telegram webview close warnings are best effort. Edits/deletes require the displayed revision. If an edit or deletion is uncertain, review current rules and audit before retrying. Audit retains rule name, actor, action, revision and timestamp, not the trigger or reply. Direct API delete requests rely on authorization and revision rather than a separate confirmation challenge. Live Telegram delivery and rights remain unverified.

Warning **issuance** still checks the actor, bot and target. Review and revocation are database-record operations, not Telegram restrictions: they require the actor's current restrict right but do not require the bot's restrict right or reject a target who later became an administrator. In the Mini App, use **Groups → Moderation → Review warnings**, enter the member ID, and explicitly confirm a revocation. These actions are rate-limited and the sample preview is read-only. A typed group command and its optional reason may be visible in the group even when the bot's response is private; avoid putting sensitive reasons in public command text. The result is scoped to the replied-to member and group. Anonymous/channel senders are not accepted as members for these commands.

Only active warnings are counted; existing records without a revocation field remain active. Warnings do not automatically impose a penalty. Revoke and audit writes use one MongoDB transaction, but a timeout can leave the caller uncertain; review the warning and audit before retrying, rather than automatically repeating it. Unmute restores the group's current default permissions. Kick is a ban followed by unban and can partially succeed. Telegram actions and MongoDB audit writes cannot be atomic together. If a result is uncertain, inspect Telegram and audit state before retrying.

The Mini App's **Welcome & rules** section also provides an optional goodbye message, disabled by default. It can fire on a leave or removal, excludes bots, and is capped per member and group in fixed one-minute buckets. Changing it requires the current `can_change_info` right; actual Telegram service-event delivery is not yet verified.

Configured link, keyword and mention rules inspect delivered member messages, commands and edits; flood and duplicate rules count new messages only. Administrators are exempt, and the bot checks its current delete right before deleting. Flagged commands from non-admins are not executed if those checks or deletion fail. Only confirmed deletions are audited. Telegram privacy mode and permissions limit which updates the bot receives; anonymous/channel `sender_chat` messages, bots, service events and rich-message internals are not fully covered. Ephemeral inputs are not deleted as ordinary group messages. These protections have not been validated in a live Telegram group.

The personal group menu entries `/collection`, `/profile`, `/daily` and `/manage` are registered as ephemeral where supported. Warning-review and correction responses also request private/ephemeral delivery, although their typed commands are not registered in that menu. If an ephemeral send is definitely rejected, the bot attempts a private DM; if the user has not started the bot, it sends only a generic public prompt. Telegram does not guarantee delivery of ephemeral messages. No live command or eligibility test has been performed in this checkout.
