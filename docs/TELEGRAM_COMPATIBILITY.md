# Telegram, Wzgram and Richgram compatibility

Researched 2026-11-02; Mini App bottom-button support reviewed 2026-11-06. [Telegram Bot API changelog](https://core.telegram.org/bots/api-changelog), [Bot API reference](https://core.telegram.org/bots/api), [Mini App validation and BottomButton](https://core.telegram.org/bots/webapps), [Wzgram](https://github.com/rjriajul/wzgram) and [Richgram](https://github.com/Badmunda05/richgram) are the authoritative starting points. Library signatures and mocked serialization are **not** a successful live Telegram send.

## Installed stack and ingress

| Component | Pinned / role | Locally verified | Caveat |
| --- | --- | --- | --- |
| aiogram | 3.31.0; the single Bot API polling ingress and regular/Bot API rich sender | `SendRichMessage`, `InputRichMessage`, ephemeral parameters, command and button fields are present; mocked tests pass. | Actual server, account and client rendering untested. |
| Wzgram | 3.1.6; optional outbound MTProto connection only | `Client(no_updates=True, in_memory=True)` has no handlers, and `send_rich_message` accepts `rich_text`. Installed metadata and signatures are checked at bot startup. | No live login/send or end-to-end speed verification. The package remains installed even if its connection is disabled. |
| WarpCrypto | 2.1.0; Wzgram crypto dependency | Installed and `pip check` passes. | Acceleration does not prove lower end-to-end bot latency. |
| Richgram | 0.2.0 builder subset, vendored and adapted, MIT notice in `summon/_vendor/RICHGRAM_LICENSE` | Heading, table and details serialize into Wzgram `InputRichMessage`; SUMMON's sender uses Wzgram's `rich_text` argument. | We do **not** install the Richgram distribution or use its stock retrying sender. |

The normal Richgram distribution depends on Kurigram; Kurigram, Pyrogram, Pyrofork and Wzgram can overwrite the shared `pyrogram` namespace. Do **not** install them together in this environment. `pip install -U wzgram` by itself does not resolve that conflict and may invalidate the tested pin. Prefer a clean virtual environment and `pip install -e '.[dev]'`; `summon-bot` checks the package namespace and fails closed if a known conflict or incompatible signature is present. See [third-party notices](THIRD_PARTY_NOTICES.md).

## Feature matrix

| Feature | Evidence / SUMMON behavior | Verification and limitations |
| --- | --- | --- |
| Rich headings, paragraphs, details, tables | Telegram Bot API 10.1 introduced rich messages; SUMMON builds escaped HTML through the adapted builders. Native Bot API rich is on by default (`SUMMON_BOT_API_RICH_ENABLED=true`). Optional Wzgram sends rich cards without keyboards (`SUMMON_RICH_ENABLED=true`). | Unit serialization and definite-rejection fallback tested; no live parse/render test. |
| Compact tables | Bot API 10.3 added `is_compact`; SUMMON emits `<table bordered compact>` in Rich HTML. | Syntax checked against documented format and mocks, not a live Telegram client. An older server may reject and receive a plain message instead. |
| Custom emoji | SUMMON uses documented `<tg-emoji emoji-id="…">Unicode</tg-emoji>` and selected IDs from the seven user-supplied palettes below in headings, field labels and photo captions. | ID, account entitlement, context, permissions and pack availability matter. Eligible definite rejections can fall back to Unicode; mocks are not eligibility proof. Set `SUMMON_PREMIUM_EMOJI_ENABLED=false` to use Unicode instead. |
| Keyboards and button styling | aiogram supports inline keyboard `style` and `icon_custom_emoji_id`; Bot API rich sends keep their original callback/web-app actions. When premium emoji are disabled, applicable button labels receive Unicode prefixes. | On an eligible definite rejection, fallback strips visual extras and preserves callback/web-app actions. A generic `BUTTON_TYPE_INVALID` on the standard sender does not prove an emoji error and is not retried as one. Rich **embedded button blocks** introduced in 10.3 are not used; Wzgram sends with aiogram keyboards are deliberately avoided. |
| Mini App native bottom button | On the home view, an optional Telegram `MainButton` opens the real collection. Its `icon_custom_emoji_id` is supplied as a string when premium emoji are enabled and the bridge reports Bot API 9.5+; older/disabled cases use a Unicode label. The normal in-app controls remain available. | Mock bridge tests cover navigation, cleanup, version checks and fallback, not live clients or entitlement. The shortcut hides while the detail dialog or navigation overlay is open. The Mini App's HTML itself does not render these IDs as emoji artwork; no sticker files are proxied or copied. |
| Ephemeral commands/messages | Bot API 10.2 introduced ephemeral interactions and 10.3 exposes `ephemeral_message_parameters`. Private group commands are marked `is_ephemeral` in the group menu when accepted; incoming ephemeral IDs are used as reply triggers. | Available only in eligible group contexts; callback/ephemeral-reply triggers have a 15-second window, and admin-initiated messages have different rules. Telegram says delivery is not guaranteed. Rejected sends fall back to DM; no live test. |
| Rich edits, drafts, media blocks, embedded button blocks, expandable quotes | Documented in recent Bot API/library versions. | Not integrated into SUMMON's production flow; do not claim they work. Test explicitly before adding them. |
| Fallback and duplicate avoidance | A definite bad request can fall back to standard Bot API text; network timeouts and other ambiguous failures propagate instead of producing a duplicate. | Unit tested only. A plain fallback can still fail due to permissions or unavailable DM. |

A local AES-IGE microbenchmark (five-run medians, 10 × 64 KiB, matching ciphertext/decryption) measured about 0.000758 s for Wzgram/WarpCrypto, 0.003429 s for Kurigram/TgCrypto, and 1.179358 s for Kurigram/pure Python AES in this sandbox. This narrow CPU test is **not** a bot, network, transfer, deployment or user-perceived speed benchmark; no whole-application speedup is claimed.

## User-supplied emoji palettes

The ordered lists are retained as glyph/ID pairs in `summon/bot/emoji_palette.py`, `summon/bot/emoji_extra_palettes.py` and `summon/bot/emoji_summon_ios.py`. IDs are opaque **strings**; do not convert them to JavaScript numbers, which cannot represent these values exactly. The registry is immutable and uses the user's original 1-based numbering:

| Registry key | User-provided pack label | Entries |
| --- | --- | ---: |
| `vector` | Emoji Vector Icons | 118 |
| `emoji_fan` | Emoji_Fan / EmojiPersian | 200 |
| `rijif` | rijif | 200 |
| `boseh` | Boseh / NewEmojis | 200 |
| `news` | News Emoji | 100 |
| `summon_bot` | Summon bot | 60 |
| `telegram_ios` | Telegram iOS Icons | 100 |
| | **Total** | **978** |

Examples: `palette_icon("rijif", 31)` returns the crown glyph and its ID; `vector_icon(3)` returns the sparkle pair. The bot selects relevant icons rather than displaying all entries: earlier Vector, Emoji_Fan, rijif, Boseh and News roles are retained; Summon bot icons mark branding, collection, rarity, rewards and gifts; iOS icons mark notes, rules, privacy and administration. Other supplied pairs remain available for future relevant UI. Every registered ID has a Unicode button fallback. Regression fixtures compare all 660 pairs from the latest five supplied lists, including numbering and fallback glyphs; the 318 earlier Vector/Emoji_Fan entries also have count and sampled-pair tests. The comparison is against transcribed fixtures, not an independent certification of the source. Boseh entry 119 was corrected to the ID in the latest supplied list.

This is metadata supplied by the user, not proof of rights, pack permanence, ID validity or bot entitlement. See [third-party notices](THIRD_PARTY_NOTICES.md). If eligible, Telegram can show custom emoji; clients and accounts may differ. Disabling premium emoji uses glyphs in cards, photo captions and button labels. On a *definite* emoji/entity rejection, standard sends may retry without custom emoji; a native rich-message rejection can fall back to a plain card and keyboard. Network timeouts and other ambiguous delivery errors are never retried automatically. A rejected ephemeral response may need a DM, which itself can be unavailable.

## Live compatibility checklist (not performed)

With an authorized test bot and isolated test group, verify: group command registration and privacy mode; native rich headings/tables/details and photo captions with Unicode and eligible premium emoji; a rich message with trade/gift keyboard callbacks; the Mini App native collection shortcut, its icon and Unicode fallback on supported and older clients; Wzgram startup/no competing updates and outbound `rich_text`; rejected-rich/emoji fallbacks; ephemeral replies both as admin and using an incoming ephemeral command; unavailable DMs; timeout/ambiguous delivery; safe webhook-to-polling cutover. Record actual Telegram/client versions and observed outcomes. Do not use production users or credentials for this rehearsal.
