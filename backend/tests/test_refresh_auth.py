from unittest.mock import patch

from fastapi.testclient import TestClient


def test_refresh_rejects_missing_server_secret_outside_local_mode(monkeypatch) -> None:
    from main import app

    monkeypatch.setenv("XBRAY_ENV", "production")
    monkeypatch.delenv("XBRAY_REFRESH_TOKEN", raising=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/refresh")
    assert response.status_code == 503
    assert response.json() == {"detail": "refresh_auth_unconfigured"}


def test_refresh_rejects_invalid_bearer_without_leaking_secret(monkeypatch) -> None:
    from main import app

    monkeypatch.setenv("XBRAY_ENV", "production")
    monkeypatch.setenv("XBRAY_REFRESH_TOKEN", "server-secret")
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/refresh",
            headers={"Authorization": "Bearer wrong-secret"},
        )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "unauthorized"}
    assert "server-secret" not in response.text


def test_refresh_accepts_valid_bearer(monkeypatch) -> None:
    from main import app

    monkeypatch.setenv("XBRAY_ENV", "production")
    monkeypatch.setenv("XBRAY_REFRESH_TOKEN", "server-secret")
    result = {"status": "ok", "tasks": []}
    with (
        patch("main.daily_refresh", return_value=result),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        response = client.post(
            "/api/refresh",
            headers={"Authorization": "Bearer server-secret"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
