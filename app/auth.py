"""Authentication: a token in the Authorization header (API), or the web page's cookie, which
holds a session (sessions.py) rather than the token (web UI)."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .sessions import Sessions
from .users import UserRegistry

COOKIE_NAME = "texdrop_session"
COOKIE_MAX_AGE = 30 * 86_400  # seconds, also the session's lifetime
# The web UI sends this header with every change; another site cannot add it to a
# request without a CORS preflight, which this server never approves.
CSRF_HEADER = "X-Requested-With"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_bearer = HTTPBearer(auto_error=False, description="Your API token, sent as `Authorization: Bearer <token>`.")


def current_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> str:
    """The user owning the request's token (or session); rejects the request otherwise."""
    if credentials is None and (session_id := request.cookies.get(COOKIE_NAME, "")):
        if request.method not in _SAFE_METHODS and CSRF_HEADER not in request.headers:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"cookie-authenticated requests need a {CSRF_HEADER} header")
        user = sessions(request).user(session_id)
        if user is None:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "the session ended or expired: log in again", headers={"WWW-Authenticate": "Bearer"}
            )
        request.state.user = user  # for the access log
        return user
    return _authenticated(request, credentials.credentials if credentials is not None else "")


async def header_user(request: Request) -> str:
    """Like current_user, but only from the Authorization header, never the cookie: for the
    MCP endpoint, which no web page calls."""
    credentials = await _bearer(request)
    return _authenticated(request, credentials.credentials if credentials is not None else "")


def _authenticated(request: Request, token: str) -> str:
    user = registry(request).authenticate(token) if token else None
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid token", headers={"WWW-Authenticate": "Bearer"})
    request.state.user = user  # for the access log
    return user


def registry(request: Request) -> UserRegistry:
    return request.app.state.users


def sessions(request: Request) -> Sessions:
    return request.app.state.sessions
