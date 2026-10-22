"""Smoke tests of the Docker image: python -m pytest tests/test_docker.py -m docker

The image is built once per session; set TEST_IMAGE to test an existing image instead.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx2
import pytest
from helpers import write_archive

from texdrop import sandbox

pytestmark = [pytest.mark.docker, pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")]

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "docker-test-token-0123456789"
BIB = "@article{knuth84, author={Donald E. Knuth}, title={Literate Programming}, journal={Comput. J.}, year={1984}}\n"
# The server refuses to start where the sandbox is incomplete; on such a host the image is tested with the override.
FULL_SANDBOX = not sandbox.missing_protections(sandbox.landlock_abi(), sandbox.syscall_filter_works())
HARDENING = ("--cap-drop", "ALL", "--security-opt", "no-new-privileges")


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(scope="session")
def image() -> str:
    if image := os.environ.get("TEST_IMAGE"):
        return image
    subprocess.run(["docker", "build", "-t", "texdrop:test", str(ROOT)], check=True)
    return "texdrop:test"


class Server:
    """A container of the image, serving a data directory from the host."""

    def __init__(self, image: str, data: Path, *options: str, hardened: bool = True) -> None:
        self.container = docker(
            "run", "--detach", "--publish", "127.0.0.1::8000", "--volume", f"{data}:/data",
            "--env", "TZ=Europe/Athens", "--env", "COMPILE_TIMEOUT_MINUTES=5",
            *(() if FULL_SANDBOX else ("--env", "ALLOW_UNSANDBOXED=1")), *(HARDENING if hardened else ()),
            *options, image,
        )  # fmt: skip
        port = docker("port", self.container, "8000/tcp").splitlines()[0].rsplit(":", 1)[1]
        self.client = httpx2.Client(
            base_url=f"http://127.0.0.1:{port}", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=330
        )
        deadline = time.monotonic() + 60
        while True:
            try:
                self.client.get("/api/health").raise_for_status()
                return
            except httpx2.HTTPError:
                if time.monotonic() > deadline:
                    raise AssertionError(docker("logs", self.container)) from None
                time.sleep(0.5)

    def compile(self, files: dict[str, str], tmp_path: Path, **form: str) -> dict:
        archive = write_archive(tmp_path / f"project-{time.monotonic_ns()}.zip", files)
        with archive.open("rb") as file:
            response = self.client.post("/api/jobs", files={"file": (archive.name, file)}, data=form)
        response.raise_for_status()
        return self.client.get(f"/api/jobs/{response.json()['id']}", params={"wait": 300}).json()

    def log(self, job: dict) -> str:
        return self.client.get(f"/api/jobs/{job['id']}/log").text

    def stop(self) -> None:
        self.client.close()
        subprocess.run(["docker", "rm", "--force", self.container], check=False, capture_output=True)


@pytest.fixture
def data(tmp_path: Path, image: str):
    data = tmp_path / "data"
    data.mkdir()
    data.chmod(0o777)  # the container runs as uid 1001
    (data / "config.json").write_text(json.dumps({"users": {"tester": TOKEN}}))
    yield data
    # Let the host user remove what the container created.
    subprocess.run(["docker", "run", "--rm", "--volume", f"{data}:/data", "--entrypoint", "chmod", image, "-R", "a+rwX", "/data"])


@pytest.fixture
def server(image: str, data: Path):
    server = Server(image, data)
    yield server
    server.stop()


def test_runs_as_an_unprivileged_user(server):
    assert docker("exec", server.container, "id", "-u") == "1001"
    assert docker("inspect", "--format", "{{.Config.User}}", server.container) == "app"


def test_web_ui_and_auth(server):
    assert 'id="login-form"' in httpx2.get(f"{server.client.base_url}/").text
    assert httpx2.get(f"{server.client.base_url}/api/jobs").status_code == 401


def test_info_names_the_tex_live_release(server):
    info = server.client.get("/api/info").json()
    assert info["server"]["tex_live"].startswith("TeX Live 20")
    assert (info["server"]["timezone"], info["server"]["compile_timeout_minutes"]) == ("Europe/Athens", 5)


def test_bibtex_project(server, tmp_path):
    job = server.compile(
        {
            "paper/main.tex": "\\documentclass{article}\n\\begin{document}\nSee \\cite{knuth84}.\n"
            "\\bibliographystyle{plain}\n\\bibliography{refs}\n\\end{document}\n",
            "paper/refs.bib": BIB,
        },
        tmp_path,
    )
    assert job["status"] == "done", server.log(job)
    assert job["submitted_at"][-6:] in ("+02:00", "+03:00")  # TZ=Europe/Athens
    assert server.client.get(f"/api/jobs/{job['id']}/pdf").content.startswith(b"%PDF")
    assert "Running 'bibtex" in server.log(job)


@pytest.mark.parametrize("engine", ["pdflatex", "xelatex", "lualatex"])
def test_engines_with_biblatex_and_minted(server, tmp_path, engine):
    job = server.compile(
        {
            "main.tex": "\\documentclass{article}\n\\usepackage[backend=biber]{biblatex}\n\\addbibresource{refs.bib}\n"
            "\\usepackage{minted}\n\\begin{document}\nSee \\cite{knuth84}.\n"
            "\\begin{minted}{python}\nprint('hello')\n\\end{minted}\n\\printbibliography\n\\end{document}\n",
            "refs.bib": BIB,
        },
        tmp_path,
        engine=engine,
    )
    assert job["status"] == "done", server.log(job)
    assert "Running 'biber" in server.log(job)


def test_documents_cannot_read_the_token_file(server, tmp_path):
    attempts = {
        "pdflatex": "\\input{/data/config.json}",
        "lualatex": '\\directlua{local f = io.open("/data/config.json") texio.write_nl(f and f:read("a") or "BLOCKED")}',
    }
    for engine, attempt in attempts.items():
        job = server.compile(
            {"main.tex": f"\\documentclass{{article}}\n\\begin{{document}}\n{attempt}\n\\end{{document}}\n"},
            tmp_path,
            engine=engine,
        )
        log = server.log(job)
        assert TOKEN not in log
        assert TOKEN not in (job["error"] or "")
        if job["has_pdf"]:
            assert TOKEN.encode() not in server.client.get(f"/api/jobs/{job['id']}/pdf").content


def test_jobs_survive_a_new_container(image, data, tmp_path):
    first = Server(image, data)
    try:
        job = first.compile({"main.tex": "\\documentclass{article}\n\\begin{document}\nHi\n\\end{document}\n"}, tmp_path)
    finally:
        first.stop()
    second = Server(image, data)
    try:
        assert [listed["id"] for listed in second.client.get("/api/jobs").json()["jobs"]] == [job["id"]]
        assert second.client.get(f"/api/jobs/{job['id']}/pdf").content.startswith(b"%PDF")
    finally:
        second.stop()


MEMORY_HOG = (
    "\\documentclass{article}\n\\begin{document}\n"
    '\\directlua{local t = {} for i = 1, 400 do t[i] = string.rep("x", 50 * 1024 * 1024) .. i end}\n'
    "\\end{document}\n"
)
DISK_FILLER = (  # 20 MB files, about 100 MB a second
    "\\documentclass{article}\n\\begin{document}\n"
    '\\directlua{local chunk = string.rep("x", 20 * 1024 * 1024) for i = 1, 100 do'
    ' local f = io.open("fill" .. i .. ".bin", "wb") f:write(chunk) f:close()'
    " local t = os.clock() while os.clock() - t < 0.2 do end end}\n"
    "\\end{document}\n"
)
HELLO = "\\documentclass{article}\n\\begin{document}\nHi\n\\end{document}\n"


def test_limits_come_from_the_environment(image, data):
    server = Server(
        image, data,
        "--env", "COMPILE_MEMORY_MB=2048", "--env", "COMPILE_DISK_MB=1024", "--env", "MAX_USER_STORAGE_MB=500",
        "--env", "UNPACK_TIMEOUT_MINUTES=2", "--env", "UNPACK_MEMORY_MB=512",
    )  # fmt: skip
    try:
        info = server.client.get("/api/info").json()["server"]
        assert (info["compile_memory_mb"], info["compile_disk_mb"], info["max_user_storage_mb"]) == (2048, 1024, 500)
        assert (info["unpack_timeout_minutes"], info["unpack_memory_mb"]) == (2, 512)
    finally:
        server.stop()


def test_the_health_check_follows_port(image, data):
    container = docker(
        "run", "--detach", "--volume", f"{data}:/data", "--env", "PORT=9000", "--health-interval", "1s",
        *(() if FULL_SANDBOX else ("--env", "ALLOW_UNSANDBOXED=1")), image,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 60
        while (health := docker("inspect", "--format", "{{.State.Health.Status}}", container)) != "healthy":
            assert time.monotonic() < deadline, health
            time.sleep(0.5)
    finally:
        subprocess.run(["docker", "rm", "--force", container], check=False, capture_output=True)


def test_like_overleaf_ghostscript_but_no_java(server, tmp_path):
    assert subprocess.run(["docker", "exec", server.container, "sh", "-c", "command -v java"], check=False).returncode != 0
    eps = "%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 100 100\nnewpath 10 10 moveto 90 90 lineto stroke\nshowpage\n"
    job = server.compile(
        {"main.tex": "\\documentclass{article}\n\\usepackage{graphicx}\n\\begin{document}\n"
                     "\\includegraphics{fig.eps}\n\\end{document}\n", "fig.eps": eps},
        tmp_path,
    )  # fmt: skip
    assert job["status"] == "done", server.log(job)
    assert "fig-eps-converted-to.pdf" in server.log(job)  # by Ghostscript
    job = server.compile({"main.tex": "\\documentclass{article}\n\\usepackage{texosquery}\n\\begin{document}\n"
                                      "\\TeXOSQueryLocale{\\loc}[\\loc]\n\\end{document}\n"}, tmp_path)  # fmt: skip
    assert job["status"] == "done", server.log(job)  # texosquery gets nothing, as on Overleaf
    assert "java: not found" in server.log(job)


def test_running_out_of_memory_stops_the_compilation_not_the_server(image, data, tmp_path):
    server = Server(image, data, "--memory", "1g", "--memory-swap", "1g")  # and no limit of our own
    try:
        job = server.compile({"main.tex": MEMORY_HOG}, tmp_path, engine="lualatex")
        assert job["status"] == "failed"
        assert job["error"].startswith("lualatex was killed, most likely because the memory ran out."), job["error"]
        assert server.compile({"main.tex": HELLO}, tmp_path)["status"] == "done"
        assert docker("inspect", "--format", "{{.State.Running}} {{.RestartCount}}", server.container) == "true 0"
    finally:
        server.stop()


def test_disk_limit_stops_the_compilation(image, data, tmp_path):
    server = Server(image, data, "--env", "COMPILE_DISK_MB=100")
    try:
        start = time.monotonic()
        job = server.compile({"main.tex": DISK_FILLER}, tmp_path, engine="lualatex")
        assert time.monotonic() - start < 30
        assert job["status"] == "failed"
        assert job["error"] == "Compilation stopped: its files took more than 100 MB of disk."
        deadline = time.monotonic() + 10  # the work directory goes right after the job is marked failed
        while list((data / "tmp").iterdir()) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert list((data / "tmp").iterdir()) == []
    finally:
        server.stop()


SERVER_STATUS = """
import pathlib
for process in pathlib.Path("/proc").glob("[0-9]*"):
    try:
        if (process / "cmdline").read_bytes() == b"python\\0-m\\0texdrop\\0":
            print((process / "status").read_text())
    except OSError:
        pass
"""


@pytest.mark.parametrize("init", [[], ["--init"]], ids=["own-init", "docker-init"])
def test_a_plain_docker_run_needs_no_options_for_safety(image, data, tmp_path, init):
    # No --cap-drop, --security-opt or --init: the image itself provides what they would.
    server = Server(image, data, *init, hardened=False)
    try:
        status = docker("exec", server.container, "python", "-c", SERVER_STATUS)
        assert "NoNewPrivs:\t1" in status and "CapEff:\t0000000000000000" in status
        docker("exec", server.container, "sh", "-c", "sleep 1 & exit")  # leaves an orphan, reparented to process 1
        time.sleep(2)
        assert ") Z " not in docker("exec", server.container, "sh", "-c", "cat /proc/[0-9]*/stat")  # no zombie
        assert server.compile({"main.tex": HELLO}, tmp_path)["status"] == "done"
        start = time.monotonic()
        docker("stop", server.container)
        assert time.monotonic() - start < 8  # the server got docker stop's signal (not killed after 10 s)
        logs = subprocess.run(["docker", "logs", server.container], capture_output=True, text=True, check=True)
        assert "Application shutdown complete." in logs.stderr
        assert "tini" not in logs.stdout + logs.stderr  # no warning from tini as process 2 under --init
    finally:
        server.stop()


def test_the_log_file_outlives_the_container(image, data, tmp_path):
    for _ in range(2):
        server = Server(image, data)
        try:
            job = server.compile({"main.tex": HELLO}, tmp_path)
            assert server.client.get(f"/api/jobs/{job['id']}/pdf").status_code == 200
        finally:
            server.stop()
    log = (data / "logs" / "server.log").read_text()
    assert re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \+0[23]00 INFO    TeXdrop ", log)  # TZ=Europe/Athens
    assert log.count(" starting, data in /data\n") == 2
    if FULL_SANDBOX:
        assert log.count("INFO    every protection of the sandbox is active\n") == 2
    assert len(re.findall(r" tester POST /api/jobs 201 \d+ ms\n", log)) == 2
    assert len(re.findall(r" tester GET /api/jobs/\d+/pdf 200 \d+ ms\n", log)) == 2
    assert len(re.findall(r"job \d+ \(tester\): main\.tex with pdflatex, done in \d+\.\d s\n", log)) == 2
    assert "/api/health" not in log and "?wait=" not in log  # successful reads are not logged


def test_refuses_to_start_with_an_incomplete_sandbox(image):
    # As root, documents would run as root too: a protection missing on any kernel.
    command = ["docker", "run", "--rm", "--user", "0", image]
    refused = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    assert refused.returncode == 1, refused.stderr
    assert "ERROR   missing protection: running as root: " in refused.stderr
    assert "ERROR   cannot start: the sandbox lacks " in refused.stderr
    allowed = docker("run", "--detach", "--user", "0", "--env", "ALLOW_UNSANDBOXED=1", image)
    try:
        deadline = time.monotonic() + 60
        while "Application startup complete." not in (logs := subprocess.getoutput(f"docker logs {allowed}")):
            assert time.monotonic() < deadline, logs
            time.sleep(0.5)
        assert "WARNING reduced protection: running as root: " in logs
        assert "WARNING running with reduced protection, as ALLOW_UNSANDBOXED=1 allows" in logs
    finally:
        subprocess.run(["docker", "rm", "--force", "--volumes", allowed], check=False, capture_output=True)


def test_code_escaping_tex_has_no_network_and_leaves_nothing_behind(server):
    # As if a document had found a way to run commands: the sandbox, as the server starts it.
    udp = "import socket; socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'x', ('1.1.1.1', 53))"
    script = f'setsid sleep 300 > /dev/null 2>&1 & python -c "{udp}"'
    sandboxed = ["python", "-m", "texdrop.sandbox", "/tmp", "sh", "-c", script]
    result = subprocess.run(
        ["docker", "exec", "--workdir", "/tmp", server.container, *sandboxed],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert "PermissionError: [Errno 13] Permission denied" in result.stderr
    processes = docker("exec", server.container, "sh", "-c", "cat /proc/[0-9]*/stat")
    assert "(sleep)" not in processes
