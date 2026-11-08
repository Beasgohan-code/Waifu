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
