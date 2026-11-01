from __future__ import annotations

import base64
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlparse
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import admin_auth as auth
from admin_auth_config import (
    AuthConfig,
    AuthConfigurationError,
    load_auth_config,
    normalize_ingress_authority,
)
from db import admin_auth as persistence
from db import store
from main import app


def _config(*, dev: bool = True) -> AuthConfig:
    callback = "http://localhost:5173/api/auth/callback" if dev else "https://xbray.vercel.app/api/auth/callback"
    origin = "http://localhost:5173" if dev else "https://xbray.vercel.app"
    return AuthConfig(True, "https://auth.example.invalid", "synthetic-anon-key",
                      origin, callback, f"{callback}?flow_state=*", b"f" * 32,
                      "test-key-v1", b"s" * 32, 1800, 28800, dev,
                      ("localhost:5173",) if dev else ("xbray.vercel.app", "xbray-api.vercel.app"))


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    if store._conn is not None:
        store._conn.close()
    store._conn = None
    monkeypatch.setattr(store, "DATABASE_URL", "")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "auth.sqlite")
    store.init_db()
    monkeypatch.setattr(auth, "_config", _config)
    with TestClient(app, base_url=_config().app_origin) as test_client:
        yield test_client
    store._conn.close()
    store._conn = None


class FakeAuthClient:
    def __init__(self, storage, *, user_id: str | None = None, provider: str = "google"):
        self.storage = storage
        self.user_id = user_id
        self.provider = provider

    def sign_in_with_oauth(self, payload):
        assert payload["provider"] == "google"
        assert payload["options"]["scopes"] == "openid email profile"
        assert "flow_state=" in payload["options"]["redirect_to"]
        self.storage.set_item("supabase.auth.token-code-verifier", "synthetic-verifier")
        return SimpleNamespace(url="https://auth.example.invalid/authorize?provider=google&redirect_to=" +
                               quote(payload["options"]["redirect_to"], safe=""))

    def exchange_code_for_session(self, payload):
        assert payload["auth_code"] == "synthetic-code"
        assert payload["code_verifier"] == "synthetic-verifier"
        return SimpleNamespace(session=SimpleNamespace(access_token="synthetic-access"))

    def get_user(self, token):
        assert token == "synthetic-access"
        user = SimpleNamespace(id=self.user_id, app_metadata={"provider": self.provider}) if self.user_id else None
        return SimpleNamespace(user=user)

    def close(self):
        return None


def _mock_provider(monkeypatch, *, user_id: str | None = None, provider: str = "google"):
    monkeypatch.setattr(auth, "_sdk", lambda _config, storage, _http_client=None: FakeAuthClient(storage, user_id=user_id, provider=provider))


def _flow_cookie(response) -> str:
    return response.cookies[auth.DEV_FLOW_COOKIE]


def _session_for(client: TestClient, monkeypatch: pytest.MonkeyPatch | None, *, member: bool = True):
    user_id = str(uuid4())
    if member:
        persistence.grant_admin(user_id, granted_by=str(uuid4()), reason="test grant")
    raw, csrf = auth._establish_session(None, _config(), user_id)
    client.cookies.set(auth.DEV_SESSION_COOKIE, raw)
    return user_id, csrf, raw


def test_config_disabled_and_strict_redirect_config():
    assert load_auth_config({"ADMIN_AUTH_ENABLED": "false"}).enabled is False
    required = {
        "ADMIN_AUTH_ENABLED": "true", "SUPABASE_URL": "https://supabase.invalid",
        "SUPABASE_ANON_KEY": "synthetic-anon-key", "ADMIN_APP_ORIGIN": "https://app.invalid",
        "ADMIN_OAUTH_CALLBACK_URL": "https://app.invalid/api/auth/callback",
        "ADMIN_AUTH_INGRESS_HOSTS": "app.invalid,api.invalid",
        "ADMIN_ALLOWED_REDIRECTS": r"https://app.invalid/api/auth/callback\?flow_state=*",
        "ADMIN_FLOW_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode(),
        "ADMIN_FLOW_KEY_VERSION": "v1",
        "ADMIN_SESSION_HASH_KEY": base64.urlsafe_b64encode(b"h" * 32).rstrip(b"=").decode(),
    }
    assert load_auth_config(required).callback_allow_pattern.endswith(r"\?flow_state=*")
    assert load_auth_config(required).ingress_hosts == ("app.invalid", "api.invalid")
    assert load_auth_config({**required, "ADMIN_AUTH_INGRESS_HOSTS": "APP.INVALID,API.INVALID"}).ingress_hosts == ("app.invalid", "api.invalid")
    assert normalize_ingress_authority("Host", "APP.INVALID:443") == "app.invalid:443"
    for invalid in ("", "app.invalid.attacker.invalid", "app.invalid,", "app.invalid:bad", "user@app.invalid", "*.invalid", "app.invalid,app.invalid"):
        with pytest.raises(AuthConfigurationError):
            load_auth_config({**required, "ADMIN_AUTH_INGRESS_HOSTS": invalid})
    with pytest.raises(AuthConfigurationError):
        load_auth_config({**required, "ADMIN_ALLOWED_REDIRECTS": "https://*.invalid/**"})
    with pytest.raises(AuthConfigurationError):
        load_auth_config({**required, "ADMIN_OAUTH_CALLBACK_URL": "https://evil.invalid/callback"})
    for key, value in (
        ("ADMIN_FLOW_KEY_VERSION", ""),
        ("SUPABASE_ANON_KEY", "short"),
        ("ADMIN_APP_ORIGIN", "https://app.invalid/path"),
        ("ADMIN_SESSION_IDLE_SECONDS", "not-an-int"),
        ("ADMIN_SESSION_IDLE_SECONDS", "30"),
        ("ADMIN_SESSION_ABSOLUTE_SECONDS", "59"),
        ("ADMIN_FLOW_ENCRYPTION_KEY", "short"),
    ):
        with pytest.raises(AuthConfigurationError):
            load_auth_config({**required, key: value})
    with pytest.raises(AuthConfigurationError):
        load_auth_config({**required, "ADMIN_FLOW_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"k" * 33).rstrip(b"=").decode()})
    with pytest.raises(AuthConfigurationError):
        load_auth_config({**required, "SUPABASE_URL": "https://user:password@supabase.invalid"})
    with pytest.raises(AuthConfigurationError):
        load_auth_config({**required, "ADMIN_OAUTH_CALLBACK_URL": "https://app.invalid/api/auth/callback?x=y"})
    with pytest.raises(AuthConfigurationError):
        load_auth_config({**required, "ADMIN_SESSION_HASH_KEY": base64.urlsafe_b64encode(b"x" * 16).rstrip(b"=").decode()})


def test_flow_crypto_binds_flow_and_callback():
    nonce, key, flow_id, callback = b"n" * 12, b"k" * 32, str(uuid4()), "https://app.invalid/callback"
    ciphertext = auth.encrypt_verifier("verifier", key, flow_id, callback, nonce)
    assert auth.decrypt_verifier(ciphertext, key, flow_id, callback, nonce) == "verifier"
    from cryptography.exceptions import InvalidTag

    with pytest.raises(InvalidTag):
        auth.decrypt_verifier(ciphertext, key, str(uuid4()), callback, nonce)
    with pytest.raises(InvalidTag):
        auth.decrypt_verifier(ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]), key, flow_id, callback, nonce)
    with pytest.raises(ValueError):
        auth.encrypt_verifier("verifier", key, flow_id, callback, b"short")


def test_private_helpers_reject_malformed_cookie_and_bound_session():
    csrf_raw = "raw"
    config = _config()
    assert auth._cookie_parts(None) is None
    assert auth._cookie_parts("not-a-cookie") is None
    assert auth._cookie_parts("x.y.z") is None
    assert auth._cookie_parts("x" * 43 + ".bad") is None
    assert auth._cookie_parts("x" * 43 + "." + "y" * 43) is not None
    assert auth._flow_cookie("a", "b") == "a.b"
    assert auth._session_cookie(config) == auth.DEV_SESSION_COOKIE
    assert auth._flow_cookie_name(config) == auth.DEV_FLOW_COOKIE
    assert auth._csrf_hash(auth._csrf(csrf_raw, config)) == auth._digest(auth._csrf(csrf_raw, config))
    assert auth._session_dto({"user_id": "u", "idle_expires_at": "i", "absolute_expires_at": "a"}, "c")["csrf_token"] == "c"
    assert auth._parse_callback_url(config, "state").endswith("?flow_state=state")
    assert auth._callback_safe_redirect(config, "https://evil.invalid").headers["location"].endswith("reason=login_failed")
    storage = auth._FlowStorage()
    assert storage.get_item("empty") is None and storage.is_empty()
    storage.set_item("key", "value")
    assert storage.get_item("key") == "value" and not storage.is_empty()
    storage.remove_item("key")
    assert storage.is_empty()
    response = auth._no_store(auth.JSONResponse({"ok": True}))
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_login_persists_verifier_and_sets_secure_bounded_flow_cookie(client, monkeypatch):
    _mock_provider(monkeypatch)
    response = client.get("/api/auth/login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["cache-control"] == "no-store"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Max-Age=86400" in cookie
    assert "flow_state=" in parse_qs(urlparse(response.headers["location"]).query)["redirect_to"][0]


def test_login_rate_limit_is_per_browser_binding(client, monkeypatch):
    _mock_provider(monkeypatch)
    responses = [client.get("/api/auth/login", follow_redirects=False) for _ in range(6)]
    assert [response.status_code for response in responses] == [302] * 5 + [429]


def test_callback_creates_session_only_for_explicit_member_and_consumes_flow(client, monkeypatch):
    user_id = str(uuid4())
    persistence.grant_admin(user_id, granted_by=str(uuid4()), reason="test grant")
    _mock_provider(monkeypatch, user_id=user_id)
    login_response = client.get("/api/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(parse_qs(urlparse(login_response.headers["location"]).query)["redirect_to"][0]).query)["flow_state"][0]
    client.cookies.set(auth.DEV_FLOW_COOKIE, _flow_cookie(login_response))
    callback_response = client.get("/api/auth/callback", params={"flow_state": state, "code": "synthetic-code"}, follow_redirects=False)
    assert callback_response.status_code == 303
    assert callback_response.headers["location"] == "http://localhost:5173/admin"
    assert callback_response.headers["cache-control"] == "no-store"
    assert auth.DEV_SESSION_COOKIE in callback_response.headers["set-cookie"]
    replay = client.get("/api/auth/callback", params={"flow_state": state, "code": "synthetic-code"}, follow_redirects=False)
    assert replay.status_code == 303 and auth.DEV_SESSION_COOKIE not in replay.headers.get("set-cookie", "")


def test_browser_binding_duplicate_state_and_missing_code_denied(client, monkeypatch):
    _mock_provider(monkeypatch)
    login_response = client.get("/api/auth/login", follow_redirects=False)
    redirect = parse_qs(urlparse(login_response.headers["location"]).query)["redirect_to"][0]
    state = parse_qs(urlparse(redirect).query)["flow_state"][0]
    client.cookies.set(auth.DEV_FLOW_COOKIE, _flow_cookie(login_response))
    wrong_cookie = auth._flow_cookie(auth._random(), auth._random())
    client.cookies.set(auth.DEV_FLOW_COOKIE, wrong_cookie)
    wrong = client.get("/api/auth/callback", params={"flow_state": state, "code": "synthetic-code"}, follow_redirects=False)
    assert wrong.status_code == 303 and auth.DEV_SESSION_COOKIE not in wrong.headers.get("set-cookie", "")
    dup = client.get("/api/auth/callback?flow_state=a&flow_state=b&code=c", follow_redirects=False)
    assert dup.status_code == 303
    missing = client.get("/api/auth/callback?flow_state=a", follow_redirects=False)
    assert missing.status_code == 303
    injected = client.get("/api/auth/callback?flow_state=a&code=c&next=https://evil.invalid", follow_redirects=False,
                          headers={"host": "evil.invalid"})
    assert injected.status_code == 400
    wrong_host = client.get("/api/auth/callback?flow_state=a&code=c", headers={"host": "evil.invalid"}, follow_redirects=False)
    assert wrong_host.status_code == 400
    with TestClient(app, base_url=_config().app_origin) as no_cookie_client:
        no_cookie = no_cookie_client.get("/api/auth/callback?flow_state=" + "a" * 43 + "&code=c", follow_redirects=False)
    assert no_cookie.status_code == 303


def test_ungranted_user_gets_no_app_session(client, monkeypatch):
    _mock_provider(monkeypatch, user_id=str(uuid4()))
    login_response = client.get("/api/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(parse_qs(urlparse(login_response.headers["location"]).query)["redirect_to"][0]).query)["flow_state"][0]
    client.cookies.set(auth.DEV_FLOW_COOKIE, _flow_cookie(login_response))
    response = client.get("/api/auth/callback", params={"flow_state": state, "code": "synthetic-code"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("/login?reason=not_authorized")
    assert auth.DEV_SESSION_COOKIE not in response.headers.get("set-cookie", "")


def test_non_google_managed_identity_gets_no_app_session(client, monkeypatch):
    _mock_provider(monkeypatch, user_id=str(uuid4()), provider="github")
    login_response = client.get("/api/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(parse_qs(urlparse(login_response.headers["location"]).query)["redirect_to"][0]).query)["flow_state"][0]
    client.cookies.set(auth.DEV_FLOW_COOKIE, _flow_cookie(login_response))
    response = client.get("/api/auth/callback", params={"flow_state": state, "code": "synthetic-code"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].endswith("/login?reason=login_failed")
    assert auth.DEV_SESSION_COOKIE not in response.headers.get("set-cookie", "")


def test_session_csrf_reload_logout_and_origin_denial(client, monkeypatch):
    _user, csrf, raw = _session_for(client, monkeypatch)
    first = client.get("/api/auth/session")
    reloaded = client.get("/api/auth/session")
    assert first.status_code == reloaded.status_code == 200
    assert first.json()["csrf_token"] == reloaded.json()["csrf_token"] == csrf
    assert raw not in first.text and "email" not in first.json()
    bad = client.post("/api/auth/logout", headers={"origin": "https://evil.invalid", "x-csrf-token": csrf})
    assert bad.status_code == 403 and "set-cookie" not in bad.headers
    assert client.get("/api/auth/session").status_code == 200
    good = client.post("/api/auth/logout", headers={"origin": _config().app_origin, "x-csrf-token": csrf})
    assert good.status_code == 200 and "Max-Age=0" in good.headers["set-cookie"]
    assert client.get("/api/auth/session").status_code == 401


def test_activity_requires_csrf_and_is_bounded(client, monkeypatch):
    _user, _csrf_token, raw = _session_for(client, monkeypatch)
    bad = client.post("/api/auth/activity", headers={"origin": _config().app_origin, "x-csrf-token": "bad"})
    assert bad.status_code == 403 and "set-cookie" not in bad.headers
    assert client.get("/api/auth/session").status_code == 200
    now = datetime.now(timezone.utc)
    assert persistence.update_activity(auth._session_hash(raw, _config()), idle_seconds=1800, now=now + timedelta(seconds=61)) is True
    assert persistence.update_activity(auth._session_hash(raw, _config()), idle_seconds=1800, now=now + timedelta(seconds=90)) is False


@pytest.mark.parametrize("ingress_host", ["xbray.vercel.app", "xbray-api.vercel.app"])
def test_vercel_external_rewrite_callback_accepts_only_configured_transport_host(
    client, monkeypatch, ingress_host
):
    config = _config(dev=False)
    monkeypatch.setattr(auth, "_config", lambda: config)
    user_id = str(uuid4())
    persistence.grant_admin(user_id, granted_by=str(uuid4()), reason="test grant")
    _mock_provider(monkeypatch, user_id=user_id)
    response = client.get("/api/auth/login", follow_redirects=False)
    redirect_to = parse_qs(urlparse(response.headers["location"]).query)["redirect_to"][0]
    assert redirect_to.startswith("https://xbray.vercel.app/api/auth/callback?flow_state=")
    state = parse_qs(urlparse(redirect_to).query)["flow_state"][0]
    flow = response.cookies[auth.FLOW_COOKIE]
    callback = client.get(
        "/api/auth/callback", params={"flow_state": state, "code": "synthetic-code"},
        headers={"host": ingress_host, "x-forwarded-host": "attacker.invalid", "forwarded": "host=attacker.invalid"},
        cookies={auth.FLOW_COOKIE: flow}, follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "https://xbray.vercel.app/admin"
    cookie = callback.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert "path=/" in cookie and "domain=" not in cookie
    assert auth.SESSION_COOKIE in callback.headers["set-cookie"]


@pytest.mark.parametrize("host", [None, "xbray-api.vercel.app.attacker.invalid", "xbray.vercel.app:bad", "preview-xbray.vercel.app", "xbray.vercel.app,attacker.invalid"])
def test_callback_rejects_untrusted_host_without_consuming_flow(client, monkeypatch, host):
    config = _config(dev=False)
    monkeypatch.setattr(auth, "_config", lambda: config)
    user_id = str(uuid4())
    persistence.grant_admin(user_id, granted_by=str(uuid4()), reason="test grant")
    _mock_provider(monkeypatch, user_id=user_id)
    login = client.get("/api/auth/login", follow_redirects=False)
    redirect_to = parse_qs(urlparse(login.headers["location"]).query)["redirect_to"][0]
    state = parse_qs(urlparse(redirect_to).query)["flow_state"][0]
    before = persistence.consume_flow
    calls = []
    monkeypatch.setattr(persistence, "consume_flow", lambda **kwargs: (calls.append(kwargs), before(**kwargs))[1])
    headers = {} if host is None else {"host": host}
    callback = client.get(
        "/api/auth/callback", params={"flow_state": state, "code": "synthetic-code"},
        headers=headers, cookies={auth.FLOW_COOKIE: login.cookies[auth.FLOW_COOKIE]}, follow_redirects=False,
    )
    assert callback.status_code == 400
    assert not calls


def test_callback_host_validation_rejects_duplicate_asgi_host_headers():
    from starlette.requests import Request as StarletteRequest

    scope = {
        "type": "http", "headers": [(b"host", b"xbray.vercel.app"), (b"host", b"xbray-api.vercel.app")],
        "method": "GET", "path": "/api/auth/callback", "query_string": b"",
        "server": ("xbray.vercel.app", 443), "client": ("test", 1), "scheme": "https",
        "root_path": "", "app": app,
    }
    assert auth._canonical_request(StarletteRequest(scope), _config(dev=False)) is False


def test_successful_login_rotation_revokes_previous_session(client, monkeypatch):
    user_id = str(uuid4())
    persistence.grant_admin(user_id, granted_by=str(uuid4()), reason="test grant")
    old_raw, _old_csrf = auth._establish_session(None, _config(), user_id)
    client.cookies.set(auth.DEV_SESSION_COOKIE, old_raw)
    _mock_provider(monkeypatch, user_id=user_id)
    login_response = client.get("/api/auth/login", follow_redirects=False)
    state = parse_qs(urlparse(parse_qs(urlparse(login_response.headers["location"]).query)["redirect_to"][0]).query)["flow_state"][0]
    client.cookies.set(auth.DEV_FLOW_COOKIE, _flow_cookie(login_response))
    response = client.get("/api/auth/callback", params={"flow_state": state, "code": "synthetic-code"}, follow_redirects=False)
    assert response.status_code == 303
    assert persistence.get_session(auth._session_hash(old_raw, _config())) is None
    assert client.get("/api/auth/session").status_code == 200


def test_config_missing_returns_503_without_changing_public_route(client, monkeypatch):
    monkeypatch.setattr(auth, "_config", lambda: (_ for _ in ()).throw(auth.AuthUnavailable()))
    assert client.get("/api/auth/login").status_code == 503
    assert client.get("/api/auth/session").status_code == 503
    assert client.get("/api/status").status_code == 200


def test_auth_config_wrapper_fails_closed(monkeypatch):
    monkeypatch.setattr(auth, "load_auth_config", lambda: (_ for _ in ()).throw(AuthConfigurationError("synthetic")))
    with pytest.raises(auth.AuthUnavailable):
        auth._config()
    monkeypatch.setattr(auth, "load_auth_config", lambda: _config())
    assert auth._config().enabled
    monkeypatch.setattr(auth, "load_auth_config", lambda: AuthConfig(False, "", "", "", "", "", b"", "", b"", 1800, 28800, False))
    with pytest.raises(auth.AuthUnavailable):
        auth._config()


def test_session_missing_or_invalid_cookie_is_cleared(client):
    absent = client.get("/api/auth/session")
    assert absent.status_code == 401 and "Max-Age=0" in absent.headers["set-cookie"]
    client.cookies.set(auth.DEV_SESSION_COOKIE, "invalid")
    malformed = client.get("/api/auth/session")
    assert malformed.status_code == 401 and "Max-Age=0" in malformed.headers["set-cookie"]


def test_session_csrf_hash_mismatch_denies_and_clears(client, monkeypatch):
    _user, _csrf, _raw = _session_for(client, monkeypatch)
    original = persistence.get_session
    monkeypatch.setattr(persistence, "get_session", lambda token: {**original(token), "csrf_token_hash": "wrong"})
    response = client.get("/api/auth/session")
    assert response.status_code == 401 and "Max-Age=0" in response.headers["set-cookie"]


def test_membership_revocation_denies_and_invalidates_existing_session(client, monkeypatch):
    user_id, _csrf_token, raw = _session_for(client, monkeypatch)
    assert persistence.revoke_admin(user_id, granted_by=str(uuid4()), reason="operator-revoked")
    response = client.get("/api/auth/session")
    assert response.status_code == 401 and "Max-Age=0" in response.headers["set-cookie"]
    assert persistence.get_session(auth._session_hash(raw, _config())) is None


def test_activity_success_and_stale_activity_clears_cookie(client, monkeypatch):
    _user, csrf, _raw = _session_for(client, monkeypatch)
    response = client.post("/api/auth/activity", headers={"origin": _config().app_origin, "x-csrf-token": csrf})
    assert response.status_code == 200 and response.json() == {"ok": True}
    monkeypatch.setattr(persistence, "update_activity", lambda *_args, **_kwargs: False)
    cadence_limited = client.post("/api/auth/activity", headers={"origin": _config().app_origin, "x-csrf-token": csrf})
    assert cadence_limited.status_code == 200
    monkeypatch.setattr(persistence, "get_session", lambda *_args, **_kwargs: None)
    stale = client.post("/api/auth/activity", headers={"origin": _config().app_origin, "x-csrf-token": csrf})
    assert stale.status_code == 401 and "Max-Age=0" in stale.headers["set-cookie"]


def test_sqlite_activity_uses_database_clock(client, monkeypatch):
    _user, csrf, raw = _session_for(client, monkeypatch)
    token_hash = auth._session_hash(raw, _config())
    old = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat(timespec="microseconds")
    store._get_conn().execute(
        "UPDATE admin_sessions SET last_activity_at = ? WHERE session_token_hash = ?",
        (old, token_hash),
    )
    store._get_conn().commit()
    response = client.post("/api/auth/activity", headers={"origin": _config().app_origin, "x-csrf-token": csrf})
    assert response.status_code == 200
    record = persistence.get_session(token_hash)
    assert record is not None and record["last_activity_at"] > old


def test_sdk_failures_are_generic_and_flows_are_consumed(client, monkeypatch):
    monkeypatch.setattr(auth, "_sdk", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic failure")))
    assert client.get("/api/auth/login").status_code == 503

    _mock_provider(monkeypatch, user_id=str(uuid4()))
    login_response = client.get("/api/auth/login", follow_redirects=False)
    redirect = parse_qs(urlparse(login_response.headers["location"]).query)["redirect_to"][0]
    state = parse_qs(urlparse(redirect).query)["flow_state"][0]
    client.cookies.set(auth.DEV_FLOW_COOKIE, _flow_cookie(login_response))
    monkeypatch.setattr(persistence, "is_admin", lambda _user_id: (_ for _ in ()).throw(RuntimeError("db detail")))
    callback = client.get("/api/auth/callback", params={"flow_state": state, "code": "synthetic-code"}, follow_redirects=False)
    assert callback.status_code == 303 and "db detail" not in callback.text


def test_instantiated_sdk_is_pkce_configured_without_network():
    storage = auth._FlowStorage()
    sdk = auth._sdk(_config(), storage)
    assert sdk._flow_type == "pkce"
    assert sdk._auto_refresh_token is False
    assert sdk._persist_session is False
    sdk.close()


def test_callback_rejects_upstream_error_unknown_query_and_malformed_codes(client):
    for query in (
        "flow_state=" + "a" * 43 + "&code=x&next=y",
        "flow_state=" + "a" * 43 + "&error=access_denied",
        "flow_state=" + "a" * 43 + "&code=" + "x" * 4097,
        "flow_state=bad&code=x",
    ):
        response = client.get("/api/auth/callback?" + query, follow_redirects=False)
        assert response.status_code == 303
        assert "next=y" not in response.headers["location"]


def test_auth_dependency_requires_membership(client, monkeypatch):
    _user, _csrf, _raw = _session_for(client, monkeypatch)
    request = client.get("/api/auth/session")
    assert request.status_code == 200
    from starlette.requests import Request as StarletteRequest

    scope = {"type": "http", "headers": [(b"cookie", f"{auth.DEV_SESSION_COOKIE}={client.cookies.get(auth.DEV_SESSION_COOKIE)}".encode())], "method": "GET", "path": "/", "query_string": b"", "server": ("test", 80), "client": ("test", 1), "scheme": "http", "root_path": "", "app": app}
    auth_request = StarletteRequest(scope)
    authorized = auth.require_admin(auth_request)
    assert authorized["permissions"] == ("monitoring:read",)
    scope["headers"] = []
    with pytest.raises(auth.AuthDenied):
        auth.require_admin(StarletteRequest(scope))


def test_auth_dependency_rejects_nonmember_and_unavailable_config(client, monkeypatch):
    _user_id, _csrf, raw = _session_for(client, monkeypatch, member=False)
    from starlette.requests import Request as StarletteRequest

    cookie = f"{auth.DEV_SESSION_COOKIE}={raw}".encode()
    scope = {"type": "http", "headers": [(b"cookie", cookie)], "method": "GET", "path": "/", "query_string": b"", "server": ("test", 80), "client": ("test", 1), "scheme": "http", "root_path": "", "app": app}
    with pytest.raises(auth.AuthDenied) as denied:
        auth.require_admin(StarletteRequest(scope))
    assert denied.value.status_code == 403
    monkeypatch.setattr(auth, "_config", lambda: (_ for _ in ()).throw(auth.AuthUnavailable()))
    with pytest.raises(auth.AuthDenied) as unavailable:
        auth.require_admin(StarletteRequest({**scope, "headers": []}))
    assert unavailable.value.status_code == 503


def test_callback_direct_wrong_path_and_database_failure(client, monkeypatch):
    from starlette.requests import Request as StarletteRequest

    wrong_scope = {"type": "http", "headers": [], "method": "GET", "path": "/wrong", "query_string": b"", "server": ("test", 80), "client": ("test", 1), "scheme": "http", "root_path": "", "app": app}
    assert auth.callback(StarletteRequest(wrong_scope)).status_code == 400

    original_admin_check = persistence.is_admin
    monkeypatch.setattr(persistence, "is_admin", lambda *_args: (_ for _ in ()).throw(sqlite3.OperationalError("db")))
    _mock_provider(monkeypatch, user_id=str(uuid4()))
    login_response = client.get("/api/auth/login", follow_redirects=False)
    redirect = parse_qs(urlparse(login_response.headers["location"]).query)["redirect_to"][0]
    state = parse_qs(urlparse(redirect).query)["flow_state"][0]
    client.cookies.set(auth.DEV_FLOW_COOKIE, _flow_cookie(login_response))
    response = client.get("/api/auth/callback", params={"flow_state": state, "code": "synthetic-code"}, follow_redirects=False)
    assert response.status_code == 503
    monkeypatch.setattr(persistence, "is_admin", original_admin_check)


def test_active_session_without_membership_is_cleared_by_session_endpoint(client):
    _user, _csrf, _raw = _session_for(client, None, member=False)
    response = client.get("/api/auth/session")
    assert response.status_code == 403 and "Max-Age=0" in response.headers["set-cookie"]


def test_config_and_database_failures_return_503(client, monkeypatch):
    original = persistence.get_session
    monkeypatch.setattr(persistence, "get_session", lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("db")))
    raw = auth._random()
    client.cookies.set(auth.DEV_SESSION_COOKIE, raw)
    assert client.get("/api/auth/session").status_code == 503
    monkeypatch.setattr(persistence, "get_session", original)

    monkeypatch.setattr(auth, "_config", lambda: (_ for _ in ()).throw(auth.AuthUnavailable()))
    assert client.get("/api/auth/callback?flow_state=x&code=y").status_code == 503


def test_login_missing_sdk_verifier_and_callback_upstream_shape_fail_closed(client, monkeypatch):
    class MissingVerifier(FakeAuthClient):
        def sign_in_with_oauth(self, _payload):
            return SimpleNamespace(url="https://auth.invalid")

    monkeypatch.setattr(auth, "_sdk", lambda _config, storage, _http_client=None: MissingVerifier(storage))
    assert client.get("/api/auth/login").status_code == 503


@pytest.mark.parametrize("shape", ["no-session", "no-user", "no-id", "exchange-error"])
def test_callback_bad_sdk_results_have_fixed_redirect(client, monkeypatch, shape):
    class BadResult(FakeAuthClient):
        def exchange_code_for_session(self, _payload):
            if shape == "exchange-error":
                raise RuntimeError("secret provider detail")
            if shape == "no-session":
                return SimpleNamespace(session=None)
            return SimpleNamespace(session=SimpleNamespace(access_token="synthetic-access"))

        def get_user(self, _token):
            if shape == "no-user":
                return None
            if shape == "no-id":
                return SimpleNamespace(user=SimpleNamespace(id="", app_metadata={"provider": "google"}))
            return SimpleNamespace(user=SimpleNamespace(id=str(uuid4()), app_metadata={"provider": "google"}))

    monkeypatch.setattr(auth, "_sdk", lambda _config, storage, _http_client=None: BadResult(storage))
    login_response = client.get("/api/auth/login", follow_redirects=False)
    redirect = parse_qs(urlparse(login_response.headers["location"]).query)["redirect_to"][0]
    state = parse_qs(urlparse(redirect).query)["flow_state"][0]
    client.cookies.set(auth.DEV_FLOW_COOKIE, _flow_cookie(login_response))
    response = client.get("/api/auth/callback", params={"flow_state": state, "code": "synthetic-code"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].endswith("reason=login_failed")
    assert "secret provider detail" not in response.text


def test_logout_and_activity_persistence_errors_do_not_claim_success(client, monkeypatch):
    _user, csrf, _raw = _session_for(client, monkeypatch)
    monkeypatch.setattr(persistence, "revoke_session", lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("db")))
    logout_response = client.post("/api/auth/logout", headers={"origin": _config().app_origin, "x-csrf-token": csrf})
    assert logout_response.status_code == 503 and "set-cookie" not in logout_response.headers

    monkeypatch.setattr(persistence, "revoke_session", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(persistence, "update_activity", lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("db")))
    activity_response = client.post("/api/auth/activity", headers={"origin": _config().app_origin, "x-csrf-token": csrf})
    assert activity_response.status_code == 503 and "set-cookie" not in activity_response.headers


def test_cleanup_is_batched_and_rejects_unbounded_batch(client):
    with pytest.raises(ValueError):
        persistence.cleanup(batch_size=1001)
    assert persistence.cleanup(batch_size=1) == {
        "admin_oauth_flows": 0, "admin_sessions": 0, "admin_auth_events": 0,
    }
