import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from helpers import process_exists

from texdrop import sandbox

pytestmark = pytest.mark.skipif(not sandbox.landlock_abi(), reason="Landlock is not available on this kernel")
syscall_filter = pytest.mark.skipif(not sandbox.syscall_filter_supported(), reason="no system-call filter here")
# Python for commands in the sandbox: the base interpreter, as a virtual environment is unreadable there.
PYTHON = os.path.realpath(sys.executable)


def command(workdir, script, *options):
    return [sys.executable, "-I", sandbox.__file__, *options, str(workdir), "sh", "-c", script]


def run(workdir, script, *options):
    return subprocess.run(
        command(workdir, script, *options), cwd=workdir, capture_output=True, text=True, timeout=30, check=False
    )


def read_pid(path: Path) -> int:
    deadline = time.monotonic() + 10
    while not path.is_file() or not path.read_text().strip():
        assert time.monotonic() < deadline, f"{path.name} was not written"
        time.sleep(0.05)
    return int(path.read_text())


@pytest.fixture
def workdir(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "secret.txt").write_text("TOP-SECRET")
    return tmp_path / "work"


def test_work_dir_is_readable_and_writable(workdir):
    result = run(workdir, "echo hi > a.txt && mkdir d && mv a.txt d/ && cat d/a.txt && rm -r d")
    assert (result.returncode, result.stdout) == (0, "hi\n")


def test_system_files_are_readable_and_programs_run(workdir):
    result = run(workdir, "cat /etc/hostname > /dev/null && ls /usr/bin > /dev/null && echo ok")
    assert result.stdout == "ok\n", result.stderr


def test_files_outside_are_not_readable(workdir):
    result = run(workdir, f"cat {workdir.parent}/secret.txt")
    assert result.returncode != 0
    assert "TOP-SECRET" not in result.stdout
    assert "Permission denied" in result.stderr


def test_nothing_outside_is_writable(workdir):
    result = run(workdir, f"echo x > {workdir.parent}/new.txt; rm {workdir.parent}/secret.txt; echo x > /tmp/escape-$$")
    assert not (workdir.parent / "new.txt").exists()
    assert (workdir.parent / "secret.txt").exists()
    assert result.stderr.count("Permission denied") >= 2


def test_symlink_to_outside_cannot_be_followed(workdir):
    result = run(workdir, f"ln -s {workdir.parent}/secret.txt link && cat link")
    assert "TOP-SECRET" not in result.stdout


@pytest.mark.skipif(sandbox.landlock_abi() < 4, reason="network rules need Landlock ABI 4")
def test_tcp_connections_are_blocked(workdir):
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        script = f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=5); print('connected')"
        result = run(workdir, f"{PYTHON} -I -c \"{script}\"")
    assert "connected" not in result.stdout
    assert "PermissionError: [Errno 13] Permission denied" in result.stderr.splitlines()[-1]


NETWORK_PROBE = """
import socket
for family, kind in [("AF_INET", "SOCK_DGRAM"), ("AF_INET6", "SOCK_DGRAM"), ("AF_INET", "SOCK_STREAM"),
                     ("AF_NETLINK", "SOCK_RAW"), ("AF_UNIX", "SOCK_DGRAM")]:
    try:
        with socket.socket(getattr(socket, family), getattr(socket, kind)) as sock:
            if kind == "SOCK_DGRAM" and family == "AF_INET":
                sock.sendto(b"leak", ("127.0.0.1", {port}))
        print(family, kind, "allowed")
    except PermissionError:
        print(family, kind, "denied")
"""


@syscall_filter
def test_udp_and_every_other_network_socket_are_blocked(workdir):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(0.5)
        (workdir / "probe.py").write_text(NETWORK_PROBE.format(port=receiver.getsockname()[1]))
        result = run(workdir, f"{PYTHON} -I probe.py")
        with pytest.raises(TimeoutError):
            receiver.recv(100)  # nothing arrived
    assert result.stdout.splitlines() == [
        "AF_INET SOCK_DGRAM denied",
        "AF_INET6 SOCK_DGRAM denied",
        "AF_INET SOCK_STREAM denied",
        "AF_NETLINK SOCK_RAW denied",
        "AF_UNIX SOCK_DGRAM allowed",  # local only, and nothing outside the sandbox to talk to
    ], result.stderr


def test_exit_code_is_the_commands(workdir):
    assert run(workdir, "exit 7").returncode == 7


def test_processes_left_behind_are_killed(workdir):
    # A new session and process group, which the old process-group kill did not reach.
    result = run(workdir, "setsid sh -c 'echo $$ > daemon.pid; exec sleep 300' > /dev/null 2>&1 & sleep 0.5")
    assert result.returncode == 0, result.stderr
    assert not process_exists(read_pid(workdir / "daemon.pid"))


def test_terminate_kills_the_command_and_its_leftovers(workdir):
    script = "setsid sh -c 'echo $$ > daemon.pid; exec sleep 300' > /dev/null 2>&1 & echo $$ > main.pid; sleep 300"
    process = subprocess.Popen(command(workdir, script), cwd=workdir)
    main, daemon = read_pid(workdir / "main.pid"), read_pid(workdir / "daemon.pid")
    process.terminate()
    assert process.wait(timeout=10) == 128 + signal.SIGTERM
    assert not process_exists(main)
    assert not process_exists(daemon)


def test_compilations_are_the_out_of_memory_killers_first_choice(workdir):
    process = subprocess.Popen(command(workdir, "echo $$ > main.pid; exec sleep 30"), cwd=workdir)
    try:
        assert Path(f"/proc/{read_pid(workdir / 'main.pid')}/oom_score_adj").read_text() == "1000\n"
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_memory_limit(workdir):
    allocate = f"{PYTHON} -I -c 'bytearray(400 * 1024**2)'"
    limited = run(workdir, allocate, f"--memory={200 * 1024**2}")
    assert limited.returncode == 1
    assert "MemoryError" in limited.stderr
    assert run(workdir, allocate).returncode == 0  # no limit by default


# The sandbox as on an older or more limited system: Landlock capped at an older
# ABI, and optionally without the system-call filter or without /proc.
DEGRADED = """
import sys
from texdrop import sandbox

abi, without = int(sys.argv[1]), sys.argv[2].split(",")
real_abi = sandbox.landlock_abi
sandbox.landlock_abi = lambda: min(abi, real_abi())

def unavailable(*args, **kwargs):
    raise OSError(22, "Invalid argument")

if "filter" in without:
    sandbox.filter_syscalls = unavailable
if "proc" in without:
    sandbox._descendants = unavailable
sandbox.main(sys.argv[3:])
"""


def run_degraded(workdir, script, abi, without=""):
    command = [sys.executable, "-I", "-c", DEGRADED, str(abi), without, str(workdir), "sh", "-c", script]
    return subprocess.run(command, cwd=workdir, capture_output=True, text=True, timeout=30, check=False)


@pytest.mark.parametrize("abi", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("without", ["", "filter"])
def test_older_kernels_still_run_commands_confined(workdir, abi, without):
    # Moving a file into another directory is refused before ABI 2; mv then copies it.
    script = f"mkdir -p a/b && echo hi > a/f && mv a/f a/b/f && cat a/b/f && cat {workdir.parent}/secret.txt"
    result = run_degraded(workdir, script, abi, without)
    assert result.stdout == "hi\n", result.stderr
    assert "Permission denied" in result.stderr  # the file outside stays unreadable
    assert result.returncode == 1


@syscall_filter
def test_filter_stops_truncation_before_landlock_abi_3(workdir):
    script = f"{PYTHON} -I -c \"import os; os.truncate('{workdir.parent}/secret.txt', 0)\""
    result = run_degraded(workdir, script, 2)
    assert "PermissionError" in result.stderr
    assert (workdir.parent / "secret.txt").read_text() == "TOP-SECRET"


def test_without_proc_the_process_group_is_still_killed(workdir):
    result = run_degraded(workdir, "sh -c 'echo $$ > left.pid; exec sleep 300' & sleep 0.5", 6, "proc")
    assert result.returncode == 0, result.stderr
    assert not process_exists(read_pid(workdir / "left.pid"))


def test_missing_protections():
    if os.geteuid() == 0 or not os.path.isdir("/proc/self"):
        pytest.skip("needs an unprivileged user and /proc")
    assert sandbox.missing_protections(6, True) == []
    assert "config.json" in sandbox.missing_protections(0, True)[0]
    assert len(sandbox.missing_protections(5, True)) == 1  # signals and abstract sockets
    old = " ".join(sandbox.missing_protections(2, False))
    assert "truncate" in old and "signals" in old and "TCP and UDP" in old
