"""The JSON API under /api, used by scripts and by the web UI alike, and the transfer
links under /api/links through which MCP clients move files (ENABLE_MCP=1)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from fastapi import Path as PathParameter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from starlette.types import Message, Receive, Scope, Send

from . import __version__
from .auth import current_user, registry
from .compiler import DEFAULT_ENGINE, ENGINES, tex_live_version
from .jobs import JobService
from .links import Link, LinkRefused, LinkService
from .settings import Settings
from .store import Job, Status

router = APIRouter(prefix="/api")

Engine = StrEnum("Engine", {name: name for name in ENGINES})
MAX_WAIT_SECONDS = 600
PER_PAGE = 5  # jobs per page unless asked otherwise
MAX_PER_PAGE = 100
MAX_PAGE = 1_000_000  # keeps the offset within SQLite's integers
MAX_JOB_ID = 2**63 - 1  # SQLite's largest integer
MCP_PATH = "/mcp"  # where the MCP server (mcp_server) answers when ENABLE_MCP=1
JobId = Annotated[int, PathParameter(ge=1, le=MAX_JOB_ID)]
_CHUNK = 1024 * 1024

# The form of an upload, the same for POST /api/jobs and for upload links.
Archive = Annotated[UploadFile, File(description="The project: .zip, .7z, .tar, .tar.gz, .tar.bz2 or .tar.xz")]
MainFile = Annotated[str | None, Form(max_length=1024, description="Main .tex file; detected when omitted")]
EngineChoice = Annotated[Engine, Form()]


class JobFile(StrEnum):
    """The files of a job that can be downloaded."""

    INPUT = "input"  # the uploaded archive
    PDF = "pdf"
    LOG = "log"


def local_time(timestamp: float, timezone: ZoneInfo) -> str:
    """A Unix time as the API shows dates: ISO 8601, in the configured time zone."""
    return datetime.fromtimestamp(timestamp, timezone).isoformat(timespec="seconds")


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
            return None if timestamp is None else local_time(timestamp, timezone)

        return cls(
            id=job.id,
            filename=job.filename,
            main=job.main,
            engine=job.engine,
            status=job.status,
            queue_position=job.queue_position,
            submitted_at=local_time(job.submitted_at, timezone),
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
    mcp_path: str | None  # the MCP endpoint on this server (ENABLE_MCP=1); None when it is off
    link_expiry_minutes: float  # an MCP transfer link stops working this long after it was made


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


def _links(request: Request) -> LinkService:
    return request.app.state.links


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


def _submit(request: Request, user: str, file: UploadFile, main: str | None, engine: Engine) -> JobOut:
    with _saved_upload(request, file) as path:
        job = _service(request).submit(user, path, file.filename, main, engine.value)
    request.app.state.worker.wake()
    return JobOut.of(job, _settings(request).timezone)


def _owned_job(request: Request, user: str, job_id: int) -> Job:
    job = _service(request).get(user, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such job")
    return job


def job_file(
    request: Request, user: str, job_id: int, file: JobFile, *, on_sent: Callable[[], None] | None = None
) -> FileResponse:
    """One of the files of a job of the user, after the checks of every download: the API's,
    the links', and the MCP server's before it makes a link. `on_sent` runs once the whole
    file has been sent."""
    job = _owned_job(request, user, job_id)
    service = _service(request)
    stem = PurePosixPath(job.main).stem
    if file is JobFile.INPUT:
        path, media_type, filename, inline = service.input_file(job), "application/octet-stream", job.filename, False
    elif file is JobFile.PDF:
        if not job.has_pdf:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "this job has no PDF")
        path, media_type, filename, inline = service.pdf_file(job), "application/pdf", f"{stem}.pdf", True
    else:
        if not job.status.finished:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "the log is available once the job has finished")
        path, media_type, filename, inline = service.log_file(job), "text/plain; charset=utf-8", f"{stem}.log", True
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    return _FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        content_disposition_type="inline" if inline else "attachment",
        on_sent=on_sent,
    )


class _FileResponse(FileResponse):
    """A FileResponse that can tell when the whole file has been sent: it then runs `on_sent`,
    unless the client went away first or asked for part of the file (a Range request). Sent
    means handed to the network; behind a reverse proxy, received by the proxy. No server
    can know more without the client confirming."""

    def __init__(self, path: Path, *, on_sent: Callable[[], None] | None, **kwargs: Any) -> None:
        super().__init__(path, **kwargs)
        self._on_sent = on_sent

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._on_sent is None:
            await super().__call__(scope, receive, send)
            return
        status_code = 0
        sent = disconnected = False

        async def watched_receive() -> Message:
            # FileResponse listens for the client going away while it streams the file.
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect" and not sent:  # after the end, the server says so too
                disconnected = True
            return message

        async def watched_send(message: Message) -> None:
            nonlocal status_code, sent
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                sent = True

        await super().__call__(scope, watched_receive, watched_send)
        if sent and not disconnected and status_code == status.HTTP_200_OK:
            await run_in_threadpool(self._on_sent)


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
            mcp_path=MCP_PATH if settings.enable_mcp else None,
            link_expiry_minutes=settings.link_expiry_seconds / 60,
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
    file: Archive,
    main: MainFile = None,
    engine: EngineChoice = Engine(DEFAULT_ENGINE),
    user: str = Depends(current_user),
) -> JobOut:
    """Queue a project for compilation."""
    return _submit(request, user, file, main, engine)


@router.get("/jobs", tags=["jobs"])
def list_jobs(
    request: Request,
    page: int = Query(1, ge=1, le=MAX_PAGE, description="Page number; page 1 has the newest jobs"),
    per_page: int = Query(PER_PAGE, ge=1, le=MAX_PER_PAGE, description="Jobs per page"),
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
    return job_file(request, user, job_id, JobFile.INPUT)


@router.get("/jobs/{job_id}/pdf", tags=["files"])
def download_pdf(request: Request, job_id: JobId, user: str = Depends(current_user)) -> FileResponse:
    """The compiled PDF (also kept when the compilation reported errors but produced one)."""
    return job_file(request, user, job_id, JobFile.PDF)


@router.get("/jobs/{job_id}/log", tags=["files"])
def download_log(request: Request, job_id: JobId, user: str = Depends(current_user)) -> FileResponse:
    """The compilation log (latexmk's output, including every TeX and BibTeX/Biber run)."""
    return job_file(request, user, job_id, JobFile.LOG)


# --- transfer links (see links.py), served when ENABLE_MCP=1 ---


class _LinkRoute(APIRoute):
    """A route under /api/links/{secret}. It checks the link as soon as the request arrives,
    before an upload is read: a refused upload is not received, and a transfer that starts
    while the link is valid may end after it expires."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handle = super().get_route_handler()

        async def check_then_handle(request: Request) -> Response:
            request.state.link = await run_in_threadpool(_checked_link, request)
            return await handle(request)

        return check_then_handle


def _checked_link(request: Request) -> Link:
    """The link in the request's path, if it may serve this request; raises LinkRefused otherwise."""
    client = request.client.host if request.client else ""
    link = _links(request).check(request.path_params["secret"], client, upload=request.method == "POST")
    if not registry(request).exists(link.user):  # removed from the users file since
        raise LinkRefused(f"link of {link.user!r}, who is no longer a user")
    request.state.user = link.user  # for the access log
    return link


link_router = APIRouter(prefix="/api/links", route_class=_LinkRoute, tags=["links"])
LinkSecret = Annotated[str, PathParameter(description="The link's secret, as the MCP server gave it")]


@link_router.post("/{secret}", status_code=status.HTTP_201_CREATED)
def upload_through_link(
    request: Request,
    secret: LinkSecret,
    file: Archive,
    main: MainFile = None,
    engine: EngineChoice = Engine(DEFAULT_ENGINE),
) -> JobOut:
    """Queue a project, as POST /api/jobs does, through an upload link of the MCP server
    instead of a token. A refused upload leaves the link valid."""
    link: Link = request.state.link
    with _links(request).using(link):
        return _submit(request, link.user, file, main, engine)


@link_router.get("/{secret}")
def download_through_link(request: Request, secret: LinkSecret) -> FileResponse:
    """A job's archive, PDF or log, as the API's downloads give them, through a download link
    of the MCP server instead of a token. A download cut off before its end leaves the link valid."""
    link: Link = request.state.link
    assert link.job_id is not None  # download links always name their job
    return job_file(request, link.user, link.job_id, JobFile(link.file), on_sent=partial(_links(request).use_up, link))
