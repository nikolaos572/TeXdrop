"""The MCP server and its transfer links, over HTTP as agents use them."""

import asyncio
import dataclasses
import itertools
import json
import logging
import os
import random
import re
import shutil
import signal
import time
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient
from helpers import MINIMAL_TEX, TOKENS, free_port, start_server, submit, write_archive
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client

from texdrop import __version__, logs
from texdrop.app import create_app
from texdrop.links import REFUSAL

ALICE = {"Authorization": f"Bearer {TOKENS['alice']}"}
TOOLS = {"server_info", "upload_link", "list_jobs", "get_job", "download_link", "delete_job", "delete_all_jobs"}


class ToolFailed(Exception):
    """A tool call answered with an error, which the model reads."""


class Agent:
    """Alice's agent: an MCP client speaking JSON-RPC over HTTP, which runs the transfers the tools
    describe with another HTTP client, without the token (as curl would)."""

    def __init__(self, mcp: TestClient, curl: TestClient) -> None:
        self.mcp = mcp
        self.curl = curl
        self._ids = itertools.count(1)

    def rpc(self, method: str, params: dict | None = None) -> dict:
        response = self.mcp.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or {}},
        )
        assert response.status_code == 200, response.text
        return response.json()

    def call(self, tool: str, **arguments) -> dict:
        result = self.rpc("tools/call", {"name": tool, "arguments": arguments})["result"]
        if result["isError"]:
            raise ToolFailed(result["content"][0]["text"])
        return result["structuredContent"]

    def upload(self, link: dict, archive: Path, **form: str) -> httpx2.Response:
        with archive.open("rb") as file:
            return self.curl.post(link["url"], files={"file": (archive.name, file)}, data=form)

    def compile(self, archive: Path, **form: str) -> dict:
        """Upload through a new link and wait for the job to finish."""
        response = self.upload(self.call("upload_link"), archive, **form)
        assert response.status_code == 201, response.text
        return self.call("get_job", job_id=response.json()["id"])


@pytest.fixture
def mcp_settings(settings):
    return dataclasses.replace(settings, enable_mcp=True)


@pytest.fixture
def mcp_app(mcp_settings, fake_compiler):
    return create_app(mcp_settings, fake_compiler)


@pytest.fixture
def agent(mcp_app):
    """With the app running: its worker compiles the jobs (with the fake latexmk)."""
    with TestClient(mcp_app, headers=ALICE) as client:
        yield Agent(client, TestClient(mcp_app))


def access_lines(caplog) -> list[str]:
    """The access log's lines, with the duration replaced by N."""
    return [
        re.sub(r" \d+ ms", " N ms", record.getMessage()) for record in caplog.records if record.name == logs.__name__
    ]


# --- the switch and the endpoint ---


def test_mcp_is_off_by_default(app, client):
    assert client.post("/mcp", json={}).status_code == 404
    assert client.get("/api/links/abc").status_code == 404
    assert client.get("/api/info").json()["server"]["mcp_path"] is None
    assert not any(path.startswith("/api/links") for path in client.get("/openapi.json").json()["paths"])


def test_info_shows_the_endpoint_and_the_link_lifetime(agent):
    server = agent.mcp.get("/api/info").json()["server"]
    assert (server["mcp_path"], server["link_expiry_minutes"]) == ("/mcp", 60)
    assert "/api/links/{secret}" in agent.mcp.get("/openapi.json").json()["paths"]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token-0123456789"},
        {"Authorization": f"Basic {TOKENS['alice']}"},
        # The web page's cookie is not accepted, even with the header that marks the web page's requests.
        {"Cookie": f"latex_token={TOKENS['alice']}", "X-Requested-With": "fetch"},
    ],
)
def test_the_endpoint_needs_the_token_in_the_authorization_header(agent, headers):
    response = agent.curl.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT"])
def test_the_endpoint_answers_only_post(agent, method):
    # No event stream (GET) nor sessions (DELETE): the protocol lets the server answer 405.
    assert agent.mcp.request(method, "/mcp").status_code == 405


def test_the_agent_finds_the_tools_and_how_to_use_them(agent):
    hello = agent.rpc(
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
    )["result"]
    assert (hello["serverInfo"]["name"], hello["serverInfo"]["version"]) == ("TeXdrop", __version__)
    for step in ("upload_link", "get_job", "download_link", "60 minutes"):
        assert step in hello["instructions"]

    tools = {tool["name"]: tool for tool in agent.rpc("tools/list")["result"]["tools"]}
    assert set(tools) == TOOLS
    assert all(tool["description"] and tool["title"] for tool in tools.values())
    for name in ("server_info", "list_jobs", "get_job"):
        assert tools[name]["annotations"]["readOnlyHint"] is True
    for name in ("delete_job", "delete_all_jobs"):  # clients ask before running these
        assert tools[name]["annotations"]["destructiveHint"] is True
    for name in ("upload_link", "download_link"):
        assert tools[name]["annotations"]["destructiveHint"] is False
    assert tools["download_link"]["inputSchema"]["required"] == ["job_id", "file"]
    assert tools["get_job"]["inputSchema"]["properties"]["wait_seconds"]["maximum"] == 50


# --- compiling through links ---


def test_compile_and_download_through_links(agent, make_archive):
    archive = make_archive({"paper/main.tex": MINIMAL_TEX, "paper/refs.bib": "@book{x, title={X}}"})
    link = agent.call("upload_link")
    assert link["url"].startswith("http://testserver/api/links/")
    assert link["method"] == "POST"
    assert link["command"] == f"curl -sS --fail-with-body -F file=@PROJECT.zip {link['url']}"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+0[23]:00", link["expires_at"])  # in Europe/Athens

    uploaded = agent.upload(link, archive, engine="lualatex")  # without the token: the link stands in for it
    assert uploaded.status_code == 201
    job = uploaded.json()
    assert (job["filename"], job["main"], job["engine"]) == ("archive-0.zip", "main.tex", "lualatex")
    assert agent.upload(link, archive).status_code == 404  # used up

    done = agent.call("get_job", job_id=job["id"])  # waits until it is done
    assert (done["status"], done["has_pdf"], done["error"]) == ("done", True, None)
    assert done == agent.mcp.get(f"/api/jobs/{job['id']}").json()

    pdf = agent.call("download_link", job_id=job["id"], file="pdf")
    assert pdf["method"] == "GET"
    assert pdf["command"] == f"curl -sS --fail-with-body -o main.pdf {pdf['url']}"
    response = agent.curl.get(pdf["url"])
    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'inline; filename="main.pdf"'
    assert response.content.startswith(b"%PDF")
    again = agent.curl.get(pdf["url"])
    assert (again.status_code, again.json()) == (404, {"detail": REFUSAL})

    log = agent.curl.get(agent.call("download_link", job_id=job["id"], file="log")["url"])
    assert "fake-latexmk" in log.text
    original = agent.call("download_link", job_id=job["id"], file="input")
    assert original["command"].startswith("curl -sS --fail-with-body -o archive-0.zip ")
    assert agent.curl.get(original["url"]).content == archive.read_bytes()


def test_a_refused_upload_leaves_the_link_valid(agent, make_archive):
    archive = make_archive({"a.tex": MINIMAL_TEX, "b.tex": MINIMAL_TEX})
    link = agent.call("upload_link")
    refused = agent.upload(link, archive)
    assert refused.status_code == 400
    assert "several .tex files found" in refused.json()["detail"]
    assert agent.upload(link, archive, engine="nonsense").status_code == 422  # checked as by POST /api/jobs
    assert agent.upload(link, archive, main="b.tex").status_code == 201
    assert agent.upload(link, archive, main="b.tex").status_code == 404


def test_failed_jobs_report_their_errors(agent, make_archive):
    failed = agent.compile(make_archive({"main.tex": "PARTIAL FAIL"}))
    assert (failed["status"], failed["has_pdf"]) == ("failed", True)
    assert failed["error"] == "./main.tex:3: Undefined control sequence."
    without_pdf = agent.compile(make_archive({"main.tex": "FAIL"}))
    with pytest.raises(ToolFailed, match="this job has no PDF"):
        agent.call("download_link", job_id=without_pdf["id"], file="pdf")


def test_links_work_only_from_the_address_that_asked_for_them(agent, mcp_app, make_archive, caplog):
    caplog.set_level(logging.INFO)
    archive = make_archive({"main.tex": MINIMAL_TEX})
    upload = agent.call("upload_link")
    elsewhere = TestClient(mcp_app, client=("10.0.0.9", 50000))
    with archive.open("rb") as file:
        response = elsewhere.post(upload["url"], files={"file": (archive.name, file)})
    assert (response.status_code, response.json()) == (404, {"detail": REFUSAL})
    job = agent.upload(upload, archive).json()  # still valid where it belongs
    agent.call("get_job", job_id=job["id"])
    download = agent.call("download_link", job_id=job["id"], file="pdf")
    assert elsewhere.get(download["url"]).status_code == 404
    assert agent.curl.get(download["url"]).status_code == 200
    lines = access_lines(caplog)
    for method in ("POST", "GET"):
        assert f"10.0.0.9 - {method} /api/links/*** 404 N ms: link for testclient used from 10.0.0.9" in lines


def test_links_expire(mcp_settings, fake_compiler, make_archive, caplog):
    caplog.set_level(logging.INFO)
    app = create_app(dataclasses.replace(mcp_settings, link_expiry_seconds=1), fake_compiler)
    with TestClient(app, headers=ALICE) as client:
        agent = Agent(client, TestClient(app))
        link = agent.call("upload_link")
        time.sleep(1.1)
        assert agent.upload(link, make_archive({"main.tex": MINIMAL_TEX})).status_code == 404
    assert access_lines(caplog)[-1].endswith(": expired link")


def test_a_refused_link_is_answered_before_the_upload_is_read(agent):
    # Without the file the form is invalid (422), but the link is checked first.
    response = agent.curl.post("/api/links/no-such-link", data={"main": "main.tex"})
    assert (response.status_code, response.json()) == (404, {"detail": REFUSAL})


def test_wrong_kind_of_link(agent, make_archive):
    job = agent.compile(make_archive({"main.tex": MINIMAL_TEX}))
    upload = agent.call("upload_link")
    download = agent.call("download_link", job_id=job["id"], file="pdf")
    assert agent.curl.get(upload["url"]).status_code == 404
    assert agent.upload(download, make_archive({"main.tex": MINIMAL_TEX})).status_code == 404
    assert agent.curl.get(download["url"]).status_code == 200  # neither attempt used it


def test_links_of_removed_users_stop_working(agent, mcp_settings, make_archive, caplog):
    caplog.set_level(logging.INFO)
    link = agent.call("upload_link")
    config = mcp_settings.data_dir / "config.json"
    config.write_text(json.dumps({"users": {"bob": TOKENS["bob"]}}))
    os.utime(config, ns=(1, 1))  # noticed even on coarse clocks
    assert agent.upload(link, make_archive({"main.tex": MINIMAL_TEX})).status_code == 404
    assert access_lines(caplog)[-1].endswith(": link of 'alice', who is no longer a user")


def test_link_secrets_never_reach_the_log(agent, make_archive, caplog):
    caplog.set_level(logging.INFO)
    job = agent.compile(make_archive({"main.tex": MINIMAL_TEX}))
    link = agent.call("download_link", job_id=job["id"], file="pdf")
    agent.curl.get(link["url"])
    agent.curl.get(link["url"])
    secret = link["url"].rsplit("/", 1)[1]
    server_side = [record.getMessage() for record in caplog.records if not record.name.startswith("httpx")]
    assert not any(secret in message for message in server_side)
    assert [line for line in access_lines(caplog) if "/api/links/" in line] == [
        "testclient alice POST /api/links/*** 201 N ms",
        "testclient alice GET /api/links/*** 200 N ms",
        "testclient - GET /api/links/*** 404 N ms: unknown link",
    ]


# --- the other tools, which do what their API endpoints do ---


def test_server_info_is_the_apis(agent):
    assert agent.call("server_info") == agent.mcp.get("/api/info").json()


def test_jobs_are_shared_with_the_api_and_the_web_page(agent, mcp_app, make_archive):
    through_api = submit(agent.mcp, make_archive({"main.tex": MINIMAL_TEX})).json()
    through_link = agent.upload(agent.call("upload_link"), make_archive({"main.tex": MINIMAL_TEX})).json()
    listed = agent.call("list_jobs")
    assert [job["id"] for job in listed["jobs"]] == [through_link["id"], through_api["id"]]
    assert listed == agent.mcp.get("/api/jobs").json()
    browser = TestClient(mcp_app, cookies={"latex_token": TOKENS["alice"]})
    assert browser.get("/api/jobs").json()["total"] == 2


def test_list_jobs_pages(agent, make_archive):
    ids = [submit(agent.mcp, make_archive({"main.tex": MINIMAL_TEX})).json()["id"] for _ in range(5)]
    page = agent.call("list_jobs", page=2, per_page=2)
    assert ([job["id"] for job in page["jobs"]], page["total"]) == (ids[2:0:-1], 5)
    assert agent.call("list_jobs", page=9)["jobs"] == []


def test_get_job_returns_after_wait_seconds(agent, make_archive):
    job = submit(agent.mcp, make_archive({"main.tex": "SLEEP"})).json()  # compiles for a minute
    start = time.monotonic()
    running = agent.call("get_job", job_id=job["id"], wait_seconds=1)
    assert running["status"] in ("queued", "processing")
    assert 1 <= time.monotonic() - start < 10
    with pytest.raises(ToolFailed, match="the log is available once the job has finished"):
        agent.call("download_link", job_id=job["id"], file="log")
    assert agent.call("delete_job", job_id=job["id"]) == {"deleted": 1}  # also stops the compilation


def test_delete_tools(agent, mcp_app, make_archive):
    first, *_ = (submit(agent.mcp, make_archive({"main.tex": MINIMAL_TEX})).json() for _ in range(3))
    bob = TestClient(mcp_app, headers={"Authorization": f"Bearer {TOKENS['bob']}"})
    submit(bob, make_archive({"main.tex": MINIMAL_TEX}))
    assert agent.call("delete_job", job_id=first["id"]) == {"deleted": 1}
    with pytest.raises(ToolFailed, match="no such job"):
        agent.call("delete_job", job_id=first["id"])
    assert agent.call("delete_all_jobs") == {"deleted": 2}
    assert agent.mcp.get("/api/jobs").json()["total"] == 0
    assert bob.get("/api/jobs").json()["total"] == 1  # only the agent's user's jobs


def test_tools_cannot_reach_other_users_jobs(agent, mcp_app, make_archive):
    bob = TestClient(mcp_app, headers={"Authorization": f"Bearer {TOKENS['bob']}"})
    job = submit(bob, make_archive({"main.tex": MINIMAL_TEX})).json()
    bob.get(f"/api/jobs/{job['id']}", params={"wait": 10})
    for tool, arguments in [
        ("get_job", {"job_id": job["id"], "wait_seconds": 0}),
        ("download_link", {"job_id": job["id"], "file": "pdf"}),
        ("delete_job", {"job_id": job["id"]}),
    ]:
        with pytest.raises(ToolFailed, match="no such job"):
            agent.call(tool, **arguments)


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_job", {"job_id": 0}),
        ("get_job", {"job_id": 2**63}),
        ("get_job", {"job_id": "1; DROP TABLE jobs"}),
        ("get_job", {"job_id": 1, "wait_seconds": 51}),
        ("get_job", {"job_id": 1, "wait_seconds": -1}),
        ("download_link", {"job_id": 1, "file": "config.json"}),
        ("download_link", {"job_id": 1}),
        ("list_jobs", {"per_page": 101}),
        ("list_jobs", {"page": 0}),
    ],
)
def test_tool_arguments_are_validated(agent, tool, arguments):
    with pytest.raises(ToolFailed, match="validation error"):
        agent.call(tool, **arguments)


def test_tool_errors_reach_the_access_log(agent, caplog):
    caplog.set_level(logging.INFO)
    with pytest.raises(ToolFailed, match="no such job"):
        agent.call("get_job", job_id=999)
    assert access_lines(caplog)[-1] == "testclient alice POST /mcp 200 N ms: no such job"


# --- the real server, with the official MCP client ---


@pytest.fixture
def server(data):
    """The real server with ENABLE_MCP=1, and its base URL."""
    port = free_port()
    process = start_server(data, port, ENABLE_MCP="1")
    yield f"http://127.0.0.1:{port}"
    process.send_signal(signal.SIGTERM)
    process.communicate(timeout=60)


async def official_client(url: str, steps, *, mode: str = "legacy", headers: dict[str, str] | None = None):
    """Connect as Alice with the SDK's client (as Claude Code and other agents do), and run `steps`."""
    async with (
        httpx2.AsyncClient(headers=ALICE | (headers or {}), timeout=60) as http,
        Client(streamable_http_client(f"{url}/mcp", http_client=http), mode=mode) as client,
    ):
        return await steps(client)


@pytest.mark.skipif(shutil.which("latexmk") is None, reason="latexmk is not installed")
@pytest.mark.parametrize("mode", ["legacy", "auto"])  # the protocol's versions with and without a handshake
def test_compile_with_the_official_client(server, tmp_path, mode):
    archive = write_archive(tmp_path / "paper.zip", {"main.tex": MINIMAL_TEX})

    async def steps(client):
        assert {tool.name for tool in (await client.list_tools()).tools} == TOOLS
        link = (await client.call_tool("upload_link", {})).structured_content
        with archive.open("rb") as file:
            job = httpx2.post(link["url"], files={"file": (archive.name, file)}).json()
        result = await client.call_tool("get_job", {"job_id": job["id"]})
        while result.structured_content["status"] in ("queued", "processing"):
            result = await client.call_tool("get_job", {"job_id": job["id"]})
        assert result.structured_content["status"] == "done", result.structured_content["error"]
        return (await client.call_tool("download_link", {"job_id": job["id"], "file": "pdf"})).structured_content

    link = asyncio.run(official_client(server, steps, mode=mode))
    assert httpx2.get(link["url"]).content.startswith(b"%PDF")
    assert httpx2.get(link["url"]).status_code == 404


def test_a_download_cut_off_leaves_the_link_valid(server, tmp_path):
    # Larger than what the network buffers hold, so that the server is still sending when the client leaves.
    noise = random.Random(0).randbytes(30 * 1024**2)
    archive = write_archive(tmp_path / "big.tar", {"main.tex": MINIMAL_TEX, "noise.bin": noise}, "tar")

    async def steps(client):
        link = (await client.call_tool("upload_link", {})).structured_content
        with archive.open("rb") as file:
            job = httpx2.post(link["url"], files={"file": (archive.name, file)}, timeout=60).json()
        return (await client.call_tool("download_link", {"job_id": job["id"], "file": "input"})).structured_content

    link = asyncio.run(official_client(server, steps))
    with httpx2.stream("GET", link["url"]) as response:
        assert response.status_code == 200
        next(response.iter_bytes(64 * 1024))  # then hang up
    assert httpx2.get(link["url"], timeout=60).content == archive.read_bytes()
    deadline = time.monotonic() + 10  # the server uses the link up just after the last byte
    while httpx2.get(link["url"]).status_code != 404:
        assert time.monotonic() < deadline


def test_behind_a_reverse_proxy_links_follow_the_forwarded_address(server, tmp_path):
    # FORWARDED_ALLOW_IPS defaults to 127.0.0.1, so the server takes this test for a reverse proxy
    # and the client's address from X-Forwarded-For, as it would from nginx.
    archive = write_archive(tmp_path / "paper.zip", {"main.tex": MINIMAL_TEX})

    async def make_link(client):
        return (await client.call_tool("upload_link", {})).structured_content

    def upload(link: dict, address: str) -> int:
        with archive.open("rb") as file:
            files = {"file": (archive.name, file)}
            return httpx2.post(link["url"], files=files, headers={"X-Forwarded-For": address}).status_code

    link = asyncio.run(official_client(server, make_link, headers={"X-Forwarded-For": "10.0.0.5"}))
    assert upload(link, "10.0.0.6") == 404
    assert upload(link, "10.0.0.5") == 201
