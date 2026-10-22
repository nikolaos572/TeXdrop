"""Safe inspection and extraction of uploaded project archives.

Every operation (inspecting an upload, submitting it, extracting it for the
compiler) goes through the same scan, so they all apply the same checks:

* the format is recognised from the file's content, never from its name;
* only regular files and directories are allowed (no links or devices);
* paths must stay inside the project (no absolute paths, no "..");
* the number of entries and their total unpacked size are bounded.

The server runs these functions only in a confined helper process (see unpack).
"""

from __future__ import annotations

import errno
import re
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import libarchive
import libarchive.exception

from .errors import InvalidInput


class ArchiveError(InvalidInput):
    """The archive is unreadable, unsupported or unsafe."""


@dataclass(frozen=True)
class ArchiveFormat:
    name: str
    extension: str
    reader_format: str  # libarchive names
    reader_filter: str


ZIP = ArchiveFormat("zip", ".zip", "zip", "none")
SEVEN_ZIP = ArchiveFormat("7z", ".7z", "7zip", "none")
TAR = ArchiveFormat("tar", ".tar", "tar", "none")
TAR_GZ = ArchiveFormat("tar.gz", ".tar.gz", "tar", "gzip")
TAR_BZ2 = ArchiveFormat("tar.bz2", ".tar.bz2", "tar", "bzip2")
TAR_XZ = ArchiveFormat("tar.xz", ".tar.xz", "tar", "xz")
FORMATS = {archive_format.name: archive_format for archive_format in (ZIP, SEVEN_ZIP, TAR, TAR_GZ, TAR_BZ2, TAR_XZ)}

_SIGNATURES = (  # (offset, magic bytes, format)
    (0, b"PK\x03\x04", ZIP),
    (0, b"PK\x05\x06", ZIP),  # empty zip
    (0, b"7z\xbc\xaf\x27\x1c", SEVEN_ZIP),
    (0, b"\x1f\x8b", TAR_GZ),
    (0, b"BZh", TAR_BZ2),
    (0, b"\xfd7zXZ\x00", TAR_XZ),
    (257, b"ustar", TAR),
)
SUPPORTED = ".zip, .7z, .tar, .tar.gz (.tgz), .tar.bz2 or .tar.xz"

MAX_ENTRIES = 20_000
_HEAD_BYTES = 64 * 1024  # how much of a .tex file is searched for \documentclass
_DOCUMENTCLASS = re.compile(rb"^[^%\n]*\\documentclass", re.MULTILINE)
# A main file's path is passed to latexmk, which runs commands through a shell:
# allow only characters that are harmless inside double quotes.
_SAFE_COMPONENT = re.compile(r"\w[\w .,+=@()-]*")


@dataclass(frozen=True)
class ArchiveInfo:
    format: ArchiveFormat
    root: str  # "" or the single top-level folder all files are in, like "paper/"
    tex_files: tuple[str, ...]  # .tex files usable as main file, relative to `root`
    main: str | None  # the detected main file, if exactly one candidate stands out


def detect_format(path: Path) -> ArchiveFormat:
    with path.open("rb") as file:
        head = file.read(512)
    for offset, magic, archive_format in _SIGNATURES:
        if head[offset : offset + len(magic)] == magic:
            return archive_format
    raise ArchiveError(f"unsupported file type: upload a {SUPPORTED} archive")


def inspect_archive(path: Path, max_bytes: int) -> ArchiveInfo:
    """Validate the archive and find its .tex files, without extracting it."""
    return _scan(path, max_bytes, destination=None)


def extract_archive(path: Path, destination: Path, max_bytes: int) -> ArchiveInfo:
    """Validate and extract the archive into `destination` (an empty directory)."""
    return _scan(path, max_bytes, destination)


def resolve_main(info: ArchiveInfo, requested: str | None) -> str:
    """Return the main file to compile: `requested` if given, else the detected one."""
    requested = (requested or "").strip()
    if not requested:
        if info.main:
            return info.main
        if not info.tex_files:
            raise ArchiveError("the archive contains no .tex file")
        raise ArchiveError(f"several .tex files found, choose the main file: {_listing(info.tex_files)}")
    if info.root and requested.startswith(info.root):
        requested = requested.removeprefix(info.root)
    if requested not in info.tex_files:
        detail = f"choose one of: {_listing(info.tex_files)}" if info.tex_files else "the archive has no usable .tex file"
        raise ArchiveError(f"main file {requested!r} is not a .tex file in the archive; {detail}")
    return requested


def is_valid_main(path: str) -> bool:
    return path.lower().endswith(".tex") and all(_SAFE_COMPONENT.fullmatch(part) for part in path.split("/"))


def _listing(names: tuple[str, ...], limit: int = 20) -> str:
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def _scan(path: Path, max_bytes: int, destination: Path | None) -> ArchiveInfo:
    archive_format = detect_format(path)
    files: dict[str, bool] = {}  # path -> is a .tex file containing \documentclass
    total = 0
    try:
        with libarchive.file_reader(str(path), archive_format.reader_format, archive_format.reader_filter) as archive:
            for count, entry in enumerate(archive, start=1):
                if count > MAX_ENTRIES:
                    raise ArchiveError(f"the archive has more than {MAX_ENTRIES} entries")
                name = _entry_path(entry.pathname or "")
                if entry.issym or entry.islnk:
                    raise ArchiveError(f"links are not allowed in the archive: {entry.pathname!r}")
                if name is None or entry.isdir:
                    continue
                if not entry.isreg:
                    raise ArchiveError(f"special files are not allowed in the archive: {entry.pathname!r}")
                total += entry.size or 0
                if total > max_bytes:
                    raise _too_large(max_bytes)
                try:
                    files[name], total = _read_entry(entry, name, destination, total, max_bytes)
                except OSError as exc:
                    raise ArchiveError(f"cannot extract {name!r}: {exc.strerror}") from None
    except libarchive.exception.ArchiveError as exc:
        if exc.errno == errno.ENOMEM:  # libarchive's message depends on which allocation failed
            raise ArchiveError("unpacking the archive ran out of memory") from None
        raise ArchiveError(f"cannot read the {archive_format.name} archive: {exc.msg}") from None
    if not files:
        raise ArchiveError("the archive contains no files")
    root = _common_folder(list(files))
    tex = {name.removeprefix(root): head for name, head in files.items() if is_valid_main(name.removeprefix(root))}
    return ArchiveInfo(archive_format, root, tuple(sorted(tex)), _detect_main(tex))


def _read_entry(
    entry: libarchive.ArchiveEntry, name: str, destination: Path | None, total: int, max_bytes: int
) -> tuple[bool, int]:
    """Read a file entry, writing it under `destination` if given. Returns whether
    it is a .tex file with a \\documentclass, and the running total of bytes."""
    is_tex = name.lower().endswith(".tex")
    if destination is None and not is_tex:
        return False, total
    declared = entry.size or 0
    head = b""
    written = 0
    with _create(destination, name) as output:
        for block in entry.get_blocks():
            if is_tex and len(head) < _HEAD_BYTES:
                head += block[: _HEAD_BYTES - len(head)]
            if output is None:  # inspecting: the head is all we need
                if len(head) >= _HEAD_BYTES:
                    break
                continue
            written += len(block)
            if written > declared and total + written - declared > max_bytes:  # header understated the size
                raise _too_large(max_bytes)
            output.write(block)
    return is_tex and _DOCUMENTCLASS.search(head) is not None, total + max(0, written - declared)


def _create(destination: Path | None, name: str) -> BinaryIO | nullcontext[None]:
    if destination is None:
        return nullcontext()
    target = destination.joinpath(*name.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    return target.open("wb")


def _too_large(max_bytes: int) -> ArchiveError:
    return ArchiveError(f"the archive unpacks to more than {max_bytes // 1024**2} MB")


def _entry_path(raw: str | bytes) -> str | None:
    """The entry's normalized relative path, or None for entries to skip."""
    if isinstance(raw, bytes):  # name not valid in the archive's encoding: keep its bytes
        raw = raw.decode("utf-8", "surrogateescape")
    if raw.startswith("/") or any(ord(char) < 32 for char in raw):
        raise ArchiveError(f"unsafe path in the archive: {raw!r}")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise ArchiveError(f"unsafe path in the archive: {raw!r}")
    if not parts or parts[0] == "__MACOSX" or parts[-1] == ".DS_Store" or parts[-1].startswith("._"):
        return None  # empty, or macOS metadata
    return "/".join(parts)


def _common_folder(names: list[str]) -> str:
    """The folder that contains every file, like "paper/" for a zipped folder."""
    root = ""
    while all("/" in name for name in names) and len({name.split("/", 1)[0] for name in names}) == 1:
        folder = names[0].split("/", 1)[0] + "/"
        root += folder
        names = [name.removeprefix(folder) for name in names]
    return root


def _detect_main(tex: dict[str, bool]) -> str | None:
    """The only .tex file at the top level, or else the only one there with a \\documentclass."""
    top_level = [name for name in tex if "/" not in name]
    if len(top_level) == 1:
        return top_level[0]
    documents = [name for name in top_level if tex[name]]
    return documents[0] if len(documents) == 1 else None
