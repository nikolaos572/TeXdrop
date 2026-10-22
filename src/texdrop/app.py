"""Application factory: wires the components together and runs the background threads."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from . import __version__, api, logs, sandbox, web
from .cleanup import CleanupScheduler
from .compiler import LatexCompiler, tex_directories
from .errors import InvalidInput
from .jobs import JobService
from .settings import Settings, SettingsError
from .storage import Storage
from .store import JobStore
from .unpack import Unpacker
from .users import UserRegistry
from .worker import Worker

log = logging.getLogger(__name__)

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
_MULTIPART_OVERHEAD = 1024**2  # room for form fields around the uploaded file


def create_app(settings: Settings | None = None, compiler: LatexCompiler | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    sandbox_abi = sandbox.landlock_abi()
    missing = sandbox.missing_protections(sandbox_abi, sandbox.syscall_filter_works())
    if missing and not settings.allow_unsandboxed:
        for protection in missing:
            log.error("missing protection: %s", protection)
        raise SettingsError(
            f"the sandbox lacks {len(missing)} of its protections here (listed above). It has all of them on "
            "Linux 6.12 or newer, on x86-64 or ARM64, with the server running as an unprivileged user. "
            "Set ALLOW_UNSANDBOXED=1 to start anyway, with the protections this system offers."
        )
    if sandbox.readable_inside(settings.data_dir, tex_directories()):
        raise SettingsError(f"DATA_DIR {settings.data_dir} lies in a directory that compilations can read")
    storage = Storage(settings.data_dir)
    storage.prepare()
    unpacker = Unpacker(settings.max_extracted_bytes, settings.unpack_timeout_seconds, settings.unpack_memory_bytes)
    service = JobService(
        JobStore(storage.db_path), storage, unpacker, max_user_storage_bytes=settings.max_user_storage_bytes
    )
    compiler = compiler or LatexCompiler(
        settings.compile_timeout_seconds,
        memory_bytes=settings.compile_memory_bytes,
        disk_bytes=settings.compile_disk_bytes,
    )
    worker = Worker(service, compiler, storage)
    cleanup = CleanupScheduler(service, settings.auto_delete_days)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if requeued := service.requeue_interrupted():
            log.info("requeued %d job(s) interrupted by the last shutdown", requeued)
        if sandbox_abi:
            log.info("compilations are sandboxed with Landlock (ABI %d)", sandbox_abi)
        if directories := tex_directories():
            log.info("compilations may also read TeX's directories %s", ", ".join(directories))
        for protection in missing:
            log.warning("reduced protection: %s", protection)
        if missing:
            log.warning("running with reduced protection, as ALLOW_UNSANDBOXED=1 allows")
        else:
            log.info("every protection of the sandbox is active")
        worker.start()
        cleanup.start()
        yield
        cleanup.stop()
        worker.stop()

    app = FastAPI(title="TeXdrop", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.storage = storage
    app.state.users = UserRegistry(storage.config_path)
    app.state.service = service
    app.state.worker = worker

    # Every refusal also gives its reason to the access log.
    def refuse(request: Request, status_code: int, detail: str) -> JSONResponse:
        logs.record_error(request, detail)
        return JSONResponse({"detail": detail}, status_code=status_code)

    @app.exception_handler(InvalidInput)
    async def invalid_input(request: Request, exc: InvalidInput) -> JSONResponse:
        return refuse(request, status.HTTP_400_BAD_REQUEST, str(exc))

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        logs.record_error(request, str(exc.detail))
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        reasons = (f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in exc.errors())
        logs.record_error(request, "; ".join(reasons))
        return await request_validation_exception_handler(request, exc)

    max_body = settings.max_upload_bytes + _MULTIPART_OVERHEAD

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        # Refuse oversized bodies before reading them. The server never reads past
        # Content-Length, so requiring the header bounds every upload.
        if request.method in _BODY_METHODS:
            length = request.headers.get("content-length", "")
            if not length.isdigit():
                return refuse(request, status.HTTP_411_LENGTH_REQUIRED, "Content-Length required")
            if int(length) > max_body:
                limit = settings.max_upload_bytes // 1024**2
                return refuse(request, status.HTTP_413_CONTENT_TOO_LARGE, f"the upload exceeds the {limit} MB limit")
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    app.middleware("http")(logs.access_log)  # added last, so it runs first and sees every response
    app.include_router(api.router)
    app.include_router(web.router)
    app.mount("/static", StaticFiles(directory=web.STATIC_DIR), name="static")
    return app
