import pytest

from summon.config import Settings


def test_default_config_contains_no_credentials_or_database_uri():
    settings = Settings(_env_file=None)
    assert settings.bot_token.get_secret_value() == ""
    assert settings.session_secret.get_secret_value() == ""
    assert settings.mongo_uri.get_secret_value() == ""
    assert settings.telegram_api_hash.get_secret_value() == ""
    assert settings.operator_ids == []


def test_production_requires_real_secret_and_database_settings():
    with pytest.raises(ValueError):
        Settings(environment="production", _env_file=None)
    with pytest.raises(ValueError):
        Settings(
            environment="production", bot_token="123:token", session_secret="x" * 40, _env_file=None
        )
    settings = Settings(
        environment="production",
        bot_token="123:token",
        session_secret="x" * 40,
        mongo_uri="mongodb://example.invalid/summon",
        webapp_url="https://summon.example/app",
        telegram_api_id=12345,
        telegram_api_hash="test-hash",
        _env_file=None,
    )
    assert settings.environment == "production"


def test_production_wzgram_requires_telegram_application_credentials():
    with pytest.raises(ValueError, match="SUMMON_TELEGRAM_API_ID"):
        Settings(
            environment="production",
            bot_token="123:token",
            session_secret="x" * 40,
            mongo_uri="mongodb://example.invalid/summon",
            webapp_url="https://summon.example",
            _env_file=None,
        )


def test_origins_must_be_exact_hosts_and_https_in_production():
    for bad in [
        "http://localhost.evil.example",
        "https://example.com/path",
        "https://user@example.com",
        "https://example.com?query=1",
    ]:
        with pytest.raises(ValueError):
            Settings(allowed_origins=[bad], _env_file=None)
    with pytest.raises(ValueError):
        Settings(
            environment="production",
            bot_token="123:token",
            session_secret="x" * 40,
            mongo_uri="mongodb://example.invalid/summon",
            allowed_origins=["http://localhost:3000"],
            _env_file=None,
        )
    assert Settings(
        allowed_origins=["http://localhost:3000", "https://example.com:8443"], _env_file=None
    ).allowed_origins == ["http://localhost:3000", "https://example.com:8443"]


def test_production_webapp_url_must_be_valid_https_without_credentials():
    for url in ["https://", "https://name:pass@example.com/app", "https://example.com/#fragment"]:
        with pytest.raises(ValueError):
            Settings(
                environment="production",
                bot_token="123:token",
                session_secret="x" * 40,
                mongo_uri="mongodb://example.invalid/summon",
                webapp_url=url,
                _env_file=None,
            )


@pytest.mark.parametrize("url", ["", "http://localhost:5173"])
def test_production_requires_nonempty_https_webapp_url(url):
    with pytest.raises(ValueError, match="SUMMON_WEBAPP_URL"):
        Settings(
            environment="production",
            bot_token="123:token",
            session_secret="x" * 40,
            mongo_uri="mongodb://example.invalid/summon",
            webapp_url=url,
            _env_file=None,
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com\n.evil",
        " https://example.com",
        "https://exa mple.com",
        "https://example.com\\evil",
        "https://example.com/has\ttab",
    ],
)
def test_unsafe_webapp_urls_are_rejected_without_silent_normalization(url):
    with pytest.raises(ValueError):
        Settings(webapp_url=url, _env_file=None)


@pytest.mark.parametrize(
    "origin",
    ["https://example.com\n.evil", "https://exa mple.com", "https://example.com\\evil"],
)
def test_unsafe_origins_are_rejected_without_silent_normalization(origin):
    with pytest.raises(ValueError):
        Settings(allowed_origins=[origin], _env_file=None)


def test_development_can_omit_webapp_url_or_use_localhost():
    assert Settings(_env_file=None).webapp_url == ""
    assert Settings(webapp_url="http://localhost:5173/app", _env_file=None).webapp_url == (
        "http://localhost:5173/app"
    )


def test_production_environment_requires_url_before_startup(monkeypatch):
    monkeypatch.setenv("SUMMON_ENVIRONMENT", "production")
    monkeypatch.setenv("SUMMON_BOT_TOKEN", "fake-test-token")
    monkeypatch.setenv("SUMMON_MONGO_URI", "mongodb://example.invalid/summon")
    monkeypatch.setenv("SUMMON_SESSION_SECRET", "x" * 40)
    monkeypatch.delenv("SUMMON_WEBAPP_URL", raising=False)
    with pytest.raises(ValueError, match="SUMMON_WEBAPP_URL"):
        Settings(_env_file=None)
    monkeypatch.setenv("SUMMON_WEBAPP_URL", "https://summon.example/app")
    monkeypatch.setenv("SUMMON_TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("SUMMON_TELEGRAM_API_HASH", "test-hash")
    assert Settings(_env_file=None).webapp_url == "https://summon.example/app"


def test_configuration_validation_error_text_does_not_echo_input_values():
    with pytest.raises(ValueError) as error:
        Settings(
            environment="production",
            bot_token="FAKE_BOT_TOKEN_SENTINEL",
            session_secret="too-short",
            mongo_uri="mongodb://FAKE_DATABASE_SENTINEL",
            _env_file=None,
        )
    assert "FAKE_BOT" not in str(error.value)
    assert "FAKE_DATABASE" not in str(error.value)
    assert "too-short" not in str(error.value)
    with pytest.raises(ValueError) as error:
        Settings(webapp_url="https://FAKE_URL_SENTINEL/?token=sensitive", _env_file=None)
    assert "FAKE_URL_SENTINEL" not in str(error.value)
    assert "sensitive" not in str(error.value)


@pytest.mark.parametrize(
    "host",
    [
        "*.example.com",
        "-example.com",
        "example..com",
        "example.com..",
        "999.999.999.999",
        "example.com\u202eevil",
        "example.com\u200bevil",
    ],
)
def test_webapp_and_origins_reject_invalid_or_ambiguous_hosts(host):
    with pytest.raises(ValueError):
        Settings(webapp_url=f"https://{host}", _env_file=None)
    with pytest.raises(ValueError):
        Settings(allowed_origins=[f"https://{host}"], _env_file=None)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/app",
        "https://xn--bcher-kva.example/app",
        "https://[2001:db8::1]:8443/app",
    ],
)
def test_well_formed_webapp_hosts_remain_valid(url):
    assert Settings(webapp_url=url, _env_file=None).webapp_url == url


@pytest.mark.parametrize(
    "origin",
    [
        "HTTPS://example.com",
        "https://EXAMPLE.com",
        "https://example.com:443",
        "https://bücher.example",
        "http://localhost:80",
    ],
)
def test_allowlist_requires_browser_serialized_origins(origin):
    with pytest.raises(ValueError):
        Settings(allowed_origins=[origin], _env_file=None)


@pytest.mark.parametrize(
    "origin",
    [
        "https://xn--bcher-kva.example",
        "https://example.com:8443",
        "https://[2001:db8::1]:8443",
        "http://localhost:5173",
    ],
)
def test_browser_serialized_origins_are_valid(origin):
    assert Settings(allowed_origins=[origin], _env_file=None).allowed_origins == [origin]


def test_simple_environment_aliases_and_prefixed_precedence(monkeypatch, tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("SUMMON_MONGO_URI=mongodb://dotenv.invalid/db\nBOT_TOKEN=dotenv-token\n")
    monkeypatch.setenv("MONGO_URL", "mongodb://environment.invalid/db")
    monkeypatch.setenv("BOT_TOKEN", "alias-token")
    monkeypatch.setenv("SESSION_SECRET", "x" * 40)
    monkeypatch.setenv("API_ID", "1234")
    monkeypatch.setenv("API_HASH", "fake-hash")
    monkeypatch.setenv("OWNER_ID", "42")
    settings = Settings(_env_file=dotenv)
    assert settings.mongo_uri.get_secret_value() == "mongodb://environment.invalid/db"
    assert settings.bot_token.get_secret_value() == "alias-token"
    assert settings.telegram_api_id == 1234
    assert settings.operator_ids == [42]
    monkeypatch.setenv("SUMMON_MONGO_URI", "mongodb://prefixed.invalid/db")
    monkeypatch.setenv("SUMMON_BOT_TOKEN", "")
    settings = Settings(_env_file=dotenv)
    assert settings.mongo_uri.get_secret_value() == "mongodb://prefixed.invalid/db"
    assert settings.bot_token.get_secret_value() == ""
    explicit = Settings(mongo_uri="mongodb://explicit.invalid/db", _env_file=dotenv)
    assert explicit.mongo_uri.get_secret_value() == "mongodb://explicit.invalid/db"


def test_bad_alias_values_are_not_exposed_in_errors(monkeypatch):
    monkeypatch.setenv("OWNER_ID", "FAKE_SECRET_OWNER_SENTINEL")
    with pytest.raises(ValueError) as error:
        Settings(_env_file=None)
    assert "FAKE_SECRET_OWNER_SENTINEL" not in str(error.value)


def test_simple_mixed_case_names_and_multiple_operators(monkeypatch):
    monkeypatch.setenv("Api_Id", "1234")
    monkeypatch.setenv("API_Hash", "synthetic-hash")
    monkeypatch.setenv("MONGODB_URL", "mongodb://example.invalid/summon")
    monkeypatch.setenv("DB_NAME", "collection")
    monkeypatch.setenv("TELEGRAM_SESSION_DB", "separate_sessions")
    monkeypatch.setenv("OPERATOR_IDS", "42, 101,42")
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://app.example, https://other.example")
    settings = Settings(_env_file=None)
    assert settings.telegram_api_id == 1234
    assert settings.telegram_api_hash.get_secret_value() == "synthetic-hash"
    assert settings.mongo_uri.get_secret_value() == "mongodb://example.invalid/summon"
    assert settings.mongo_database == "collection"
    assert settings.telegram_session_database == "separate_sessions"
    assert settings.operator_ids == [42, 101]
    assert settings.allowed_origins == ["https://app.example", "https://other.example"]


def test_simple_lists_allow_json_empty_and_single_owner(monkeypatch):
    monkeypatch.setenv("OPERATOR_IDS", '[7, "8"]')
    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.setenv("ALLOWED_ORIGINS", '["https://app.example"]')
    assert Settings(_env_file=None).operator_ids == [7, 8]
    assert Settings(_env_file=None).allowed_origins == ["https://app.example"]
    monkeypatch.delenv("OPERATOR_IDS")
    assert Settings(_env_file=None).operator_ids == [42]
    monkeypatch.setenv("OWNER_ID", "")
    assert Settings(_env_file=None).operator_ids == []


@pytest.mark.parametrize("value", ["0", "-1", str(2**53), "42,,43"])
def test_invalid_operator_aliases_are_rejected_without_reflecting_values(monkeypatch, value):
    monkeypatch.setenv("OPERATOR_IDS", value)
    with pytest.raises(ValueError) as error:
        Settings(_env_file=None)
    assert "42,,43" not in str(error.value)


def test_invalid_alias_list_never_echoes_the_input(monkeypatch):
    monkeypatch.setenv("OPERATOR_IDS", "[PRIVATE_TEST_SENTINEL")
    with pytest.raises(ValueError) as error:
        Settings(_env_file=None)
    assert "PRIVATE_TEST_SENTINEL" not in str(error.value)


def test_short_and_prefixed_aliases_keep_per_source_precedence(monkeypatch, tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("SUMMON_TELEGRAM_API_HASH=dotenv-hash\nSUMMON_OPERATOR_IDS=[5]\n")
    monkeypatch.setenv("API_Hash", "environment-hash")
    monkeypatch.setenv("OWNER_ID", "99")
    settings = Settings(_env_file=dotenv)
    assert settings.telegram_api_hash.get_secret_value() == "environment-hash"
    assert settings.operator_ids == [99]
    monkeypatch.setenv("SUMMON_TELEGRAM_API_HASH", "")
    monkeypatch.setenv("SUMMON_OPERATOR_IDS", "[]")
    settings = Settings(_env_file=dotenv)
    assert settings.telegram_api_hash.get_secret_value() == ""
    assert settings.operator_ids == []


def test_example_env_loads_without_credentials_or_an_operator():
    import os
    from pathlib import Path
    from unittest.mock import patch

    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(_env_file=Path(__file__).resolve().parents[1] / ".env.example")
    assert settings.environment == "development"
    assert settings.operator_ids == [] and settings.telegram_api_id is None
    assert settings.bot_token.get_secret_value() == ""
    assert settings.mongo_uri.get_secret_value() == ""
    assert settings.telegram_session_database != settings.mongo_database


def test_imgbb_and_channel_aliases_are_optional_and_mixed_case(monkeypatch):
    monkeypatch.setenv("Imgbb_key", "FAKE_IMG_HOST_KEY")
    monkeypatch.setenv("DB_CHANNEL", "-1001234567890")
    settings = Settings(_env_file=None)
    assert settings.artwork_backend == "mongo"  # The legacy backend stays unchanged.
    assert settings.imgbb_key.get_secret_value() == "FAKE_IMG_HOST_KEY"
    assert settings.db_channel == -1001234567890
    assert "FAKE_IMG_HOST_KEY" not in repr(settings)
    monkeypatch.setenv("ARTWORK_BACKEND", "imgbb")
    assert Settings(_env_file=None).artwork_backend == "imgbb"
    monkeypatch.setenv("SUMMON_IMGBB_KEY", "")
    with pytest.raises(ValueError, match="IMGBB_KEY") as error:
        Settings(_env_file=None)
    assert "FAKE_IMG_HOST_KEY" not in str(error.value)


def test_channel_and_host_configuration_fail_closed(monkeypatch):
    monkeypatch.setenv("ARTWORK_BACKEND", "imgbb")
    with pytest.raises(ValueError, match="IMGBB_KEY"):
        Settings(_env_file=None)
    monkeypatch.setenv("IMGBB_API_KEY", "test-key")
    for bad in ("0", "123", str(-(2**63)), "not-an-id"):
        monkeypatch.setenv("DB_CHANNEL_ID", bad)
        with pytest.raises(ValueError):
            Settings(_env_file=None)
    monkeypatch.setenv("DB_CHANNEL_ID", "")
    assert Settings(_env_file=None).db_channel is None


def test_optional_catbox_userhash_alias_is_secret_and_prefixed_value_wins(monkeypatch):
    monkeypatch.setenv("CATBOX_USERHASH", "FAKE_USERHASH")
    settings = Settings(_env_file=None)
    assert settings.catbox_userhash.get_secret_value() == "FAKE_USERHASH"
    assert "FAKE_USERHASH" not in repr(settings)
    monkeypatch.setenv("SUMMON_CATBOX_USERHASH", "")
    assert Settings(_env_file=None).catbox_userhash.get_secret_value() == ""


def test_backup_settings_need_only_mongo_and_exclude_session_database(monkeypatch):
    from summon.config import BackupConnectionSettings

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("MONGO_URL", "mongodb://example.invalid/summon")
    monkeypatch.setenv("MONGO_DB_NAME", "summon")
    monkeypatch.setenv("TELEGRAM_SESSION_DB", "summon_telegram")
    monkeypatch.setenv("OPERATOR_IDS", "not a list")
    settings = BackupConnectionSettings(_env_file=None)
    assert settings.mongo_uri.get_secret_value() == "mongodb://example.invalid/summon"
    assert settings.mongo_database == "summon"
    monkeypatch.setenv("TELEGRAM_SESSION_DB", "summon")
    with pytest.raises(ValueError, match="session database"):
        BackupConnectionSettings(_env_file=None)


def test_synthetic_production_deployment_uses_the_documented_short_names(monkeypatch):
    values = {
        "ENVIRONMENT": "production",
        "BOT_TOKEN": "123456:synthetic-bot-token",
        "API_ID": "12345",
        "API_HASH": "synthetic-app-hash",
        "MONGO_URL": "mongodb://example.invalid:27017/?replicaSet=synthetic",
        "MONGO_DB_NAME": "summon",
        "TELEGRAM_SESSION_DB": "summon_telegram",
        "SESSION_SECRET": "synthetic-session-secret-" + "x" * 32,
        "WEBAPP_URL": "https://miniapp.example",
        "OWNER_ID": "42",
        "ALLOWED_ORIGINS": "https://miniapp.example",
        "DB_CHANNEL": "-1001234567890",
        "WEBHOOKS_DISABLED_CONFIRMED": "true",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = Settings(_env_file=None)
    assert settings.environment == "production"
    assert settings.telegram_api_id == 12345 and settings.operator_ids == [42]
    assert (
        settings.mongo_database == "summon"
        and settings.telegram_session_database == "summon_telegram"
    )
    assert settings.allowed_origins == [settings.webapp_url]
    assert settings.webhooks_disabled_confirmed and settings.db_channel == -1001234567890
    # Host and channel settings stay optional, and no credential is represented in logs.
    assert (
        settings.imgbb_key.get_secret_value() == ""
        and settings.catbox_userhash.get_secret_value() == ""
    )
    assert all(
        values[key] not in repr(settings)
        for key in ("BOT_TOKEN", "API_HASH", "SESSION_SECRET", "MONGO_URL")
    )
