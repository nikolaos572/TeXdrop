"""The JSON API under /api, used by scripts and by the web UI alike."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi import Path as PathParameter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import __version__
from .auth import current_user
from .compiler import DEFAULT_ENGINE, ENGINES, tex_live_version
from .jobs import JobService
from .settings import Settings
from .store import Job, Status

router = APIRouter(prefix="/api")

Engine = StrEnum("Engine", {name: name for name in ENGINES})
MAX_WAIT_SECONDS = 600
MAX_PER_PAGE = 100
MAX_PAGE = 1_000_000  # keeps the offset within SQLite's integers
MAX_JOB_ID = 2**63 - 1  # SQLite's largest integer
JobId = Annotated[int, PathParameter(ge=1, le=MAX_JOB_ID)]
_CHUNK = 1024 * 1024


class JobOut(BaseModel):
    id: int
    filename: str
    main: str
    engine: str
    status: Status
    queue_position: int | None
    submitted_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None
    has_pdf: bool
    has_log: bool

    @classmethod
    def of(cls, job: Job, timezone: ZoneInfo) -> JobOut:
        def local(timestamp: float | None) -> str | None:
            if timestamp is None:
                return None
            return datetime.fromtimestamp(timestamp, timezone).isoformat(timespec="seconds")

        return cls(
            id=job.id,
            filename=job.filename,
            main=job.main,
            engine=job.engine,
            status=job.status,
            queue_position=job.queue_position,
            submitted_at=local(job.submitted_at),
            started_at=local(job.started_at),
            finished_at=local(job.finished_at),
            error=job.error,
            has_pdf=job.has_pdf,
            has_log=job.status.finished,
        )


class JobListOut(BaseModel):
    jobs: list[JobOut]
    total: int  # all of the user's jobs, on every page
    page: int
    per_page: int


class InspectOut(BaseModel):
    format: str
    tex_files: list[str]
    main: str | None


class DeletedOut(BaseModel):
    deleted: int


class ServerOut(BaseModel):
    version: str
    tex_live: str | None
    engines: list[str]
    default_engine: str
    timezone: str  # of the dates in job details
    auto_delete_days: int  # finished jobs are deleted this many days later; 0: never
    compile_timeout_minutes: float
    max_upload_mb: int
    max_extracted_mb: int  # total size of an archive's unpacked files
    compile_memory_mb: int  # for each process of a compilation; 0: no limit
    compile_disk_mb: int  # for a compilation's files; 0: no limit
    max_user_storage_mb: int  # for each user's archives, PDFs and logs; 0: no limit
    unpack_timeout_minutes: float  # for inspecting or unpacking an archive
    unpack_memory_mb: int  # for the process that does it; 0: no limit


class QueueOut(BaseModel):
    queued: int  # jobs of all users
    processing: int


class AccountOut(BaseModel):
    user: str
    jobs: int
    storage_bytes: int  # archives, PDFs and logs


class InfoOut(BaseModel):
    server: ServerOut
    queue: QueueOut
    account: AccountOut


def _service(request: Request) -> JobService:
    return request.app.state.service


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@contextmanager
def _saved_upload(request: Request, upload: UploadFile) -> Iterator[Path]:
    """Copy the upload to a temporary file, enforcing the size limit."""
    settings = _settings(request)
    path = request.app.state.storage.temp_path("upload")
    try:
        with path.open("wb") as output:
            size = 0
            while chunk := upload.file.read(_CHUNK):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(
                        status.HTTP_413_CONTENT_TOO_LARGE,
                        f"the file exceeds the {settings.max_upload_bytes // 1024**2} MB limit",
                    )
                output.write(chunk)
        yield path
    finally:
        path.unlink(missing_ok=True)  # already moved away if a job was created


def _owned_job(request: Request, user: str, job_id: int) -> Job:
    job = _service(request).get(user, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such job")
    return job


def _file(path: Path, *, media_type: str, filename: str, inline: bool) -> FileResponse:
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    return FileResponse(
        path, media_type=media_type, filename=filename, content_disposition_type="inline" if inline else "attachment"
    )


@router.get("/health", tags=["status"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/info", tags=["status"])
def server_info(request: Request, user: str = Depends(current_user)) -> InfoOut:
    """The server's settings and limits, the shared queue, and your jobs and storage."""
    settings = _settings(request)
    usage = _service(request).usage(user)
    return InfoOut(
        server=ServerOut(
            version=__version__,
            tex_live=tex_live_version(),
            engines=list(ENGINES),
            default_engine=DEFAULT_ENGINE,
            timezone=settings.timezone.key,
            auto_delete_days=settings.auto_delete_days,
            compile_timeout_minutes=settings.compile_timeout_seconds / 60,
            max_upload_mb=settings.max_upload_bytes // 1024**2,
            max_extracted_mb=settings.max_extracted_bytes // 1024**2,
            compile_memory_mb=settings.compile_memory_bytes // 1024**2,
            compile_disk_mb=settings.compile_disk_bytes // 1024**2,
            max_user_storage_mb=settings.max_user_storage_bytes // 1024**2,
            unpack_timeout_minutes=settings.unpack_timeout_seconds / 60,
            unpack_memory_mb=settings.unpack_memory_bytes // 1024**2,
        ),
        queue=QueueOut(queued=usage.queued, processing=usage.processing),
        account=AccountOut(user=user, jobs=usage.jobs, storage_bytes=usage.storage_bytes),
    )


@router.post("/inspect", tags=["jobs"])
def inspect(request: Request, file: UploadFile = File(...), user: str = Depends(current_user)) -> InspectOut:
    """List the archive's .tex files and the main file that would be compiled."""
    with _saved_upload(request, file) as path:
        info = _service(request).inspect(path)
    return InspectOut(format=info.format.name, tex_files=list(info.tex_files), main=info.main)


@router.post("/jobs", status_code=status.HTTP_201_CREATED, tags=["jobs"])
def submit(
    request: Request,
    file: UploadFile = File(..., description="The project: .zip, .7z, .tar, .tar.gz, .tar.bz2 or .tar.xz"),
    main: str | None = Form(None, max_length=1024, description="Main .tex file; detected when omitted"),
    engine: Engine = Form(Engine(DEFAULT_ENGINE)),
    user: str = Depends(current_user),
) -> JobOut:
    """Queue a project for compilation."""
    with _saved_upload(request, file) as path:
        job = _service(request).submit(user, path, file.filename, main, engine.value)
    request.app.state.worker.wake()
    return JobOut.of(job, _settings(request).timezone)


@router.get("/jobs", tags=["jobs"])
def list_jobs(
    request: Request,
    page: int = Query(1, ge=1, le=MAX_PAGE, description="Page number; page 1 has the newest jobs"),
    per_page: int = Query(5, ge=1, le=MAX_PER_PAGE, description="Jobs per page"),
    user: str = Depends(current_user),
) -> JobListOut:
    """Your jobs, newest first, one page at a time. A page past the last one is empty."""
    timezone = _settings(request).timezone
    jobs, total = _service(request).list(user, offset=(page - 1) * per_page, limit=per_page)
    return JobListOut(jobs=[JobOut.of(job, timezone) for job in jobs], total=total, page=page, per_page=per_page)


@router.get("/jobs/{job_id}", tags=["jobs"])
async def get_job(
    request: Request,
    job_id: JobId,
    wait: int = Query(0, ge=0, le=MAX_WAIT_SECONDS, description="Seconds to wait for the job to finish"),
    user: str = Depends(current_user),
) -> JobOut:
    deadline = time.monotonic() + wait
    while True:
        job = await run_in_threadpool(_owned_job, request, user, job_id)
        if job.status.finished or time.monotonic() >= deadline or await request.is_disconnected():
            return JobOut.of(job, _settings(request).timezone)
        await asyncio.sleep(1)


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["jobs"])
def delete_job(request: Request, job_id: JobId, user: str = Depends(current_user)) -> None:
    """Delete a job and its files, stopping its compilation if it is running."""
    deleted = _service(request).delete(user, job_id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such job")
    request.app.state.worker.cancel([job_id])


@router.delete("/jobs", tags=["jobs"])
def delete_all_jobs(request: Request, user: str = Depends(current_user)) -> DeletedOut:
    """Delete all your jobs, whatever their status."""
    deleted = _service(request).delete(user)
    request.app.state.worker.cancel(job.id for job in deleted)
    return DeletedOut(deleted=len(deleted))


@router.get("/jobs/{job_id}/input", tags=["files"])
def download_input(request: Request, job_id: JobId, user: str = Depends(current_user)) -> FileResponse:
    """The uploaded archive."""
    job = _owned_job(request, user, job_id)
    return _file(_service(request).input_file(job), media_type="application/octet-stream", filename=job.filename, inline=False)


@router.get("/jobs/{job_id}/pdf", tags=["files"])
def download_pdf(request: Request, job_id: JobId, user: str = Depends(current_user)) -> FileResponse:
    """The compiled PDF (also kept when the compilation reported errors but produced one)."""
    job = _owned_job(request, user, job_id)
    if not job.has_pdf:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "this job has no PDF")
    name = f"{PurePosixPath(job.main).stem}.pdf"
    return _file(_service(request).pdf_file(job), media_type="application/pdf", filename=name, inline=True)


@router.get("/jobs/{job_id}/log", tags=["files"])
def download_log(request: Request, job_id: JobId, user: str = Depends(current_user)) -> FileResponse:
    """The compilation log (latexmk's output, including every TeX and BibTeX/Biber run)."""
    job = _owned_job(request, user, job_id)
    if not job.status.finished:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "the log is available once the job has finished")
    name = f"{PurePosixPath(job.main).stem}.log"
    return _file(_service(request).log_file(job), media_type="text/plain; charset=utf-8", filename=name, inline=True)
