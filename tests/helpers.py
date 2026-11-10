"""Test helpers shared by several test modules (fixtures are in conftest.py)."""

from __future__ import annotations

import io
import itertools
import json
import os
import socket
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

import httpx2
import libarchive
from fastapi import FastAPI
from fastapi.testclient import TestClient

TOKENS = {"alice": "alice-token-0123456789", "bob": "bob-token-9876543210"}

# Stands in for latexmk (inside the real sandbox). The main file's content picks the behaviour.
FAKE_LATEXMK = r"""
echo "fake-latexmk $*"
echo "env HOME=$HOME TMPDIR=$TMPDIR TEXMFVAR=$TEXMFVAR openout_any=$openout_any shell_escape=$shell_escape"
echo "limits memory=$(ulimit -v)"
for arg; do main=$arg; done
stem=$(basename "$main" .tex)
if grep -q DAEMON "$main"; then setsid sh -c 'echo $$ > daemon.pid; exec sleep 300' > /dev/null 2>&1 & sleep 0.3; fi
if grep -q DISKFILL "$main"; then
  mkdir hidden && dd if=/dev/zero of=hidden/fill bs=1M count=8 2> /dev/null && chmod 000 hidden && sleep 60
fi
if grep -q SLEEP "$main"; then sleep 60; fi
if grep -q SYMLINK "$main"; then ln -s "$(cat secret-path.txt)" "$stem.pdf"; exit 0; fi
if grep -q PARTIAL "$main"; then printf '%%PDF-1.4 partial\n' > "$stem.pdf"; fi
if grep -q FAIL "$main"; then echo "$main:3: Undefined control sequence."; exit 12; fi
if grep -q OOM "$main"; then echo "Killed"; echo "  lualatex: Command for 'lualatex' gave return code 137"; exit 12; fi
if grep -q READTEX "$main"; then cat "$(cat texdir.txt)/marker.txt" || exit 3; fi
printf '%%PDF-1.4 fake\n' > "$stem.pdf"
"""

MINIMAL_TEX = "\\documentclass{article}\n\\begin{document}\nHello.\n\\end{document}\n"

_mtimes = itertools.count(1)


def write_json(path: Path, document: object) -> Path:
    """Write a JSON file as the administrator would, with a new modification time each time, so that
    the server notices the change even when the size stays the same and the clock is coarse."""
    path.write_text(json.dumps(document), encoding="utf-8")
    stamp = next(_mtimes) * 1_000_000_000
    os.utime(path, ns=(stamp, stamp))
    return path


def write_archive(path: Path, files: dict[str, str | bytes], fmt: str = "zip") -> Path:
    """Create an archive of `fmt` holding `files` (member name -> content)."""
    data = {name: content.encode() if isinstance(content, str) else content for name, content in files.items()}
    if fmt == "zip":
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in data.items():
                archive.writestr(name, content)
    elif fmt == "7z":
        with libarchive.file_writer(str(path), "7zip") as archive:
            for name, content in data.items():
                archive.add_file_from_memory(name, len(content), content)
    else:
        mode = {"tar": "w", "tar.gz": "w:gz", "tar.bz2": "w:bz2", "tar.xz": "w:xz"}[fmt]
        with tarfile.open(path, mode) as archive:
            for name, content in data.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
    return path


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def server_environment(data: Path, port: int) -> dict[str, str]:
    # ALLOW_UNSANDBOXED: the tests that run the real server are not about the sandbox, so they also
    # run where it is incomplete. Without the variables of TeXdrop the test process may have, nor TZ,
    # which a test sets when it needs it.
    names = ("TZ", "USERS_FILE", "ENABLE_MCP", "LINK_EXPIRY_MINUTES")
    environment = {name: value for name, value in os.environ.items() if name not in names}
    return environment | {"DATA_DIR": str(data), "HOST": "127.0.0.1", "PORT": str(port), "ALLOW_UNSANDBOXED": "1"}


def start_server(data: Path, port: int, **env: str) -> subprocess.Popen[str]:
    """Run the real server, as python -m texdrop starts it, and wait until it answers."""
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


def run_cli(data: Path, *arguments: str, **env: str) -> subprocess.CompletedProcess[str]:
    """python -m texdrop with `arguments`, as the administrator runs it."""
    return subprocess.run(
        [sys.executable, "-m", "texdrop", *arguments],
        env=server_environment(data, free_port()) | env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def login(app: FastAPI, user: str = "alice") -> str:
    """Log a browser in as `user`, as the web page does: the value of its session cookie."""
    response = TestClient(app).post("/api/session", json={"token": TOKENS[user]})
    assert response.status_code == 200, response.text
    return response.cookies["texdrop_session"]


def browser_of(app: FastAPI, user: str = "alice", **options) -> TestClient:
    """A client of `app`, logged in as `user` through the web page."""
    return TestClient(app, cookies={"texdrop_session": login(app, user)}, **options)


def run_queue(app: FastAPI) -> None:
    """Process every queued job, oldest first, in the calling thread."""
    while job := app.state.service.claim_next():
        app.state.worker.process(job)


def submit(client: TestClient, archive: Path, **form: str):
    with archive.open("rb") as file:
        return client.post("/api/jobs", files={"file": (archive.name, file)}, data=form)
