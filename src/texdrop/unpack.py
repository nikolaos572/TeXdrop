"""Inspect and extract uploaded archives in a separate, confined process.

Reading an archive runs libarchive's C code on untrusted input, and the server
can read every user's files and tokens, so that code never runs inside it. A
helper process (python -m texdrop.unpack ARCHIVE MAX_BYTES MEMORY_BYTES
[DESTINATION]) loads what it needs, then confines itself with the sandbox: it
can read the archive and system files, write only into the destination
directory, cannot open network sockets or start processes, and has a time and
memory limit (UNPACK_TIMEOUT_MINUTES, UNPACK_MEMORY_MB). It applies the archive
module's checks and reports the result as JSON, which the server checks again:
a helper subverted by a crafted archive could report anything.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import sandbox
from .archive import FORMATS, MAX_ENTRIES, ArchiveError, ArchiveInfo, extract_archive, inspect_archive, is_valid_main

log = logging.getLogger(__name__)

_MAX_REPORT_BYTES = 16 * 1024**2
_MAX_ERROR_CHARS = 2000


class _BadReport(Exception):
    """The helper's output is not a well-formed result."""


@dataclass(frozen=True)
class Unpacker:
    """Runs the helper process with these limits."""

    max_bytes: int  # total size of an archive's unpacked files
    timeout_seconds: float = 300
    memory_bytes: int = 4 * 1024**3  # address space of the helper; 0: no limit

    def inspect(self, archive: Path) -> ArchiveInfo:
        """archive.inspect_archive(), run by the helper."""
        return self._run(archive, None)

    def extract(self, archive: Path, destination: Path) -> ArchiveInfo:
        """archive.extract_archive(), run by the helper; creates `destination`."""
        destination.mkdir(parents=True, exist_ok=True)
        return self._run(archive, destination)

    def _run(self, archive: Path, destination: Path | None) -> ArchiveInfo:
        # Files instead of pipes: the helper's output is read only up to a limit.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            try:
                helper = subprocess.run(
                    _command(archive, self.max_bytes, self.memory_bytes, destination),
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=errors,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                minutes = self.timeout_seconds / 60
                raise ArchiveError(f"unpacking the archive took longer than {minutes:g} minutes") from None
            output.seek(0)
            report = output.read(_MAX_REPORT_BYTES + 1)
            if helper.returncode == 0 and len(report) <= _MAX_REPORT_BYTES:
                try:
                    return _read_report(report)
                except _BadReport:
                    pass
            errors.seek(0)
            stderr = errors.read(_MAX_ERROR_CHARS).decode("utf-8", "replace")
        log.warning("archive helper failed (exit code %d): %s", helper.returncode, stderr.strip() or "no error output")
        raise ArchiveError("the archive could not be unpacked")


def _command(archive: Path, max_bytes: int, memory_bytes: int, destination: Path | None) -> list[str]:
    command = [sys.executable, "-I", "-m", __name__, str(archive), str(max_bytes), str(memory_bytes)]
    return command if destination is None else [*command, str(destination)]


def _read_report(report: bytes) -> ArchiveInfo:
    """The helper's result, checked as untrusted input: an ArchiveInfo, or the
    ArchiveError the helper reported. Anything else raises _BadReport."""
    try:
        data = json.loads(report)
    except ValueError:  # not JSON, or not UTF-8
        raise _BadReport from None
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        raise ArchiveError(data["error"][:_MAX_ERROR_CHARS])
    try:
        archive_format = FORMATS[data["format"]]
        root, tex_files, main = data["root"], tuple(data["tex_files"]), data["main"]
    except (KeyError, TypeError):
        raise _BadReport from None
    valid = (
        _is_valid_root(root)
        and len(tex_files) <= MAX_ENTRIES
        and all(isinstance(name, str) and is_valid_main(name) for name in tex_files)
        and (main is None or main in tex_files)
    )
    if not valid:
        raise _BadReport
    return ArchiveInfo(archive_format, root, tex_files, main)


def _is_valid_root(root: object) -> bool:
    """Whether `root` is empty or folders like "paper/", as archive._common_folder() returns them."""
    if not isinstance(root, str) or (root and not root.endswith("/")):
        return False
    parts = root.removesuffix("/").split("/") if root else []
    return all(part not in ("", ".", "..") and min(map(ord, part)) >= 32 for part in parts)


def main(argv: list[str]) -> None:
    archive, max_bytes, memory_bytes = Path(argv[0]), int(argv[1]), int(argv[2])
    destination = Path(argv[3]) if len(argv) > 3 else None
    sandbox.restrict(  # now that everything is loaded
        None if destination is None else str(destination),
        files=[str(archive)],
        memory_bytes=memory_bytes,
        processes=False,
    )
    try:
        if destination is None:
            info = inspect_archive(archive, max_bytes)
        else:
            info = extract_archive(archive, destination, max_bytes)
        report = {"format": info.format.name, "root": info.root, "tex_files": list(info.tex_files), "main": info.main}
    except ArchiveError as exc:
        report = {"error": str(exc)}
    sys.stdout.write(json.dumps(report))


if __name__ == "__main__":
    main(sys.argv[1:])
