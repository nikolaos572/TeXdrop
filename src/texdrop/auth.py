"""Authentication: a token in the Authorization header (API) or in a cookie (web UI)."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .users import UserRegistry

COOKIE_NAME = "latex_token"
COOKIE_MAX_AGE = 30 * 86_400  # seconds
# The web UI sends this header with every change; another site cannot add it to a
# request without a CORS preflight, which this server never approves.
CSRF_HEADER = "X-Requested-With"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_bearer = HTTPBearer(auto_error=False, description="Your API token, sent as `Authorization: Bearer <token>`.")


def current_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> str:
    """The user owning the request's token; rejects the request otherwise."""
    if credentials is not None:
        token = credentials.credentials
    else:
        token = request.cookies.get(COOKIE_NAME, "")
        if token and request.method not in _SAFE_METHODS and CSRF_HEADER not in request.headers:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"cookie-authenticated requests need a {CSRF_HEADER} header")
    user = registry(request).authenticate(token) if token else None
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid token", headers={"WWW-Authenticate": "Bearer"})
    request.state.user = user  # for the access log
    return user


def registry(request: Request) -> UserRegistry:
    return request.app.state.users
