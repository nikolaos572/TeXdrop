"""Users and their API tokens, read from a JSON file the administrator edits.

The file maps user names to tokens, for example::

    {"users": {"alice": "4c1f...", "bob": "9e0d..."}}

The user name is also the name of the user's storage directory. The file is
re-read whenever it changes, so users can be added or removed without a restart.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import threading
from pathlib import Path

log = logging.getLogger(__name__)

NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~+/=-]{16,256}")  # safe in headers and cookies


class ConfigError(ValueError):
    """The users file is malformed."""


def parse_users(raw: object) -> dict[str, str]:
    """Validate the decoded JSON document and return a token -> user name map."""
    users = raw.get("users") if isinstance(raw, dict) else None
    if not isinstance(users, dict):
        raise ConfigError('expected an object like {"users": {"name": "token"}}')
    tokens: dict[str, str] = {}
    for name, token in users.items():
        if not NAME_PATTERN.fullmatch(name):
            raise ConfigError(f"invalid user name {name!r}: use 1-32 of a-z, 0-9, '_' and '-'")
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            raise ConfigError(f"invalid token for {name!r}: use 16-256 letters, digits or ._~+/=-")
        if token in tokens:
            raise ConfigError(f"users {tokens[token]!r} and {name!r} share a token")
        tokens[token] = name
    return tokens


class UserRegistry:
    """Maps tokens to user names, reloading the file when it changes."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._signature: tuple[int, int] | None = None
        self._tokens: dict[str, str] = {}
        self._reload(strict=True)

    def authenticate(self, token: str) -> str | None:
        """Return the user owning `token`, or None."""
        with self._lock:
            self._reload(strict=False)
            tokens = self._tokens
        candidate = token.encode()
        user = None
        for known, name in tokens.items():  # compare against all, in constant time
            if hmac.compare_digest(known.encode(), candidate):
                user = name
        return user

    def _reload(self, *, strict: bool) -> None:
        """Re-read the file if it changed. A broken edit keeps the previous users,
        except at startup (`strict`), where it is an error."""
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            if self._signature != (0, 0):
                log.warning("users file %s not found: every request will be rejected", self._path)
            self._signature, self._tokens = (0, 0), {}
            return
        signature = (stat.st_mtime_ns, stat.st_size)
        if signature == self._signature:
            return
        self._signature = signature
        try:
            self._tokens = parse_users(json.loads(self._path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:  # ConfigError and JSONDecodeError are ValueErrors
            if strict:
                raise ConfigError(f"{self._path}: {exc}") from exc
            log.error("ignoring invalid users file %s (keeping the previous users): %s", self._path, exc)
            return
        log.info("loaded %d user(s) from %s", len(self._tokens), self._path)
