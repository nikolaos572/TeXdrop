"""The job lifecycle: submit, list, delete, and the worker's claim and complete steps.

Files are moved inside the database transaction that creates, finishes or
deletes a job, so a job and its files appear and disappear together even when
the worker and web requests act on the same job at the same time.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .archive import ArchiveInfo, resolve_main
from .compiler import ENGINES
from .errors import InvalidInput
from .storage import Storage
from .store import Job, JobStore, Status
from .unpack import Unpacker

_UNSAFE_FILENAME_CHARS = re.compile(r"[^\w .,+=@()\[\]-]")
SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class Usage:
    jobs: int  # the user's
    storage_bytes: int  # taken by the user's files
    queued: int  # jobs of all users waiting to compile
    processing: int  # jobs of all users compiling now


class JobService:
    def __init__(
        self,
        store: JobStore,
        storage: Storage,
        unpacker: Unpacker,
        clock: Callable[[], float] = time.time,
        *,
        max_user_storage_bytes: int = 0,  # 0: no limit
    ) -> None:
        self._store = store
        self._storage = storage
        self._unpacker = unpacker
        self._clock = clock
        self._max_user_storage_bytes = max_user_storage_bytes

    def inspect(self, upload: Path) -> ArchiveInfo:
        return self._unpacker.inspect(upload)

    def extract(self, job: Job, destination: Path) -> ArchiveInfo:
        """Unpack the job's archive into `destination` for compiling it."""
        return self._unpacker.extract(self.input_file(job), destination)

    def submit(self, user: str, upload: Path, filename: str | None, main: str | None, engine: str) -> Job:
        """Validate the uploaded archive and queue it; the upload file is moved into storage."""
        if engine not in ENGINES:
            raise InvalidInput(f"unknown engine {engine!r}; choose one of: {', '.join(ENGINES)}")
        info = self.inspect(upload)
        main = resolve_main(info, main)
        with self._store.transaction(write=True) as jobs:  # also keeps two uploads from sharing the room left
            self._check_storage(user, upload)
            job = jobs.insert(
                user=user,
                filename=safe_filename(filename) or f"upload{info.format.extension}",
                extension=info.format.extension,
                main=main,
                engine=engine,
                now=self._clock(),
            )
            self._storage.store_input(upload, user, job.id, job.extension)
        return job

    def get(self, user: str, job_id: int) -> Job | None:
        with self._store.transaction(write=False) as jobs:
            return jobs.get(user, job_id)

    def list(self, user: str, *, offset: int = 0, limit: int = -1) -> tuple[list[Job], int]:
        """A page of the user's jobs (newest first) and the number of jobs the user has in all."""
        with self._store.transaction(write=False) as jobs:
            return jobs.list(user, offset=offset, limit=limit), jobs.count(user)

    def usage(self, user: str) -> Usage:
        """The user's jobs and storage, and the state of the shared queue."""
        with self._store.transaction(write=False) as jobs:
            count = jobs.count(user)
            queued = jobs.count_with_status(Status.QUEUED)
            processing = jobs.count_with_status(Status.PROCESSING)
        return Usage(jobs=count, storage_bytes=self._storage.user_size(user), queued=queued, processing=processing)

    def delete(self, user: str, job_id: int | None = None) -> list[Job]:
        """Delete one of the user's jobs, or all of them, with their files."""
        with self._store.transaction(write=True) as jobs:
            deleted = jobs.delete(user, job_id)
            self._remove_files(deleted)
        return deleted

    def delete_expired(self, max_age_days: int) -> int:
        """Delete jobs that finished more than `max_age_days` ago."""
        cutoff = self._clock() - max_age_days * SECONDS_PER_DAY
        with self._store.transaction(write=True) as jobs:
            expired = jobs.delete_finished_before(cutoff)
            self._remove_files(expired)
        return len(expired)

    def requeue_interrupted(self) -> int:
        with self._store.transaction(write=True) as jobs:
            return jobs.requeue_interrupted()

    def claim_next(self) -> Job | None:
        with self._store.transaction(write=True) as jobs:
            return jobs.claim_next(self._clock())

    def complete(self, job: Job, *, pdf: Path | None, log: Path, error: str | None) -> bool:
        """Store a processed job's results; False if the job was deleted meanwhile."""
        status = Status.DONE if error is None else Status.FAILED
        with self._store.transaction(write=True) as jobs:
            if not jobs.finish(job.id, status=status, error=error, has_pdf=pdf is not None, now=self._clock()):
                return False
            self._storage.store_outputs(job.user, job.id, pdf, log)
        return True

    def input_file(self, job: Job) -> Path:
        return self._storage.input_path(job.user, job.id, job.extension)

    def pdf_file(self, job: Job) -> Path:
        return self._storage.pdf_path(job.user, job.id)

    def log_file(self, job: Job) -> Path:
        return self._storage.log_path(job.user, job.id)

    def _check_storage(self, user: str, upload: Path) -> None:
        """Refuse an upload that does not fit in the user's storage (MAX_USER_STORAGE_MB)."""
        if not self._max_user_storage_bytes:
            return
        used, needed = self._storage.user_size(user), upload.stat().st_size
        if used + needed > self._max_user_storage_bytes:
            raise InvalidInput(
                f"not enough storage: your jobs take {_size(used)} of your {_size(self._max_user_storage_bytes)}"
                f" and this archive needs {_size(needed)}; delete old jobs first"
            )

    def _remove_files(self, jobs: list[Job]) -> None:
        for job in jobs:
            self._storage.remove_job(job.user, job.id, job.extension)


def _size(size: int) -> str:
    return f"{size / 1024**2:.1f} MB" if size >= 1024**2 else f"{size / 1024:.0f} KB"


def safe_filename(name: str | None) -> str:
    """The last component of a client-supplied file name, with unusual characters replaced."""
    base = re.split(r"[/\\]", name or "")[-1]
    return _UNSAFE_FILENAME_CHARS.sub("_", base).strip(" .")[:200]
