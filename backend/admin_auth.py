"""Protocol boundary for the private human-admin authentication routes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import uuid4

import httpx
import psycopg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from supabase_auth import SyncGoTrueClient  # pyright: ignore[reportPrivateImportUsage]

from admin_auth_config import (
    AuthConfig,
    AuthConfigurationError,
    load_auth_config,
    normalize_ingress_authority,
)
from db import admin_auth as persistence

router = APIRouter(prefix="/api/auth", tags=["auth"])
SESSION_COOKIE = "__Host-xbray_admin"
DEV_SESSION_COOKIE = "xbray_admin_dev"
FLOW_COOKIE = "__Host-xbray_oauth_flow"
DEV_FLOW_COOKIE = "xbray_oauth_flow_dev"
FLOW_TTL = timedelta(minutes=10)
_OPAQUE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_CALLBACK_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


class AuthUnavailable(RuntimeError):
    pass


def _config() -> AuthConfig:
    try:
        config = load_auth_config()
    except AuthConfigurationError as exc:
        raise AuthUnavailable from exc
    if not config.enabled:
        raise AuthUnavailable
    return config


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _random() -> str:
    return _b64(secrets.token_bytes(32))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _session_hash(raw: str, config: AuthConfig) -> str:
    return hmac.new(config.session_hash_key, b"xbray:session:" + raw.encode(), hashlib.sha256).hexdigest()


def _csrf(raw: str, config: AuthConfig) -> str:
    return _b64(hmac.new(config.session_hash_key, b"xbray:csrf:" + raw.encode(), hashlib.sha256).digest())


def _csrf_hash(token: str) -> str:
    return _digest(token)


def _flow_cookie(handle: str, binding: str) -> str:
    return f"{handle}.{binding}"


def _cookie_parts(value: str | None) -> tuple[str, str] | None:
    if value is None or value.count(".") != 1:
        return None
    handle, binding = value.split(".", 1)
    if not _OPAQUE.fullmatch(handle) or not _OPAQUE.fullmatch(binding):
        return None
    return handle, binding


def _flow_aad(flow_id: str, callback: str) -> bytes:
    return f"xbray-oauth-flow-v1\0{flow_id}\0{callback}".encode()


def encrypt_verifier(verifier: str, key: bytes, flow_id: str, callback: str, nonce: bytes) -> bytes:
    if len(nonce) != 12:
        raise ValueError("AES-GCM nonce must be 96 bits")
    return AESGCM(key).encrypt(nonce, verifier.encode(), _flow_aad(flow_id, callback))


def decrypt_verifier(ciphertext: bytes, key: bytes, flow_id: str, callback: str, nonce: bytes) -> str:
    return AESGCM(key).decrypt(nonce, ciphertext, _flow_aad(flow_id, callback)).decode()


class _FlowStorage:
    """SDK verifier adapter scoped to one flow; never shared across requests."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_item(self, key: str) -> str | None:
        return self.values.get(key)

    def set_item(self, key: str, value: str) -> None:
        self.values[key] = value

    def remove_item(self, key: str) -> None:
        self.values.pop(key, None)

    def is_empty(self) -> bool:
        return not self.values


def _sdk(config: AuthConfig, storage: _FlowStorage, http_client: httpx.Client | None = None) -> SyncGoTrueClient:
    return SyncGoTrueClient(
        url=f"{config.supabase_url}/auth/v1",
        headers={"apikey": config.supabase_anon_key},
        storage=cast(Any, storage),
        flow_type="pkce",
        auto_refresh_token=False,
        persist_session=False,
        http_client=http_client or httpx.Client(timeout=httpx.Timeout(8.0, connect=3.0)),
    )


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _auth_error(status: int, code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers=_CALLBACK_HEADERS)


def _session_cookie(config: AuthConfig) -> str:
    return DEV_SESSION_COOKIE if config.dev_http_cookie else SESSION_COOKIE


def _flow_cookie_name(config: AuthConfig) -> str:
    return DEV_FLOW_COOKIE if config.dev_http_cookie else FLOW_COOKIE


def _set_cookie(response: Response, name: str, value: str, *, max_age: int, secure: bool) -> None:
    response.set_cookie(name, value, max_age=max_age, httponly=True, secure=secure,
                        samesite="lax", path="/")


def _clear_cookie(response: Response, name: str, *, secure: bool) -> None:
    response.delete_cookie(name, path="/", secure=secure, httponly=True, samesite="lax")


def _canonical_request(request: Request, config: AuthConfig) -> bool:
    # OAuth redirects are cross-site GETs; browsers may omit Origin. The route is
    # fixed and flow state, browser binding and PKCE authenticate the callback.
    callback = urlsplit(config.callback_url)
    hosts = request.headers.getlist("host")
    if request.url.path != callback.path or len(hosts) != 1:
        return False
    try:
        authority = normalize_ingress_authority("Host", hosts[0])
    except AuthConfigurationError:
        return False
    return authority in config.ingress_hosts


def _parse_callback_url(config: AuthConfig, state: str) -> str:
    parsed = urlsplit(config.callback_url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode({"flow_state": state}), ""))


def _callback_parameters(request: Request) -> tuple[str, str] | None:
    query = request.query_params
    allowed_keys = {"flow_state", "code", "error", "error_description", "error_code"}
    if any(key not in allowed_keys for key in query):
        return None
    if any(query.getlist(key) for key in ("error", "error_description", "error_code")):
        return None
    states = query.getlist("flow_state")
    codes = query.getlist("code")
    if len(states) != 1 or not _OPAQUE.fullmatch(states[0]):
        return None
    if len(codes) != 1 or not codes[0] or len(codes[0]) > 4096:
        return None
    return states[0], codes[0]


def _consume_callback_verifier(
    config: AuthConfig, state: str, handle: str, binding: str
) -> tuple[str, str] | None:
    flow = persistence.consume_flow(
        state_hash=_digest(state), handle_hash=_digest(handle), binding_hash=_digest(binding)
    )
    if flow is None:
        return None
    callback_url = str(flow["callback_url"])
    if callback_url != _parse_callback_url(config, state):
        return None
    if str(flow["key_version"]) != config.flow_key_version:
        return None
    verifier = decrypt_verifier(
        bytes(flow["verifier_ciphertext"]), config.flow_key, str(flow["id"]),
        callback_url, bytes(flow["nonce"]),
    )
    return verifier, callback_url


def _exchange_google_identity(
    client: SyncGoTrueClient, code: str, verifier: str, callback_url: str
) -> str | None:
    exchanged = client.exchange_code_for_session({
        "auth_code": code, "code_verifier": verifier, "redirect_to": callback_url,
    })
    session_value = exchanged.session
    if session_value is None or not session_value.access_token:
        return None
    user_response = client.get_user(session_value.access_token)
    user = user_response.user if user_response is not None else None
    if user is None or not user.id:
        return None
    app_metadata = cast(dict[str, Any], getattr(user, "app_metadata", None) or {})
    if app_metadata.get("provider") != "google":
        persistence.record_event(str(user.id), "login_denied", "provider_not_allowed")
        return None
    return str(user.id)


def _callback_safe_redirect(config: AuthConfig, reason: str) -> RedirectResponse:
    # Only fixed enum reasons are allowed; OAuth code/state/errors are never reflected.
    safe_reason = reason if reason in {"access_denied", "login_failed", "not_authorized"} else "login_failed"
    target = f"{config.app_origin}/login?reason={safe_reason}"
    response = RedirectResponse(target, status_code=303, headers=_CALLBACK_HEADERS)
    _clear_cookie(response, _flow_cookie_name(config), secure=not config.dev_http_cookie)
    return response


def _session_material(request: Request, config: AuthConfig) -> tuple[str, str, dict[str, Any]] | None:
    raw = request.cookies.get(_session_cookie(config))
    if not raw or not _OPAQUE.fullmatch(raw):
        return None
    record = persistence.get_session(_session_hash(raw, config))
    if record is None:
        return None
    csrf_token = _csrf(raw, config)
    if not hmac.compare_digest(_csrf_hash(csrf_token), str(record["csrf_token_hash"])):
        return None
    return raw, csrf_token, record


def _session_dto(record: dict[str, Any], csrf: str) -> dict[str, Any]:
    return {
        "user_id": str(record["user_id"]),
        "permissions": ["monitoring:read"],
        "idle_expires_at": str(record["idle_expires_at"]),
        "absolute_expires_at": str(record["absolute_expires_at"]),
        "csrf_token": csrf,
    }


def _require_admin(request: Request) -> tuple[AuthConfig, str, str, dict[str, Any]]:
    config = _config()
    try:
        material = _session_material(request, config)
    except (psycopg.Error, sqlite3.Error) as exc:
        raise AuthUnavailable from exc
    if material is None:
        raise AuthDenied(401, "session_invalid", clear_cookie=True, config=config)
    raw, csrf, record = material
    if record["active_member"] is None:
        try:
            persistence.revoke_session(_session_hash(raw, config))
        except (psycopg.Error, sqlite3.Error) as exc:
            raise AuthUnavailable from exc
        raise AuthDenied(403, "not_authorized", clear_cookie=True, config=config)
    return config, raw, csrf, record


class AuthDenied(Exception):
    def __init__(self, status_code: int, code: str, *, clear_cookie: bool = False,
                 config: AuthConfig | None = None) -> None:
        self.status_code = status_code
        self.code = code
        self.clear_cookie = clear_cookie
        self.config = config


def _expected_origin(request: Request, config: AuthConfig) -> bool:
    return request.headers.get("origin") == config.app_origin


def _valid_csrf(request: Request, csrf: str, supplied: str | None) -> bool:
    return supplied is not None and hmac.compare_digest(csrf, supplied)


@router.get("/login")
def login(request: Request) -> Response:
    try:
        config = _config()
    except AuthUnavailable:
        return _auth_error(503, "auth_unavailable")
    prior_flow = _cookie_parts(request.cookies.get(_flow_cookie_name(config)))
    binding = prior_flow[1] if prior_flow is not None else _random()
    flow_id, handle, state = str(uuid4()), _random(), _random()
    storage = _FlowStorage()
    callback_url = _parse_callback_url(config, state)
    try:
        client = _sdk(config, storage)
        try:
            oauth = client.sign_in_with_oauth({
                "provider": "google",
                "options": {"redirect_to": callback_url, "scopes": "openid email profile"},
            })
        finally:
            client.close()
        verifier = storage.get_item("supabase.auth.token-code-verifier")
        if not verifier:
            return _auth_error(503, "auth_unavailable")
        nonce = secrets.token_bytes(12)
        ciphertext = encrypt_verifier(verifier, config.flow_key, flow_id, callback_url, nonce)
        now = datetime.now(timezone.utc)
        created = persistence.create_flow(
            flow_id=flow_id, handle_hash=_digest(handle), state_hash=_digest(state),
            binding_hash=_digest(binding), ciphertext=ciphertext, nonce=nonce,
            key_version=config.flow_key_version, callback_url=callback_url,
            expires_at=now + FLOW_TTL, now=now,
        )
        if not created:
            return _auth_error(429, "login_rate_limited")
    except Exception:  # noqa: BLE001 - auth setup must fail closed for unexpected SDK/DB errors
        return _auth_error(503, "auth_unavailable")
    response = RedirectResponse(oauth.url, status_code=302, headers=_CALLBACK_HEADERS)
    _set_cookie(response, _flow_cookie_name(config), _flow_cookie(handle, binding),
                max_age=86400, secure=not config.dev_http_cookie)
    return response


@router.get("/callback")
def callback(request: Request) -> Response:
    try:
        config = _config()
    except AuthUnavailable:
        return _auth_error(503, "auth_unavailable")
    if not _canonical_request(request, config):
        return _auth_error(400, "invalid_callback")
    parameters = _callback_parameters(request)
    if parameters is None:
        return _callback_safe_redirect(config, "login_failed")
    state, code = parameters
    cookie = _cookie_parts(request.cookies.get(_flow_cookie_name(config)))
    if cookie is None:
        return _callback_safe_redirect(config, "login_failed")
    handle, binding = cookie
    try:
        flow_material = _consume_callback_verifier(config, state, handle, binding)
        if flow_material is None:
            return _callback_safe_redirect(config, "login_failed")
        verifier, callback_url = flow_material
        client = _sdk(config, _FlowStorage())
        try:
            user_id = _exchange_google_identity(client, code, verifier, callback_url)
        finally:
            client.close()
        if user_id is None:
            return _callback_safe_redirect(config, "login_failed")
        if not persistence.is_admin(user_id):
            persistence.record_event(user_id, "login_denied", "membership_required")
            return _callback_safe_redirect(config, "not_authorized")
        raw, _csrf_token = _establish_session(request, config, user_id)
        return _login_success(config, raw)
    except (psycopg.Error, sqlite3.Error):
        return _auth_error(503, "auth_unavailable")
    except Exception:  # noqa: BLE001 - callback errors never expose upstream details
        return _callback_safe_redirect(config, "login_failed")


def _establish_session(request: Request | None, config: AuthConfig, user_id: str) -> tuple[str, str]:
    raw, session_id = _random(), str(uuid4())
    csrf = _csrf(raw, config)
    now = datetime.now(timezone.utc)
    absolute = now + timedelta(seconds=config.absolute_seconds)
    previous = request.cookies.get(_session_cookie(config)) if request is not None else None
    previous_hash = _session_hash(previous, config) if previous and _OPAQUE.fullmatch(previous) else None
    persistence.establish_session(
        session_id=session_id, token_hash=_session_hash(raw, config), user_id=user_id,
        csrf_hash=_csrf_hash(csrf), idle_expires_at=min(now + timedelta(seconds=config.idle_seconds), absolute),
        absolute_expires_at=absolute, previous_token_hash=previous_hash, now=now,
    )
    return raw, csrf


def _login_success(config: AuthConfig, raw: str) -> Response:
    response = RedirectResponse(f"{config.app_origin}/admin", status_code=303, headers=_CALLBACK_HEADERS)
    _set_cookie(response, _session_cookie(config), raw,
                max_age=config.absolute_seconds, secure=not config.dev_http_cookie)
    return response


@router.get("/session")
def session(request: Request) -> Response:
    try:
        config = _config()
        material = _session_material(request, config)
    except AuthUnavailable:
        return _auth_error(503, "auth_unavailable")
    except (psycopg.Error, sqlite3.Error):
        return _auth_error(503, "auth_unavailable")
    if material is None:
        response = _auth_error(401, "session_invalid")
        _clear_cookie(response, _session_cookie(config), secure=not config.dev_http_cookie)
        return response
    raw, csrf, record = material
    if record["active_member"] is None:
        try:
            persistence.revoke_session(_session_hash(raw, config))
        except (psycopg.Error, sqlite3.Error):
            return _auth_error(503, "auth_unavailable")
        response = _auth_error(403, "not_authorized")
        _clear_cookie(response, _session_cookie(config), secure=not config.dev_http_cookie)
        return response
    return _no_store(JSONResponse(_session_dto(record, csrf)))


@router.post("/logout")
def logout(request: Request, x_csrf_token: str | None = Header(default=None)) -> Response:
    try:
        config, raw, csrf, _record = _require_admin(request)
    except AuthUnavailable:
        return _auth_error(503, "auth_unavailable")
    except AuthDenied as exc:
        response = _auth_error(exc.status_code, exc.code)
        if exc.clear_cookie and exc.config is not None:
            _clear_cookie(response, _session_cookie(exc.config), secure=not exc.config.dev_http_cookie)
        return response
    except (psycopg.Error, sqlite3.Error):
        return _auth_error(503, "auth_unavailable")
    if not _expected_origin(request, config) or not _valid_csrf(request, csrf, x_csrf_token):
        return _auth_error(403, "request_forbidden")
    try:
        persistence.revoke_session(_session_hash(raw, config))
        persistence.record_event(str(_record["user_id"]), "logout", "user_request")
    except (psycopg.Error, sqlite3.Error):
        return _auth_error(503, "auth_unavailable")
    response = JSONResponse({"ok": True}, headers=_CALLBACK_HEADERS)
    _clear_cookie(response, _session_cookie(config), secure=not config.dev_http_cookie)
    return response


@router.post("/activity")
def activity(request: Request, x_csrf_token: str | None = Header(default=None)) -> Response:
    try:
        config, raw, csrf, _record = _require_admin(request)
    except AuthUnavailable:
        return _auth_error(503, "auth_unavailable")
    except AuthDenied as exc:
        response = _auth_error(exc.status_code, exc.code)
        if exc.clear_cookie and exc.config is not None:
            _clear_cookie(response, _session_cookie(exc.config), secure=not exc.config.dev_http_cookie)
        return response
    except (psycopg.Error, sqlite3.Error):
        return _auth_error(503, "auth_unavailable")
    if not _expected_origin(request, config) or not _valid_csrf(request, csrf, x_csrf_token):
        return _auth_error(403, "request_forbidden")
    try:
        updated = persistence.update_activity(_session_hash(raw, config), idle_seconds=config.idle_seconds)
    except (psycopg.Error, sqlite3.Error):
        return _auth_error(503, "auth_unavailable")
    if not updated:
        # A valid signal arriving inside the server-enforced cadence window is
        # advisory and harmless; distinguish it from concurrent expiry/revoke.
        try:
            still_valid = persistence.get_session(_session_hash(raw, config)) is not None
        except (psycopg.Error, sqlite3.Error):
            return _auth_error(503, "auth_unavailable")
        if not still_valid:
            response = _auth_error(401, "session_invalid")
            _clear_cookie(response, _session_cookie(config), secure=not config.dev_http_cookie)
            return response
    return JSONResponse({"ok": True}, headers=_CALLBACK_HEADERS)


def require_admin(request: Request) -> dict[str, Any]:
    """Dependency intended for future private, read-only admin endpoints."""
    try:
        _config_value, _raw, _csrf_token, record = _require_admin(request)
    except AuthUnavailable as exc:
        raise AuthDenied(503, "auth_unavailable") from exc
    return {"user_id": str(record["user_id"]), "permissions": ("monitoring:read",)}
