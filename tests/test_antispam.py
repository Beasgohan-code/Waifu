from summon.models import GroupSettings
from summon.services.antispam import static_spam_reason


def test_link_filter_handles_telegram_and_http_urls():
    settings = GroupSettings(link_filter=True)
    assert static_spam_reason("Visit https://example.com", settings) == "link"
    assert static_spam_reason("Join t.me/example", settings) == "link"
    assert static_spam_reason("Hello everyone", settings) is None


def test_keyword_normalization_and_mentions():
    settings = GroupSettings(blocked_keywords=["ＦＲＥＥ offer"], mention_limit=1)
    assert static_spam_reason("A free OFFER for you", settings) == "keyword"
    assert static_spam_reason("Hi @alice and @bobby", settings) == "mentions"
    assert static_spam_reason("Hi @alice", settings) is None


def test_disabled_filters_do_not_block():
    assert static_spam_reason("https://example.com @alice", GroupSettings()) is None


async def test_flood_and_duplicate_thresholds_use_bounded_time_buckets():
    from datetime import UTC, datetime

    from summon.services.antispam import SpamGuard

    class Counters:
        def __init__(self):
            self.rows = {}

        async def find_one_and_update(self, query, update, **_kwargs):
            key = query["_id"]
            row = self.rows.setdefault(
                key, {"count": 0, "expires_at": update["$setOnInsert"]["expires_at"]}
            )
            row["count"] += 1
            return row

    from types import SimpleNamespace

    counters = Counters()
    guard = SpamGuard(SimpleNamespace(rate_limits=counters))
    now = datetime(2026, 11, 2, tzinfo=UTC)
    settings = GroupSettings(flood_enabled=True, flood_limit=3, flood_window_seconds=10)
    assert [await guard.inspect(-100, 42, "hello", settings, now) for _ in range(4)] == [
        None,
        None,
        None,
        "flood",
    ]
    duplicate = GroupSettings(duplicate_enabled=True, duplicate_limit=2)
    assert [await guard.inspect(-100, 43, "repeat", duplicate, now) for _ in range(3)] == [
        None,
        None,
        "duplicate",
    ]
    assert all(row["expires_at"] > now for row in counters.rows.values())


def test_link_and_mention_entities_cannot_hide_behind_display_text():
    from types import SimpleNamespace

    settings = GroupSettings(link_filter=True)
    assert (
        static_spam_reason("click here", settings, entities=[SimpleNamespace(type="text_link")])
        == "link"
    )
    assert (
        static_spam_reason("example.com", settings, entities=[SimpleNamespace(type="url")])
        == "link"
    )
    mentions = GroupSettings(mention_limit=1)
    entities = [SimpleNamespace(type="text_mention"), SimpleNamespace(type="text_mention")]
    assert static_spam_reason("Alice and Bob", mentions, entities=entities) == "mentions"


def test_text_mentions_are_not_double_counted_even_after_utf16_emoji():
    from aiogram.types import MessageEntity

    settings = GroupSettings(mention_limit=2)
    text = "🚀 @alice and @bobby"
    # Telegram entity offsets use UTF-16 code units, not Python code points.
    entities = [
        MessageEntity(
            type="text_mention",
            offset=3,
            length=6,
            user={"id": 1, "is_bot": False, "first_name": "Alice"},
        )
    ]
    assert static_spam_reason(text, settings, entities=entities) is None


async def test_edit_does_not_increment_flood_or_duplicate_counters():
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from summon.services.antispam import SpamGuard

    counters = SimpleNamespace(find_one_and_update=AsyncMock())
    guard = SpamGuard(SimpleNamespace(rate_limits=counters))
    settings = GroupSettings(flood_enabled=True, duplicate_enabled=True, link_filter=True)
    now = datetime.now(UTC)
    assert (
        await guard.inspect(-100, 42, "ordinary edited text", settings, now, count_activity=False)
        is None
    )
    assert (
        await guard.inspect(-100, 42, "https://example.com", settings, now, count_activity=False)
        == "link"
    )
    counters.find_one_and_update.assert_not_awaited()
