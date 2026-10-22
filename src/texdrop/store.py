"""Job metadata in SQLite.

SQLite is a library, not a server: the database is the file jobs.db in the data
directory (with -wal/-shm companions while it is open). WAL mode lets web
requests read while the worker writes. Every transaction opens its own
short-lived connection, so no connection is ever shared between threads, and
write transactions are serialized by SQLite itself (BEGIN IMMEDIATE).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Status(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"

    @property
    def finished(self) -> bool:
        return self in (Status.DONE, Status.FAILED)


@dataclass(frozen=True)
class Job:
    id: int
    user: str
    filename: str  # as uploaded, sanitized
    extension: str  # of the stored archive, e.g. ".tar.gz"
    main: str
    engine: str
    status: Status
    submitted_at: float  # Unix time
    started_at: float | None
    finished_at: float | None
    error: str | None
    has_pdf: bool
    queue_position: int | None = None  # 1 = runs next; None unless queued


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reused: id order is submission order
    user         TEXT    NOT NULL,
    filename     TEXT    NOT NULL,
    extension    TEXT    NOT NULL,
    main         TEXT    NOT NULL,
    engine       TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    submitted_at REAL    NOT NULL,
    started_at   REAL,
    finished_at  REAL,
    error        TEXT,
    has_pdf      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_by_user ON jobs (user, id);
CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs (status, id);
"""

_SELECT = """
SELECT *, CASE WHEN status = 'queued' THEN
    (SELECT COUNT(*) FROM jobs AS ahead WHERE ahead.status = 'queued' AND ahead.id <= jobs.id)
END AS queue_position FROM jobs
"""


class JobStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    @contextmanager
    def transaction(self, *, write: bool) -> Iterator[JobTable]:
        """Run the enclosed queries atomically. Work done inside a write
        transaction (such as moving files) happens before any other write."""
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield JobTable(conn)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30, isolation_level=None)  # explicit transactions
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=NORMAL")  # durable enough with WAL, and faster
        return conn


class JobTable:
    """Queries on the jobs table, bound to one open transaction."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(self, *, user: str, filename: str, extension: str, main: str, engine: str, now: float) -> Job:
        cursor = self._conn.execute(
            "INSERT INTO jobs (user, filename, extension, main, engine, status, submitted_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user, filename, extension, main, engine, Status.QUEUED, now),
        )
        job = self.get(user, cursor.lastrowid)
        assert job is not None
        return job

    def get(self, user: str, job_id: int) -> Job | None:
        row = self._conn.execute(_SELECT + "WHERE user = ? AND id = ?", (user, job_id)).fetchone()
        return _job(row) if row else None

    def list(self, user: str, *, offset: int = 0, limit: int = -1) -> list[Job]:
        """The user's jobs, newest first: at most `limit` of them (-1: all) after skipping `offset`."""
        rows = self._conn.execute(
            _SELECT + "WHERE user = ? ORDER BY id DESC LIMIT ? OFFSET ?", (user, limit, offset)
        ).fetchall()
        return [_job(row) for row in rows]

    def count(self, user: str) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM jobs WHERE user = ?", (user,)).fetchone()[0]

    def count_with_status(self, status: Status) -> int:
        """Jobs of all users with this status."""
        return self._conn.execute("SELECT COUNT(*) FROM jobs WHERE status = ?", (status,)).fetchone()[0]

    def delete(self, user: str, job_id: int | None = None) -> list[Job]:
        """Delete one job of the user, or all of them when `job_id` is None."""
        if job_id is None:
            rows = self._conn.execute("DELETE FROM jobs WHERE user = ? RETURNING *", (user,)).fetchall()
        else:
            rows = self._conn.execute("DELETE FROM jobs WHERE user = ? AND id = ? RETURNING *", (user, job_id)).fetchall()
        return [_job(row) for row in rows]

    def claim_next(self, now: float) -> Job | None:
        """Mark the oldest queued job (of any user) as processing and return it."""
        row = self._conn.execute(
            "UPDATE jobs SET status = ?, started_at = ?"
            " WHERE id = (SELECT id FROM jobs WHERE status = ? ORDER BY id LIMIT 1) RETURNING *",
            (Status.PROCESSING, now, Status.QUEUED),
        ).fetchone()
        return _job(row) if row else None

    def finish(self, job_id: int, *, status: Status, error: str | None, has_pdf: bool, now: float) -> bool:
        """Record the result of a processing job; False if the job was deleted meanwhile."""
        cursor = self._conn.execute(
            "UPDATE jobs SET status = ?, error = ?, has_pdf = ?, finished_at = ? WHERE id = ? AND status = ?",
            (status, error, has_pdf, now, job_id, Status.PROCESSING),
        )
        return cursor.rowcount == 1

    def requeue_interrupted(self) -> int:
        """Put jobs that were processing when the server stopped back in the queue."""
        cursor = self._conn.execute(
            "UPDATE jobs SET status = ?, started_at = NULL WHERE status = ?", (Status.QUEUED, Status.PROCESSING)
        )
        return cursor.rowcount

    def delete_finished_before(self, cutoff: float) -> list[Job]:
        rows = self._conn.execute("DELETE FROM jobs WHERE finished_at < ? RETURNING *", (cutoff,)).fetchall()
        return [_job(row) for row in rows]


def _job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        user=row["user"],
        filename=row["filename"],
        extension=row["extension"],
        main=row["main"],
        engine=row["engine"],
        status=Status(row["status"]),
        submitted_at=row["submitted_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error=row["error"],
        has_pdf=bool(row["has_pdf"]),
        # Absent from RETURNING rows. (`in row` would search the values, hence .keys().)
        queue_position=row["queue_position"] if "queue_position" in row.keys() else None,
    )
