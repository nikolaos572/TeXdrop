"""The MCP server at /mcp (ENABLE_MCP=1): the API's operations as tools for AI agents.

An agent (Claude Code, Antigravity, ...) connects with the user's token in the
Authorization header and finds the tools itself. Each tool runs the code of the API
endpoint it stands for, so the checks, the limits and the jobs are those of the API and
the web page. Tool calls carry only JSON, so files travel through transfer links
instead (see links.py): upload_link and download_link hand out a link and the curl
command that uses it, which the agent runs.

The endpoint is stateless and answers in plain JSON, without event streams: each call
is authenticated on its own, and any reverse proxy passes the answers through.
"""

from __future__ import annotations

import logging
import shlex
from collections.abc import Iterator
from contextlib import AbstractAsyncContextManager, contextmanager
from typing import Annotated, Literal

from fastapi import HTTPException, Request
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.types import Receive, Scope, Send

from . import __version__, api, logs
from .auth import header_user
from .links import UPLOAD, LinkService
from .settings import Settings

MAX_WAIT_SECONDS = 50  # per call: reverse proxies commonly give up on an answer after 60 seconds

INSTRUCTIONS = """\
TeXdrop compiles LaTeX projects to PDF with the full TeX Live: pdfLaTeX, XeLaTeX or LuaLaTeX, \
with BibTeX or Biber run as needed. To compile a project:

1. Pack the project's folder into a .zip archive (.tar.gz, .tar.bz2, .tar.xz, .tar and .7z work too).
2. Call upload_link, and run the curl command it returns with your archive in place of PROJECT.zip. \
It prints the new job as JSON: note its id.
3. Call get_job with that id, again while the job is queued or processing. A failed job's error \
field has the first lines of TeX's errors.
4. Call download_link for the PDF (a failed job may have one too: see has_pdf) or the full log, \
and run the curl command it returns.

A link works for {minutes:g} minutes, only from this computer, for one complete transfer; a \
transfer that fails or is cut off leaves it valid, for another try. A link refused with 404 has \
expired or been used: ask for a new one. Jobs are shared with the user's web page and API.\
"""

# Hints for clients, which may run read-only tools without asking and ask before destructive ones.
_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
_MAKES_LINK = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
_DELETES = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)

JobIdArgument = Annotated[int, Field(ge=1, le=api.MAX_JOB_ID, description="The job's id")]


class LinkOut(BaseModel):
    url: str
    method: Literal["POST", "GET"]
    expires_at: str  # unless used before; in the server's time zone, like the dates of jobs
    command: str  # a curl command line that uses the link


class McpEndpoint:
    """The ASGI app of the route /mcp: authenticates each request, then lets the SDK answer it."""

    def __init__(self, settings: Settings) -> None:
        # The SDK logs each request at INFO ("Terminating session: None"); the access log has them already.
        logging.getLogger("mcp").setLevel(logging.WARNING)
        server = _server(settings)
        # This builds the SDK's session manager; the Starlette app it also returns is not used, since
        # the endpoint is a route of TeXdrop's app, with its middleware and error handling.
        server.streamable_http_app(
            stateless_http=True,
            json_response=True,
            # The SDK's protection against DNS rebinding would accept only localhost in the Host header.
            # A web page cannot call this endpoint anyway: it needs the token in the Authorization
            # header, which browsers do not add by themselves (the web page's cookie is not accepted).
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        self._sessions = server.session_manager
        self._app = StreamableHTTPASGIApp(self._sessions)

    def running(self) -> AbstractAsyncContextManager[None]:
        """The context in which the endpoint serves requests, entered by the app's lifespan."""
        return self._sessions.run()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await header_user(Request(scope))  # 401 without a valid token; sets request.state.user for the tools
        await self._app(scope, receive, send)


def _server(settings: Settings) -> MCPServer:
    """The tools. Each gets the HTTP request of its call, authenticated by McpEndpoint, and passes
    it to the API's code, which reads the app's state from it."""
    server = MCPServer(
        "TeXdrop",
        title="TeXdrop",
        description="Compiles LaTeX projects to PDF.",
        version=__version__,
        instructions=INSTRUCTIONS.format(minutes=settings.link_expiry_seconds / 60),
    )

    @server.tool(title="Server information", annotations=_READ_ONLY)
    def server_info(ctx: Context) -> api.InfoOut:
        """The server's settings and limits (engines, largest upload, compilation timeout, ...),
        the queue of all users, and your account: your number of jobs and the storage they take."""
        request = _request(ctx)
        return api.server_info(request, request.state.user)

    @server.tool(title="Upload link", annotations=_MAKES_LINK)
    def upload_link(ctx: Context) -> LinkOut:
        """A link to upload a LaTeX project for compilation, and the curl command that does it:
        replace PROJECT.zip with the path of your archive (.zip, .tar.gz, .tar.bz2, .tar.xz, .tar
        or .7z). The upload prints the new job as JSON, with its id for get_job. Optional form
        fields, added to the command like the archive: -F main=FILE.tex, the main file relative to
        the project's root (detected when there is a single candidate), and -F engine=pdflatex
        (the default), xelatex or lualatex. A refused upload prints why, and the link stays valid
        for another try."""
        return _new_link(_request(ctx), UPLOAD)

    @server.tool(title="List jobs", annotations=_READ_ONLY)
    def list_jobs(
        ctx: Context,
        page: Annotated[int, Field(ge=1, le=api.MAX_PAGE, description="Page 1 has the newest jobs")] = 1,
        per_page: Annotated[int, Field(ge=1, le=api.MAX_PER_PAGE, description="Jobs per page")] = api.PER_PAGE,
    ) -> api.JobListOut:
        """Your jobs (also those submitted through the web page or the API), newest first, one
        page at a time, and how many there are. A page past the last one is empty."""
        request = _request(ctx)
        return api.list_jobs(request, page, per_page, request.state.user)

    @server.tool(title="Get a job", annotations=_READ_ONLY)
    async def get_job(
        ctx: Context,
        job_id: JobIdArgument,
        wait_seconds: Annotated[
            int, Field(ge=0, le=MAX_WAIT_SECONDS, description="How long to wait for the job to finish")
        ] = MAX_WAIT_SECONDS,
    ) -> api.JobOut:
        """A job: its status (queued, processing, done or failed), its place in the queue, and
        for a failed job the first lines of TeX's errors in error. Returns as soon as the job
        finishes, or after wait_seconds: call again while it is queued or processing."""
        request = _request(ctx)
        with _tool_errors(request):
            return await api.get_job(request, job_id, wait_seconds, request.state.user)

    @server.tool(title="Download link", annotations=_MAKES_LINK)
    def download_link(
        ctx: Context,
        job_id: JobIdArgument,
        file: Annotated[
            api.JobFile, Field(description="pdf, log (the full compilation log), or input (the uploaded archive)")
        ],
    ) -> LinkOut:
        """A link to download a file of a job, and the curl command that saves it in the current
        directory. The PDF exists when has_pdf is true, the log once the job has finished."""
        request = _request(ctx)
        with _tool_errors(request):  # the download's own checks, before making a link for it
            filename = api.job_file(request, request.state.user, job_id, file).filename
        return _new_link(request, file, job_id, filename)

    @server.tool(title="Delete a job", annotations=_DELETES)
    def delete_job(ctx: Context, job_id: JobIdArgument) -> api.DeletedOut:
        """Delete a job and its files (archive, PDF and log), stopping its compilation if it is running."""
        request = _request(ctx)
        with _tool_errors(request):
            api.delete_job(request, job_id, request.state.user)
        return api.DeletedOut(deleted=1)

    @server.tool(title="Delete all jobs", annotations=_DELETES)
    def delete_all_jobs(ctx: Context) -> api.DeletedOut:
        """Delete ALL your jobs and their files, whatever their status, including the jobs
        submitted through the web page and the API."""
        request = _request(ctx)
        return api.delete_all_jobs(request, request.state.user)

    return server


def _request(ctx: Context) -> Request:
    return ctx.request_context.request


@contextmanager
def _tool_errors(request: Request) -> Iterator[None]:
    """Turn the API's refusals (no such job, no PDF, ...) into tool errors, which the model reads."""
    try:
        yield
    except HTTPException as exc:
        logs.record_error(request, str(exc.detail))
        raise ToolError(str(exc.detail)) from None


def _new_link(request: Request, file: str, job_id: int | None = None, filename: str | None = None) -> LinkOut:
    """A link for the client of `request` to transfer `file` (UPLOAD or one of a job's files)."""
    if request.client is None:
        raise ToolError("the server cannot tell your address, to which links are bound")
    # Behind a reverse proxy, the address is the client's if FORWARDED_ALLOW_IPS names the proxy.
    links: LinkService = request.app.state.links
    secret, expires_at = links.create(request.state.user, request.client.host, file, job_id)
    upload = file == UPLOAD
    url = str(request.url_for("upload_through_link" if upload else "download_through_link", secret=secret))
    if upload:
        command = f"curl -sS --fail-with-body -F file=@PROJECT.zip {shlex.quote(url)}"
    else:
        command = f"curl -sS --fail-with-body -o {shlex.quote(filename or file)} {shlex.quote(url)}"
    return LinkOut(
        url=url,
        method="POST" if upload else "GET",
        expires_at=api.local_time(expires_at, request.app.state.settings.timezone),
        command=command,
    )
