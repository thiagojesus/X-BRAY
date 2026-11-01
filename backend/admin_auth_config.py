"""Fail-closed configuration for human admin authentication."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


class AuthConfigurationError(ValueError):
    """Required auth configuration is absent or unsafe."""


_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43,128}$")


@dataclass(frozen=True)
class AuthConfig:
    enabled: bool
    supabase_url: str
    supabase_anon_key: str
    app_origin: str
    callback_url: str
    callback_allow_pattern: str
    flow_key: bytes
    flow_key_version: str
    session_hash_key: bytes
    idle_seconds: int
    absolute_seconds: int
    dev_http_cookie: bool
    ingress_hosts: tuple[str, ...] = ()


def normalize_ingress_authority(name: str, value: str) -> str:
    if not value or value.strip() != value or any(char in value for char in "/\\?#@,%\r\n\t "):
        raise AuthConfigurationError(f"{name} contains an invalid authority")
    try:
        parsed = urlsplit(f"//{value}")
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise AuthConfigurationError(f"{name} contains an invalid authority") from exc
    if not host or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise AuthConfigurationError(f"{name} contains an invalid authority")
    if ":" in host and not value.startswith("["):
        raise AuthConfigurationError(f"{name} contains an invalid IPv6 authority")
    canonical_host = host.lower()
    if canonical_host not in {"localhost", "127.0.0.1", "::1"} and not re.fullmatch(
        r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
        canonical_host,
    ):
        raise AuthConfigurationError(f"{name} contains an invalid hostname")
    return f"{f'[{canonical_host}]' if ':' in canonical_host else canonical_host}{f':{port}' if port is not None else ''}"


def _read_bound_int(name: str, value: str | None, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    try:
        result = int(value)
    except ValueError as exc:
        raise AuthConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= result <= maximum:
        raise AuthConfigurationError(f"{name} is outside the allowed range")
    return result


def _secret_bytes(name: str, value: str | None, *, exact_length: int | None = None) -> bytes:
    if value is None or not _KEY_PATTERN.fullmatch(value):
        raise AuthConfigurationError(f"{name} must be a base64url secret of at least 256 bits")
    import base64

    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except ValueError as exc:
        raise AuthConfigurationError(f"{name} is invalid") from exc
    if len(decoded) < 32:
        raise AuthConfigurationError(f"{name} must be at least 256 bits")
    if exact_length is not None and len(decoded) != exact_length:
        raise AuthConfigurationError(f"{name} must decode to exactly {exact_length} bytes")
    return decoded


def _valid_http_url(name: str, value: str | None, *, path: str | None = None) -> str:
    if value is None:
        raise AuthConfigurationError(f"{name} is required")
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise AuthConfigurationError(f"{name} must be an absolute HTTP(S) URL")
    if parsed.fragment or parsed.query or (path is not None and parsed.path != path):
        raise AuthConfigurationError(f"{name} has an invalid path or query")
    return value.rstrip("/")


def load_auth_config(environ: dict[str, str] | None = None) -> AuthConfig:
    values = os.environ if environ is None else environ
    enabled = values.get("ADMIN_AUTH_ENABLED", "false").lower() == "true"
    dev_cookie = values.get("ADMIN_AUTH_DEV_HTTP_COOKIE", "false").lower() == "true"
    if not enabled:
        return AuthConfig(False, "", "", "", "", "", b"", "", b"", 1800, 28800, False)

    supabase = _valid_http_url("SUPABASE_URL", values.get("SUPABASE_URL"), path="")
    app_origin = _valid_http_url("ADMIN_APP_ORIGIN", values.get("ADMIN_APP_ORIGIN"))
    if urlsplit(app_origin).path not in {"", "/"}:
        raise AuthConfigurationError("ADMIN_APP_ORIGIN must not contain a path")
    callback = _valid_http_url(
        "ADMIN_OAUTH_CALLBACK_URL", values.get("ADMIN_OAUTH_CALLBACK_URL"), path="/api/auth/callback"
    )
    app_parts = urlsplit(app_origin)
    callback_parts = urlsplit(callback)
    is_loopback = app_parts.hostname in {"localhost", "127.0.0.1", "::1"}
    if (app_parts.scheme, app_parts.netloc) != (callback_parts.scheme, callback_parts.netloc):
        raise AuthConfigurationError("callback must use the configured application origin")
    if app_parts.scheme != "https" and not (dev_cookie and is_loopback and app_parts.scheme == "http"):
        raise AuthConfigurationError("HTTP auth is allowed only for explicitly enabled loopback development")
    raw_ingress = values.get("ADMIN_AUTH_INGRESS_HOSTS", "")
    if not raw_ingress:
        raise AuthConfigurationError("ADMIN_AUTH_INGRESS_HOSTS is required")
    ingress = tuple(normalize_ingress_authority("ADMIN_AUTH_INGRESS_HOSTS", entry) for entry in raw_ingress.split(","))
    if len(set(ingress)) != len(ingress):
        raise AuthConfigurationError("ADMIN_AUTH_INGRESS_HOSTS must not contain duplicates")
    if callback_parts.netloc.lower() not in ingress:
        raise AuthConfigurationError("callback authority must be in ADMIN_AUTH_INGRESS_HOSTS")
    if is_loopback and any(
        urlsplit(f"//{host}").hostname not in {"localhost", "127.0.0.1", "::1"}
        for host in ingress
    ):
        raise AuthConfigurationError("loopback development may allow only loopback ingress hosts")
    allowed = {item.strip() for item in values.get("ADMIN_ALLOWED_REDIRECTS", "").split(",") if item.strip()}
    callback_pattern = f"{callback}\\?flow_state=*"
    if callback_pattern not in allowed:
        raise AuthConfigurationError("callback correlation URL pattern must be explicitly allow-listed")

    flow_version = values.get("ADMIN_FLOW_KEY_VERSION", "")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", flow_version):
        raise AuthConfigurationError("ADMIN_FLOW_KEY_VERSION is required and malformed")
    anon = values.get("SUPABASE_ANON_KEY")
    if not anon or len(anon) < 16:
        raise AuthConfigurationError("SUPABASE_ANON_KEY is required")
    idle = _read_bound_int("ADMIN_SESSION_IDLE_SECONDS", values.get("ADMIN_SESSION_IDLE_SECONDS"), 1800, 60, 1800)
    absolute = _read_bound_int(
        "ADMIN_SESSION_ABSOLUTE_SECONDS", values.get("ADMIN_SESSION_ABSOLUTE_SECONDS"), 28800, idle, 28800
    )
    return AuthConfig(
        True,
        supabase,
        anon,
        app_origin,
        callback,
        callback_pattern,
        _secret_bytes(
            "ADMIN_FLOW_ENCRYPTION_KEY", values.get("ADMIN_FLOW_ENCRYPTION_KEY"), exact_length=32
        ),
        flow_version,
        _secret_bytes("ADMIN_SESSION_HASH_KEY", values.get("ADMIN_SESSION_HASH_KEY")),
        idle,
        absolute,
        dev_cookie and is_loopback and app_parts.scheme == "http",
        ingress,
    )
