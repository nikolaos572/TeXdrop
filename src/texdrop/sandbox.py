"""Confine a command with Landlock and a system-call filter, then clean up after it.

Usage: python -m texdrop.sandbox [--memory=BYTES] [--read=DIR]... WORKDIR COMMAND [ARG...]

The command and everything it starts may read and execute system files (and
each --read DIR) and read/write WORKDIR. Everything else (other jobs, the token
configuration, the database) is invisible to it; it cannot open network
sockets (UNIX sockets only) or signal processes outside the sandbox. --memory
caps the address space of each of its processes.

Every protection is used as far as the kernel offers it: Landlock (Linux 5.13+)
gained features up to ABI 6 (Linux 6.12), and the system-call filter needs
x86-64 or ARM64. Where one is missing the command still runs, with less
protection; missing_protections() describes what is lost, for the server's log.

This process stays outside the sandbox and supervises the command: processes
the command leaves behind become its children (it is a "subreaper"), and when
the command ends, or this process receives SIGTERM, it kills every one of them,
including those that left the command's process group or session.
"""

from __future__ import annotations

import ctypes
import errno
import functools
import os
import platform
import resource
import signal
import struct
import subprocess
import sys
import time
from collections.abc import Sequence
from contextlib import suppress
from typing import NoReturn

_SYS_CREATE_RULESET = 444  # same number on every architecture
_SYS_ADD_RULE = 445
_SYS_RESTRICT_SELF = 446
_CREATE_RULESET_VERSION = 1
_RULE_PATH_BENEATH = 1
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
_PR_SET_CHILD_SUBREAPER = 36
_PR_SET_NO_NEW_PRIVS = 38

_EXECUTE = 1 << 0
_WRITE_FILE = 1 << 1
_READ_FILE = 1 << 2
_READ_DIR = 1 << 3
_REFER = 1 << 13  # ABI 2
_TRUNCATE = 1 << 14  # ABI 3
_IOCTL_DEV = 1 << 15  # ABI 5
_NET_TCP = (1 << 0) | (1 << 1)  # bind and connect, ABI 4
_SCOPES = (1 << 0) | (1 << 1)  # abstract UNIX sockets and signals, ABI 6

# The system-call filter (seccomp), a classic BPF program run on every system call.
_SECCOMP_ARCHES = {  # machine: (AUDIT_ARCH_*, socket, truncate, the calls that create processes)
    "x86_64": (0xC000003E, 41, 76, (56, 57, 58)),  # clone, fork, vfork
    "aarch64": (0xC00000B7, 198, 45, (220,)),  # clone
}
_SYS_IO_URING_SETUP = 425  # same number on every architecture
_SYS_CLONE3 = 435
_X32_SYSCALL_BIT = 0x40000000
_AF_UNIX = 1
_BPF_LOAD = 0x20  # BPF_LD | BPF_W | BPF_ABS: load 32 bits of struct seccomp_data
_BPF_JEQ = 0x15  # BPF_JMP | BPF_JEQ | BPF_K
_BPF_JGE = 0x35  # BPF_JMP | BPF_JGE | BPF_K
_BPF_RETURN = 0x06  # BPF_RET | BPF_K
_SECCOMP_KILL = 0x80000000  # the whole process
_SECCOMP_ERRNO = 0x00050000
_SECCOMP_ALLOW = 0x7FFF0000

READ_ONLY_DIRS = (
    "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc", "/opt",
    "/var/lib/texmf", "/var/cache/fontconfig",
)
DEVICES = ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom")
MAX_FILE_BYTES = 2 * 1024**3  # no single output file may grow beyond this
CLEANUP_SECONDS = 5  # how long to keep killing leftover processes before giving up


_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long


def _syscall(number: int, *args: object) -> int:
    result = _libc.syscall(ctypes.c_long(number), *args)
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return result


def _prctl(option: int, *args: int | ctypes.Array[ctypes.c_char]) -> None:
    values = [ctypes.c_ulong(arg) if isinstance(arg, int) else arg for arg in args]
    values += [ctypes.c_ulong(0)] * (4 - len(values))  # unused arguments must be 0
    if _libc.prctl(ctypes.c_int(option), *values) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def forbid_new_privileges() -> None:
    """Make this process and its future children unable to gain privileges, through
    setuid programs or file capabilities, as Docker's no-new-privileges option does."""
    _prctl(_PR_SET_NO_NEW_PRIVS, 1)


def landlock_abi() -> int:
    """Return the Landlock ABI version available to this process, 0 if none."""
    try:
        return _syscall(_SYS_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint32(_CREATE_RULESET_VERSION))
    except OSError:
        return 0


def syscall_filter_supported() -> bool:
    """Whether the system-call filter knows this processor architecture."""
    return platform.machine() in _SECCOMP_ARCHES


@functools.cache
def syscall_filter_works() -> bool:
    """Whether the system-call filter can be installed here (tried in a child process)."""
    if not syscall_filter_supported():
        return False
    check = [sys.executable, "-I", __file__, "--check-filter"]
    return subprocess.run(check, stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False).returncode == 0


def missing_protections(abi: int, syscall_filter: bool) -> list[str]:
    """What a document can do here that the sandbox prevents on a recent kernel
    (Linux 6.12 or newer, x86-64 or ARM64). Empty when nothing is missing."""
    missing = []
    if not abi:
        missing.append(
            "no Landlock (Linux 5.13+): documents can read and change every file the server can, "
            "including config.json with all tokens and every user's jobs"
        )
    elif abi < 3 and not syscall_filter:
        missing.append(
            "documents can empty (truncate) files outside their directory that the server can write, "
            "such as the database (needs Linux 6.2+ or the system-call filter)"
        )
    if 0 < abi < 6:
        missing.append(
            "documents can send signals to other processes of the server's user, including the server, "
            "and reach local services through abstract UNIX sockets (needs Linux 6.12+)"
        )
    if not syscall_filter:
        network = "TCP and UDP" if abi < 4 else "UDP"
        missing.append(
            f"no system-call filter ({platform.machine()}): documents can use the network ({network}), "
            "and the archive helper is not stopped from starting processes"
        )
    if not os.path.isdir("/proc/self"):
        missing.append("/proc is not mounted: processes a document starts in a new process group survive the job")
    if os.geteuid() == 0:
        missing.append("running as root: documents run as root too; run the server as an unprivileged user")
    return missing


def readable_inside(path: str | os.PathLike[str], extra_dirs: Sequence[str] = ()) -> bool:
    """Whether confined commands can read `path`, because it lies in a system
    directory or in one of `extra_dirs`."""
    resolved = os.path.realpath(path)
    return any(
        os.path.commonpath([resolved, os.path.realpath(directory)]) == os.path.realpath(directory)
        for directory in (*READ_ONLY_DIRS, *extra_dirs)
    )


def _fs_rights(abi: int) -> int:
    rights = (1 << 13) - 1  # ABI 1: execute .. make_sym
    if abi >= 2:
        rights |= _REFER
    if abi >= 3:
        rights |= _TRUNCATE
    if abi >= 5:
        rights |= _IOCTL_DEV
    return rights


def _allow(ruleset: int, path: str, access: int) -> None:
    try:
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except FileNotFoundError:
        return
    try:
        rule = ctypes.create_string_buffer(struct.pack("=Qi", access, fd))  # packed C struct
        _syscall(_SYS_ADD_RULE, ctypes.c_int(ruleset), ctypes.c_int(_RULE_PATH_BENEATH), rule, ctypes.c_uint32(0))
    finally:
        os.close(fd)


def confine(workdir: str | None, abi: int, files: Sequence[str] = (), dirs: Sequence[str] = ()) -> None:
    """Restrict this process (and its future children) to the rules above, plus
    reading `files`, and reading and executing in `dirs`. Without a `workdir`
    nothing is writable. Needs no_new_privs."""
    handled = _fs_rights(abi)
    fields = [handled]  # struct landlock_ruleset_attr grew one field in ABI 4 and one in ABI 6
    if abi >= 4:
        fields.append(_NET_TCP)
    if abi >= 6:
        fields.append(_SCOPES)
    attr = struct.pack(f"={len(fields)}Q", *fields)
    ruleset = _syscall(
        _SYS_CREATE_RULESET, ctypes.create_string_buffer(attr), ctypes.c_size_t(len(attr)), ctypes.c_uint32(0)
    )
    try:
        for path in (*READ_ONLY_DIRS, *dirs):
            _allow(ruleset, path, _EXECUTE | _READ_FILE | _READ_DIR)
        device_access = _READ_FILE | _WRITE_FILE | (handled & _TRUNCATE)
        for path in DEVICES:
            _allow(ruleset, path, device_access)
        for path in files:
            _allow(ruleset, path, _READ_FILE)
        if workdir is not None:
            _allow(ruleset, workdir, handled)
        _syscall(_SYS_RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint32(0))
    finally:
        os.close(ruleset)


def _bpf(code: int, value: int, jump_true: int = 0, jump_false: int = 0) -> bytes:
    return struct.pack("=HBBI", code, jump_true, jump_false, value)  # struct sock_filter


def filter_syscalls(*, processes: bool, truncate: bool) -> None:
    """Forbid network sockets (any but UNIX sockets) and io_uring (which can open
    them) to this process and its future children. Unless `processes`, also forbid
    creating processes and threads; unless `truncate`, truncating files by name
    (for Landlock before ABI 3, which cannot restrict it). Needs no_new_privs."""
    arch, socket_call, truncate_call, process_calls = _SECCOMP_ARCHES[platform.machine()]
    program = [
        _bpf(_BPF_LOAD, 4),  # seccomp_data.arch
        _bpf(_BPF_JEQ, arch, jump_true=1),
        _bpf(_BPF_RETURN, _SECCOMP_KILL),  # another ABI, such as 32-bit x86: nothing allowed
        _bpf(_BPF_LOAD, 0),  # seccomp_data.nr
        _bpf(_BPF_JGE, _X32_SYSCALL_BIT, jump_false=1),
        _bpf(_BPF_RETURN, _SECCOMP_KILL),  # the x32 ABI
    ]
    denied = {_SYS_IO_URING_SETUP: errno.EPERM}
    if not processes:
        denied |= dict.fromkeys(process_calls, errno.EPERM) | {_SYS_CLONE3: errno.ENOSYS}  # ENOSYS: use clone
    if not truncate:
        denied[truncate_call] = errno.EACCES
    for call, error in denied.items():
        program += [_bpf(_BPF_JEQ, call, jump_false=1), _bpf(_BPF_RETURN, _SECCOMP_ERRNO | error)]
    program += [
        _bpf(_BPF_JEQ, socket_call, jump_true=1),
        _bpf(_BPF_RETURN, _SECCOMP_ALLOW),
        _bpf(_BPF_LOAD, 16),  # the low half of the first argument: the address family
        _bpf(_BPF_JEQ, _AF_UNIX, jump_false=1),
        _bpf(_BPF_RETURN, _SECCOMP_ALLOW),
        _bpf(_BPF_RETURN, _SECCOMP_ERRNO | errno.EACCES),
    ]
    instructions = ctypes.create_string_buffer(b"".join(program))
    fprog = struct.pack("=HxxxxxxQ", len(program), ctypes.addressof(instructions))  # struct sock_fprog
    _prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.create_string_buffer(fprog))


def restrict(
    workdir: str | None,
    *,
    files: Sequence[str] = (),
    dirs: Sequence[str] = (),
    memory_bytes: int = 0,
    processes: bool = True,
) -> None:
    """Apply every restriction to this process; its future children inherit them.

    Landlock as in confine(), the system-call filter as in filter_syscalls(), each
    as far as this kernel offers it; resource limits (`memory_bytes`: address
    space per process, 0 for none); and first place in the out-of-memory killer's
    choice, ahead of the server.
    """
    with suppress(OSError), open("/proc/self/oom_score_adj", "w") as score:  # before Landlock hides /proc
        score.write("1000")
    forbid_new_privileges()
    if abi := landlock_abi():
        confine(workdir, abi, files, dirs)
    if syscall_filter_supported():
        with suppress(OSError):  # seccomp unavailable here; the server warned at startup
            filter_syscalls(processes=processes, truncate=abi >= 3)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES, MAX_FILE_BYTES))
    if memory_bytes:
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))


_SUPERVISOR_SIGNALS = {signal.SIGCHLD, signal.SIGTERM}  # blocked, and taken with sigwaitinfo()


def _run_confined(workdir: str, command: list[str], dirs: list[str], memory_bytes: int) -> NoReturn:
    """In the forked child: confine it, then replace it with the command."""
    try:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, _SUPERVISOR_SIGNALS)  # the mask survives exec
        os.setpgid(0, 0)  # a process group of its own, which kill_descendants() kills at once
        restrict(workdir, dirs=dirs, memory_bytes=memory_bytes)
        os.execvp(command[0], command)
    except BaseException as exc:  # never return into the supervisor's code
        os.write(2, f"sandbox: cannot run {command[0]}: {exc}\n".encode())
    os._exit(127)


def _reap(child: int = 0) -> int | None:
    """Collect the children that ended; the wait status of `child` if it is one of them."""
    status = None
    with suppress(ChildProcessError):  # no children left
        while (ended := os.waitpid(-1, os.WNOHANG))[0]:
            if ended[0] == child:
                status = ended[1]
    return status


def _supervise(child: int) -> int:
    """Wait for `child` to end, reaping the orphans that end meanwhile. Returns its
    exit code, or -SIGTERM if this process is told to stop first."""
    while (status := _reap(child)) is None:
        if signal.sigwaitinfo(_SUPERVISOR_SIGNALS).si_signo == signal.SIGTERM:
            return -signal.SIGTERM
    return os.waitstatus_to_exitcode(status)


def _descendants() -> list[int]:
    """The processes below this one, found through the parent ids in /proc."""
    children: dict[int, list[int]] = {}
    for name in os.listdir("/proc"):
        if name.isdigit():
            try:
                with open(f"/proc/{name}/stat", "rb") as file:
                    stat = file.read()
            except OSError:  # ended meanwhile
                continue
            parent = int(stat[stat.rindex(b")") + 2 :].split()[1])  # after "pid (name) state"
            children.setdefault(parent, []).append(int(name))
    found, pending = [], [os.getpid()]
    while pending:
        below = children.get(pending.pop(), [])
        found += below
        pending += below
    return found


def _wait_for_children(deadline: float) -> None:
    """Reap this process's children as they end, until none is left or time is up."""
    while time.monotonic() < deadline:
        _reap()
        try:
            os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)  # only to learn whether any is left
        except ChildProcessError:
            return
        time.sleep(0.01)


def kill_descendants(group: int) -> None:
    """Kill the process group `group`, then every other process below this one.
    Repeats until none is left, since killing a process hands its children to
    this one, and they may still be starting more."""
    with suppress(ProcessLookupError, PermissionError):  # already empty
        os.killpg(group, signal.SIGKILL)
    deadline = time.monotonic() + CLEANUP_SECONDS
    while True:
        _reap()
        try:
            remaining = _descendants()
        except OSError:  # no /proc: the process group was all there was to kill
            _wait_for_children(deadline)
            return
        if not remaining:
            return
        if time.monotonic() > deadline:
            os.write(2, f"sandbox: {len(remaining)} leftover process(es) could not be stopped\n".encode())
            return
        for pid in remaining:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        time.sleep(0.01)


def _check_filter() -> int:
    """Install the system-call filter in this process: exit code 0 if it works."""
    try:
        _prctl(_PR_SET_NO_NEW_PRIVS, 1)
        filter_syscalls(processes=False, truncate=False)
    except OSError:
        return 1
    return 0


def main(argv: list[str]) -> None:
    memory_bytes, dirs = 0, []
    while argv and argv[0].startswith("--"):
        option, _, value = argv.pop(0).partition("=")
        if option == "--memory":
            memory_bytes = int(value)
        elif option == "--read":
            dirs.append(value)
        elif option == "--check-filter":
            sys.exit(_check_filter())
        else:
            argv = []  # unknown option: show the usage
    if len(argv) < 2:
        sys.exit("usage: python -m texdrop.sandbox [--memory=BYTES] [--read=DIR]... WORKDIR COMMAND [ARG...]")
    workdir, command = argv[0], argv[1:]
    with suppress(OSError):  # without it, kill_descendants() still kills the command's process group
        _prctl(_PR_SET_CHILD_SUBREAPER, 1)
    signal.pthread_sigmask(signal.SIG_BLOCK, _SUPERVISOR_SIGNALS)
    child = os.fork()
    if child == 0:
        _run_confined(workdir, command, dirs, memory_bytes)
    with suppress(OSError):  # also here, whichever runs first (fails once the child has started the command)
        os.setpgid(child, child)
    code = _supervise(child)
    kill_descendants(child)
    sys.exit(code if code >= 0 else 128 - code)  # like a shell: 128 + the signal number


if __name__ == "__main__":
    main(sys.argv[1:])
