import hmac
import os
from typing import Annotated

from fastapi import Header, HTTPException


def require_refresh_auth(authorization: Annotated[str | None, Header()] = None) -> None:
    environment = os.environ.get("XBRAY_ENV")
    if environment in {"local", "test"}:
        return
    token = os.environ.get("XBRAY_REFRESH_TOKEN")
    if not token:
        raise HTTPException(status_code=503, detail="refresh_auth_unconfigured")
    prefix = "Bearer "
    if authorization is None or not authorization.startswith(prefix):
        raise HTTPException(
            status_code=401,
            detail="unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )
    supplied = authorization[len(prefix):]
    if not hmac.compare_digest(supplied, token):
        raise HTTPException(
            status_code=401,
            detail="unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )
