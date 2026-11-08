"""Same-origin static assets and host-supplied API ports, without deployment."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from summon.api.app import create_app
from summon.api.run import main
from summon.config import Settings


def test_api_uses_platform_port_but_defaults_to_8000(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    with patch("summon.api.run.uvicorn.run") as run:
        main()
        assert run.call_args.kwargs["host"] == "0.0.0.0"  # noqa: S104 - deployment bind
        assert run.call_args.kwargs["port"] == 8000
        assert run.call_args.kwargs["access_log"] is False

    monkeypatch.setenv("PORT", "10000")
    with patch("summon.api.run.uvicorn.run") as run:
        main()
        assert run.call_args.kwargs["port"] == 10000


@pytest.mark.parametrize("port", ["", "SECRET_SENTINEL", "0", "65536", "-1"])
def test_invalid_platform_port_fails_without_echoing_its_value(monkeypatch, port):
    monkeypatch.setenv("PORT", port)
    with (
        patch("summon.api.run.uvicorn.run") as run,
        pytest.raises(ValueError, match="PORT") as error,
    ):
        main()
    assert "SECRET_SENTINEL" not in str(error.value)
    assert error.value.__cause__ is None
    run.assert_not_called()


def test_static_site_is_opt_in_and_keeps_api_routes_first(tmp_path):
    web = tmp_path / "site"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>SUMMON synthetic</title>")
    (web / "assets").mkdir()
    (web / "assets" / "bundle.js").write_text("console.log('synthetic');")
    (web / "art").mkdir()
    (web / "art" / "aiko.txt").write_text("sample art")
    (web / "api").mkdir()
    (web / "api" / "not-a-real-route").write_text("NOT_AN_API_RESPONSE")
    (tmp_path / "private.txt").write_text("NOT_PUBLIC_SENTINEL")
    (web / "link.txt").symlink_to(tmp_path / "private.txt")
    settings = Settings(preview_mode=True, web_dist=str(web), _env_file=None)
    with TestClient(create_app(settings=settings)) as client:
        root = client.get("/")
        assert root.status_code == 200
        assert "SUMMON synthetic" in root.text
        assert root.headers["x-content-type-options"] == "nosniff"
        assert client.get("/assets/bundle.js").status_code == 200
        assert client.get("/art/aiko.txt").text == "sample art"
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/api/bootstrap").json()["data"]["preview"] is True
        assert client.post("/api/auth/session", json={"init_data": "x"}).status_code == 403
        assert client.get("/api/not-a-real-route").status_code == 404
        assert client.get("/link.txt").status_code == 404
        assert "NOT_PUBLIC_SENTINEL" not in client.get("/%2e%2e/private.txt").text


def test_unconfigured_static_site_is_not_served():
    settings = Settings(preview_mode=True, _env_file=None)
    with TestClient(create_app(settings=settings)) as client:
        assert client.get("/").status_code == 404
        assert client.get("/api/bootstrap").status_code == 200


def test_bad_static_directory_fails_early(tmp_path):
    settings = Settings(preview_mode=True, web_dist=str(tmp_path / "missing"), _env_file=None)
    with pytest.raises(RuntimeError):
        create_app(settings=settings)
