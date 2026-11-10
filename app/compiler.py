"""Compile an extracted project with latexmk, confined by the sandbox."""

from __future__ import annotations

import functools
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import sandbox
from .storage import disk_usage

ENGINES = {"pdflatex": "-pdf", "xelatex": "-pdfxe", "lualatex": "-pdflua"}
DEFAULT_ENGINE = "pdflatex"
# Pre-warmed per-job caches (biber's unpacked runtime, LuaTeX font names), built by the Dockerfile.
JOB_TEMPLATE = Path("/opt/latex-job-template")
# TeX errors ("! ..." or "file:line: ..." with -file-line-error) and biber errors.
_ERROR_LINE = re.compile(r"^(?:! .+|\S[^:\n]*:\d+: .+|ERROR - .+)$", re.MULTILINE)
# latexmk reporting a program that died of SIGKILL (128 + 9): the kernel's out-of-memory killer.
_KILLED = re.compile(r"^\s*(\S+): Command for '[^'\n]*' gave return code 137$", re.MULTILINE)
_LOG_SCAN_BYTES = 4 * 1024**2
DISK_CHECK_SECONDS = 2.0  # how often the work directory is measured against the disk limit
STOP_SECONDS = 15  # how long the sandbox may take to stop a compilation and its leftovers


@dataclass(frozen=True)
class CompileResult:
    pdf: Path | None  # the document, if one was produced (possibly despite errors)
    error: str | None  # None when the compilation succeeded


class LatexCompiler:
    def __init__(
        self,
        timeout_seconds: float,
        latexmk: Sequence[str] = ("latexmk",),
        template: Path = JOB_TEMPLATE,
        *,
        memory_bytes: int = 0,  # address space of each process; 0: no limit
        disk_bytes: int = 0,  # the work directory; 0: no limit
    ) -> None:
        self._timeout = timeout_seconds
        self._latexmk = list(latexmk)  # the command that runs latexmk
        self._template = template
        self._memory_bytes = memory_bytes
        self._disk_bytes = disk_bytes

    def run(
        self, workdir: Path, project: Path, main: str, engine: str, outputs: Path, cancelled: threading.Event
    ) -> CompileResult:
        """Compile `main` (relative to `project`, which lies inside `workdir`).

        Only `workdir` is writable for the compilation. The transcript is written
        to `outputs`.log and the PDF copied to `outputs`.pdf, both outside it.
        """
        main_path = PurePosixPath(main)
        directory = project.joinpath(*main_path.parent.parts)
        self._prepare(workdir)
        command = [
            sys.executable, "-I", sandbox.__file__, f"--memory={self._memory_bytes}",
            *(f"--read={directory}" for directory in tex_directories()), str(workdir),
            *self._latexmk, "-norc", ENGINES[engine], "-interaction=nonstopmode", "-file-line-error", "-f",
            f"./{main_path.name}",
        ]  # fmt: skip
        log = outputs.with_suffix(".log")
        with log.open("wb") as transcript:
            process = subprocess.Popen(
                command,
                cwd=directory,
                env=_environment(workdir),
                stdin=subprocess.DEVNULL,
                stdout=transcript,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # apart from the server's process group and terminal
            )
            stopped = self._wait(process, workdir, cancelled)
            if stopped:
                transcript.write(f"\n{stopped}\n".encode())
        pdf = outputs.with_suffix(".pdf")
        relative_pdf = directory.relative_to(workdir) / f"{main_path.stem}.pdf"
        if not _copy_regular_file(workdir, relative_pdf, pdf):
            pdf = None
        if stopped:
            return CompileResult(pdf, stopped)
        if process.returncode == 0 and pdf is not None:
            return CompileResult(pdf, None)
        return CompileResult(pdf, _error_summary(log) or f"latexmk failed with exit code {process.returncode}")

    def _prepare(self, workdir: Path) -> None:
        if self._template.is_dir():
            shutil.copytree(self._template, workdir, symlinks=True, dirs_exist_ok=True)
        (workdir / "tmp").mkdir(exist_ok=True)

    def _wait(self, process: subprocess.Popen[bytes], workdir: Path, cancelled: threading.Event) -> str | None:
        """Wait for the process to end. If it has to be stopped, stop it and say why."""
        deadline = time.monotonic() + self._timeout
        measured = time.monotonic()
        while True:
            try:
                process.wait(timeout=0.25)
                return None
            except subprocess.TimeoutExpired:
                pass
            if cancelled.is_set():
                reason = "Compilation cancelled."
            elif time.monotonic() >= deadline:
                reason = f"Compilation timed out after {self._timeout / 60:g} minutes."
            elif self._disk_bytes and time.monotonic() - measured >= DISK_CHECK_SECONDS:
                measured = time.monotonic()
                if disk_usage(workdir) <= self._disk_bytes:
                    continue
                reason = f"Compilation stopped: its files took more than {self._disk_bytes / 1024**2:g} MB of disk."
            else:
                continue
            _stop(process)
            return reason


@functools.cache
def tex_live_version() -> str | None:
    """The installed TeX Live release, such as "TeX Live 2026"; None if it cannot be found."""
    try:
        output = subprocess.run(
            ["tex", "--version"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"\((TeX Live \d{4})", output)
    return match.group(1) if match else None


@functools.cache
def tex_directories() -> tuple[str, ...]:
    """TeX's directories outside the system directories compilations may read, such
    as a TeX Live installed in a home directory (found through kpsewhich)."""
    found, home = [], Path(os.path.expanduser("~"))
    for variable in ("TEXMFROOT", "TEXMFLOCAL", "TEXMFSYSVAR", "TEXMFSYSCONFIG"):
        try:
            value = subprocess.run(
                ["kpsewhich", f"-var-value={variable}"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=False,
            ).stdout.strip()  # fmt: skip
        except (OSError, subprocess.SubprocessError):  # no TeX here
            return ()
        directory = Path(value).resolve()
        if (
            Path(value).is_absolute()
            and directory.is_dir()
            and not sandbox.readable_inside(directory)
            and directory != directory.parent  # never "/"
            and not home.is_relative_to(directory)  # nor "/home" or a whole home directory
        ):
            found.append(directory)
    return tuple(str(d) for d in dict.fromkeys(found) if not any(d != o and d.is_relative_to(o) for o in found))


def _stop(process: subprocess.Popen[bytes]) -> None:
    """Have the sandbox kill the compilation and every process it left behind.
    Should the sandbox not end in time, kill the sandbox's process instead."""
    process.terminate()
    try:
        process.wait(timeout=STOP_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _environment(workdir: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "HOME": str(workdir),
        "TMPDIR": str(workdir / "tmp"),
        "TEXMFVAR": str(workdir / "texmf-var"),
        "PAR_GLOBAL_TEMP": str(workdir / "biber-cache"),  # where biber keeps its unpacked runtime
        "openout_any": "p",  # TeX writes only inside the project (TeX Live's default, enforced)
        "shell_escape": "p",  # \write18 runs only TeX Live's whitelisted programs (default, enforced)
        "max_print_line": "1000",  # do not wrap error messages in the log
    }


def _copy_regular_file(root: Path, relative: Path, target: Path) -> bool:
    """Copy root/relative to target if it is a regular file, never following a
    link: the sandboxed compilation could have planted one to leak other files."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in relative.parts[:-1]:
            parent, fd = fd, os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(parent)
        parent, fd = fd, os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        os.close(parent)
    except OSError:
        os.close(fd)
        return False
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return False
        with target.open("wb") as copy:
            shutil.copyfileobj(source, copy)
    return True


def _error_summary(log: Path, limit: int = 5) -> str | None:
    """The programs the kernel killed, then the first distinct error lines of the transcript."""
    with log.open("rb") as file:
        text = file.read(_LOG_SCAN_BYTES).decode("utf-8", "replace")
    killed = [f"{name} was killed, most likely because the memory ran out." for name in _KILLED.findall(text)]
    errors = list(dict.fromkeys(killed + [match.group(0).strip() for match in _ERROR_LINE.finditer(text)]))
    return "\n".join(errors[:limit]) or None
