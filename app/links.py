"""Transfer links: short-lived URLs through which AI agents upload and download files.

MCP tool calls carry only JSON that the model writes and reads, so the MCP server
(mcp_server) hands out links instead, which the agent uses with an HTTP client such as
curl. A link holds a random secret that stands in for the user's token, for one transfer:

- it works only from the address of the client that asked for it;
- it stops working LINK_EXPIRY_MINUTES after it was made;
- one complete transfer uses it up: a job created from its upload, or one whole
  download. A transfer that fails or is cut off leaves it valid, for another try.

The API checks a link when its request arrives, before an upload is received, so a
transfer that starts in time may end after the link expires. Links are kept in the jobs
database by the SHA-256 of their secret: the secrets themselves are only in the URLs.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from .store import connect

UPLOAD = "upload"  # what an upload link transfers; a download link names a job's file instead
REFUSAL = "no such link: links expire, work once and only from the address they were made for; ask for a new one"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    hash       TEXT PRIMARY KEY,  -- SHA-256 of the secret in the link's URL
    user       TEXT NOT NULL,
    file       TEXT NOT NULL,     -- 'upload', or the job's file it downloads: 'input', 'pdf' or 'log'
    job_id     INTEGER,           -- NULL for an upload
    client     TEXT NOT NULL,     -- the only address the link works from
    expires_at REAL NOT NULL      -- Unix time
)
"""


@dataclass(frozen=True)
class Link:
    hash: str
    user: str
    file: str
    job_id: int | None
    client: str
    expires_at: float


class LinkRefused(Exception):
    """The link cannot serve this request. The message says why, for the server's log;
    the client only ever gets REFUSAL, whatever the reason."""


class LinkService:
    def __init__(self, db_path: Path, lifetime_seconds: float, clock: Callable[[], float] = time.time) -> None:
        self._path = db_path
        self._lifetime = lifetime_seconds
        self._clock = clock
        with closing(connect(db_path)) as conn:
            conn.execute(_SCHEMA)

    def create(self, user: str, client: str, file: str, job_id: int | None = None) -> tuple[str, float]:
        """A new link for `client` to transfer `file`: its secret and the time it expires.
        Expired links are deleted on the way, so the table holds only recent ones."""
        secret = secrets.token_urlsafe(32)  # 256 random bits
        now = self._clock()
        expires_at = now + self._lifetime
        with closing(connect(self._path)) as conn:
            conn.execute("DELETE FROM links WHERE expires_at <= ?", (now,))
            conn.execute(
                "INSERT INTO links (hash, user, file, job_id, client, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (_hash(secret), user, file, job_id, client, expires_at),
            )
        return secret, expires_at

    def check(self, secret: str, client: str, *, upload: bool) -> Link:
        """The link with this secret, if `client` may use it now for an upload (or a download)."""
        with closing(connect(self._path)) as conn:
            row = conn.execute("SELECT * FROM links WHERE hash = ?", (_hash(secret),)).fetchone()
        if row is None:
            raise LinkRefused("unknown link")  # never made, used up, or expired and deleted since
        link = _link(row)
        if link.expires_at <= self._clock():
            raise LinkRefused("expired link")
        if link.client != client:
            raise LinkRefused(f"link for {link.client} used from {client}")
        if (link.file == UPLOAD) != upload:
            raise LinkRefused(f"{link.file} link used for {'an upload' if upload else 'a download'}")
        return link

    @contextmanager
    def using(self, link: Link) -> Iterator[None]:
        """Use a checked link for the enclosed transfer: it is used up when the block completes,
        and valid again when it raises. It is taken out meanwhile, so that of two transfers
        through the same link at the same time, one at most succeeds."""
        with closing(connect(self._path)) as conn:
            if not _delete(conn, link):
                raise LinkRefused("link used up meanwhile")
            try:
                yield
            except BaseException:
                conn.execute(
                    "INSERT OR IGNORE INTO links (hash, user, file, job_id, client, expires_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (link.hash, link.user, link.file, link.job_id, link.client, link.expires_at),
                )
                raise

    def use_up(self, link: Link) -> None:
        """Delete a link whose transfer has completed (a download, which happens after the
        request's handler returned, so outside of using())."""
        with closing(connect(self._path)) as conn:
            _delete(conn, link)


def _delete(conn: sqlite3.Connection, link: Link) -> bool:
    return conn.execute("DELETE FROM links WHERE hash = ?", (link.hash,)).rowcount == 1


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _link(row: sqlite3.Row) -> Link:
    return Link(
        hash=row["hash"],
        user=row["user"],
        file=row["file"],
        job_id=row["job_id"],
        client=row["client"],
        expires_at=row["expires_at"],
    )
