import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient
from helpers import MINIMAL_TEX, TOKENS, run_queue, submit, write_archive

from texdrop import logs
from texdrop.__main__ import SHUTDOWN_SECONDS
from texdrop.archive import SUPPORTED

TIME = r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4}"
NOT_AN_INTEGER = "Input should be a valid integer, unable to parse string as an integer"


def access_lines(caplog) -> list[str]:
    """The access log's lines, with the duration replaced by N."""
    return [
        re.sub(r" \d+ ms", " N ms", record.getMessage()) for record in caplog.records if record.name == logs.__name__
    ]


def test_one_line_per_change_or_download(client, app, make_archive, caplog):
    caplog.set_level(logging.INFO)
    job = submit(client, make_archive({"main.tex": MINIMAL_TEX})).json()
    run_queue(app)
    # What the web page repeats every few seconds, and Docker's health check: not logged.
    reads = ["/", "/info", "/static/app.js", "/api/health", "/api/session", "/api/info", "/api/jobs"]
    for path in [*reads, f"/api/jobs/{job['id']}"]:
        assert client.get(path).status_code == 200
    assert client.get(f"/api/jobs/{job['id']}/pdf").status_code == 200
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 204
    assert access_lines(caplog) == [
        "testclient alice POST /api/jobs 201 N ms",
        f"testclient alice GET /api/jobs/{job['id']}/pdf 200 N ms",
        f"testclient alice DELETE /api/jobs/{job['id']} 204 N ms",
    ]


def test_failed_requests_say_why(app, client, caplog):
    caplog.set_level(logging.INFO)
    anonymous = TestClient(app)
    assert anonymous.post("/api/session", json={"token": "wrong-token-0123456789"}).status_code == 401
    assert anonymous.get("/api/jobs").status_code == 401
    assert client.get("/api/jobs/999").status_code == 404
    assert client.get("/api/jobs/0").status_code == 422
    assert client.get("/api/jobs", params={"page": "x"}).status_code == 422
    assert client.post("/api/inspect", files={"file": ("a.zip", b"hello")}).status_code == 400
    assert (
        client.post("/api/jobs", content=iter([b"x"]), headers={"Content-Type": "application/zip"}).status_code == 411
    )
    assert access_lines(caplog) == [
        "testclient - POST /api/session 401 N ms: invalid token",
        "testclient - GET /api/jobs 401 N ms: missing or invalid token",
        "testclient alice GET /api/jobs/999 404 N ms: no such job",
        "testclient alice GET /api/jobs/0 422 N ms: path.job_id: Input should be greater than or equal to 1",
        f"testclient alice GET /api/jobs?page=x 422 N ms: query.page: {NOT_AN_INTEGER}",
        f"testclient alice POST /api/inspect 400 N ms: unsupported file type: upload a {SUPPORTED} archive",
        "testclient - POST /api/jobs 411 N ms: Content-Length required",
    ]


def test_unexpected_errors_are_logged_as_errors(app, client, monkeypatch, caplog):
    monkeypatch.setattr(app.state.service, "list", lambda *args, **kwargs: 1 / 0)
    failing = TestClient(app, headers=client.headers, raise_server_exceptions=False)
    assert failing.get("/api/jobs").status_code == 500
    [record] = [record for record in caplog.records if record.name == logs.__name__]
    assert record.levelno == logging.ERROR
    assert re.fullmatch(r"testclient alice GET /api/jobs 500 \d+ ms", record.getMessage())


def test_requests_cannot_fake_log_lines(client, caplog):
    caplog.set_level(logging.INFO)
    assert client.get("/api/jobs/1%0A2026-10-22 12:00:00 +0000 INFO    forged").status_code == 422
    quoted = "/api/jobs/1%0A2026-10-22%2012%3A00%3A00%20%2B0000%20INFO%20%20%20%20forged"
    assert access_lines(caplog) == [f"testclient alice GET {quoted} 422 N ms: path.job_id: {NOT_AN_INTEGER}"]


def test_one_line_escapes_control_characters():
    assert logs.one_line("a\nb\r\tc\x85d\u2028e\x1bf é") == "a\\nb\\r\\tc\\x85d\\u2028e\\x1bf é"


def test_one_line_per_finished_job(client, app, make_archive, caplog):
    caplog.set_level(logging.INFO)
    done = submit(client, make_archive({"main.tex": MINIMAL_TEX})).json()
    failed = submit(client, make_archive({"main.tex": "FAIL"})).json()
    run_queue(app)
    lines = [record.getMessage() for record in caplog.records if record.name == "texdrop.worker"]
    assert len(lines) == 2
    assert re.fullmatch(rf"job {done['id']} \(alice\): main\.tex with pdflatex, done in \d+\.\d s", lines[0])
    assert re.fullmatch(
        rf"job {failed['id']} \(alice\): main\.tex with pdflatex, failed after \d+\.\d s: "
        r"\./main\.tex:3: Undefined control sequence\.",
        lines[1],
    )


@pytest.fixture
def root_logger():
    root = logging.getLogger()
    before = list(root.handlers)
    yield root
    for handler in root.handlers[:]:
        if handler not in before:
            root.removeHandler(handler)
            handler.close()


def test_the_file_is_rotated(tmp_path, root_logger):
    logs.log_to_file(tmp_path / "logs", max_bytes=2000, backups=2)
    for number in range(100):
        logging.getLogger("texdrop.test").warning("line %d %s", number, "x" * 50)
    files = sorted((tmp_path / "logs").iterdir())
    assert [file.name for file in files] == ["server.log", "server.log.1", "server.log.2"]
    assert all(file.stat().st_size <= 2000 for file in files)
    newest = files[0].read_text().splitlines()
    assert re.fullmatch(rf"{TIME} WARNING line 99 x{{50}}", newest[-1])


def test_without_a_writable_directory_the_console_still_logs(tmp_path, root_logger, caplog):
    (tmp_path / "logs").write_text("a file where the directory should be")
    handlers = list(root_logger.handlers)
    logs.log_to_file(tmp_path / "logs")
    assert root_logger.handlers == handlers
    assert "no log file: cannot write" in caplog.text


# The real server, as python -m texdrop starts it.


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def server_environment(data: Path, port: int) -> dict[str, str]:
    # ALLOW_UNSANDBOXED: these tests are about logging, so they also run where the sandbox is incomplete.
    # Without TZ, which a test sets when it needs it.
    environment = {name: value for name, value in os.environ.items() if name != "TZ"}
    return environment | {"DATA_DIR": str(data), "HOST": "127.0.0.1", "PORT": str(port), "ALLOW_UNSANDBOXED": "1"}


def start_server(data: Path, port: int, **env: str) -> subprocess.Popen[str]:
    """Run the server and wait until it answers."""
    server = subprocess.Popen(
        [sys.executable, "-m", "texdrop"],
        env=server_environment(data, port) | env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 60
    while True:
        try:
            httpx2.get(f"http://127.0.0.1:{port}/api/health").raise_for_status()
            return server
        except httpx2.HTTPError:
            if time.monotonic() > deadline or server.poll() is not None:
                server.kill()
                raise AssertionError(f"the server did not start:\n{server.communicate()[0]}") from None
            time.sleep(0.2)


@pytest.fixture
def data(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"users": TOKENS}))
    return data


def test_the_server_logs_to_the_console_and_its_data_directory(data):
    port = free_port()
    server = start_server(data, port)
    try:
        url = f"http://127.0.0.1:{port}"
        assert httpx2.post(f"{url}/api/session", json={"token": "wrong-token-0123456789"}).status_code == 401
        # Neither the server nor what it starts can gain privileges, as with no-new-privileges.
        assert "NoNewPrivs:\t1" in Path(f"/proc/{server.pid}/status").read_text()
    finally:
        server.send_signal(signal.SIGTERM)
        console = server.communicate(timeout=30)[0]
    assert server.returncode in (0, -signal.SIGTERM)  # uvicorn raises the signal again once it has shut down
    log = (data / "logs" / "server.log").read_text()
    assert log in console  # the same lines in both
    lines = log.splitlines()
    assert re.fullmatch(rf"{TIME} INFO    TeXdrop [\d.]+ starting, data in {re.escape(str(data))}", lines[0])
    assert any(
        line.endswith(f"INFO    Uvicorn running on http://127.0.0.1:{port} (Press CTRL+C to quit)") for line in lines
    )
    assert any(
        re.fullmatch(rf"{TIME} INFO    127\.0\.0\.1 - POST /api/session 401 \d+ ms: invalid token", line)
        for line in lines
    )
    assert "/api/health" not in log
    assert lines[-1].endswith(f"INFO    Finished server process [{server.pid}]")


@pytest.mark.skipif(shutil.which("latexmk") is None, reason="latexmk is not installed")
def test_shutdown_does_not_wait_for_clients_waiting_on_a_job(data, tmp_path):
    port = free_port()
    server = start_server(data, port, COMPILE_TIMEOUT_MINUTES="1")  # should the test fail, TeX stops anyway
    headers = {"Authorization": f"Bearer {TOKENS['alice']}"}
    try:
        with httpx2.Client(base_url=f"http://127.0.0.1:{port}", headers=headers, timeout=60) as client:
            endless = write_archive(tmp_path / "endless.zip", {"main.tex": "\\def\\x{\\x}\\x"})  # runs until stopped
            with endless.open("rb") as file:
                job = client.post("/api/jobs", files={"file": (endless.name, file)}).json()
            with ThreadPoolExecutor(1) as executor:
                waiting = executor.submit(client.get, f"/api/jobs/{job['id']}", params={"wait": 600})
                while (status := client.get(f"/api/jobs/{job['id']}").json()["status"]) == "queued":
                    time.sleep(0.2)
                assert status == "processing"
                time.sleep(1)  # the waiting request has reached the server
                start = time.monotonic()
                server.send_signal(signal.SIGTERM)
                console = server.communicate(timeout=120)[0]
                assert time.monotonic() - start < SHUTDOWN_SECONDS + 3  # before docker stop would kill it (10 s)
                with pytest.raises(httpx2.HTTPError):
                    waiting.result()  # cut off
    finally:
        if server.poll() is None:  # failed early: the server also stops the compilation
            server.send_signal(signal.SIGTERM)
            server.communicate(timeout=120)
    assert "Application shutdown complete." in console


def test_log_times_are_in_the_tz_time_zone(data):
    # UTC without TZ, as the dates the API shows, whatever the system's time zone.
    assert re.match(r"\S+ \S+ \+0000 ERROR ", failed_start(data, COMPILE_TIMEOUT_MINUTES="soon"))
    athens = failed_start(data, TZ="Europe/Athens", COMPILE_TIMEOUT_MINUTES="soon")
    assert re.match(r"\S+ \S+ \+0[23]00 ERROR ", athens)


def test_startup_errors_reach_the_log_file(data):
    (data / "config.json").write_text('{"users": {"Bad Name": "0123456789abcdef"}}')
    assert failed_start(data).endswith(
        f"ERROR   cannot start: {data / 'config.json'}: invalid user name 'Bad Name': use 1-32 of a-z, 0-9, '_' and '-'"
    )
    assert failed_start(data, COMPILE_TIMEOUT_MINUTES="soon").endswith(
        "ERROR   cannot start: COMPILE_TIMEOUT_MINUTES must be an integer, got 'soon'"
    )


def failed_start(data: Path, **env: str) -> str:
    """Run the server, which must stop at once; return its last line, the same on the console and in the file."""
    result = subprocess.run(
        [sys.executable, "-m", "texdrop"],
        env=server_environment(data, free_port()) | env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    last = result.stderr.splitlines()[-1]
    assert (data / "logs" / "server.log").read_text().splitlines()[-1] == last
    return last
