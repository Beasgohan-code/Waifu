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
        _env_file=None,
    )
    assert settings.environment == "production"


def test_rich_client_requires_telegram_application_credentials():
    with pytest.raises(ValueError):
        Settings(rich_enabled=True, _env_file=None)


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
