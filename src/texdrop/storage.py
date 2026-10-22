"""Layout of the data directory (the volume mounted into the container).

    <data>/config.json                       users and tokens
    <data>/jobs.db                           job metadata (SQLite)
    <data>/users/<user>/inputs/<id><ext>     uploaded archives
    <data>/users/<user>/outputs/<id>.pdf     compiled documents
    <data>/users/<user>/outputs/<id>.log     compilation logs
    <data>/logs/server.log                   the server's log (written by the logs module)
    <data>/tmp/                              uploads in progress and compile work dirs

It also measures and removes compile work dirs, whose content is hostile.
"""

from __future__ import annotations

import os
import shutil
import stat
import uuid
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

from .users import NAME_PATTERN


class Storage:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.config_path = root / "config.json"
        self.db_path = root / "jobs.db"
        self.tmp_dir = root / "tmp"

    def prepare(self) -> None:
        """Create the layout and discard leftovers of an interrupted run."""
        (self.root / "users").mkdir(parents=True, exist_ok=True)
        remove_tree(self.tmp_dir)
        self.tmp_dir.mkdir()

    def input_path(self, user: str, job_id: int, extension: str) -> Path:
        return self._user_dir(user) / "inputs" / f"{job_id}{extension}"

    def pdf_path(self, user: str, job_id: int) -> Path:
        return self._user_dir(user) / "outputs" / f"{job_id}.pdf"

    def log_path(self, user: str, job_id: int) -> Path:
        return self._user_dir(user) / "outputs" / f"{job_id}.log"

    def temp_path(self, prefix: str) -> Path:
        """A fresh, not yet existing path inside the temporary directory."""
        return self.tmp_dir / f"{prefix}-{uuid.uuid4().hex}"

    def store_input(self, upload: Path, user: str, job_id: int, extension: str) -> None:
        target = self.input_path(user, job_id, extension)
        target.parent.mkdir(parents=True, exist_ok=True)
        upload.replace(target)

    def store_outputs(self, user: str, job_id: int, pdf: Path | None, log: Path) -> None:
        target = self.log_path(user, job_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(log, target)
        if pdf is not None:
            shutil.move(pdf, self.pdf_path(user, job_id))

    def remove_job(self, user: str, job_id: int, extension: str) -> None:
        for path in (self.input_path(user, job_id, extension), self.pdf_path(user, job_id), self.log_path(user, job_id)):
            path.unlink(missing_ok=True)

    def user_size(self, user: str) -> int:
        """Bytes taken by the user's archives, PDFs and logs."""
        total = 0
        for folder in ("inputs", "outputs"):
            with suppress(FileNotFoundError), os.scandir(self._user_dir(user) / folder) as entries:
                for entry in entries:
                    with suppress(FileNotFoundError):  # deleted meanwhile
                        total += entry.stat(follow_symlinks=False).st_size
        return total

    def _user_dir(self, user: str) -> Path:
        if not NAME_PATTERN.fullmatch(user):  # names come from the validated users file
            raise ValueError(f"invalid user name {user!r}")
        return self.root / "users" / user


def disk_usage(root: Path) -> int:
    """Bytes of disk taken by `root` and everything under it (a job's work directory)."""
    return sum(info.st_blocks * 512 for info in _walk(root))


def remove_tree(root: Path) -> None:
    """Delete `root` and everything under it, as far as possible."""
    for _ in _walk(root):  # gives every directory back its owner's rights
        pass
    shutil.rmtree(root, ignore_errors=True)


def _walk(root: Path) -> Iterator[os.stat_result]:
    """lstat() of `root` and of everything under it, never following links.

    A compilation can take its own rights to a directory away (chmod 000), which
    would hide the files in it from disk_usage() and from deletion, so every
    directory gets its owner's rights back before it is read.
    """
    pending = [root]
    while pending:
        path = pending.pop()
        try:
            info = path.lstat()
        except OSError:  # deleted meanwhile
            continue
        yield info
        if stat.S_ISDIR(info.st_mode):
            with suppress(OSError):
                if info.st_mode & stat.S_IRWXU != stat.S_IRWXU:
                    _give_owner_rights(path)
                with os.scandir(path) as entries:
                    pending.extend(Path(entry.path) for entry in entries)


def _give_owner_rights(directory: Path) -> None:
    # Through an O_PATH descriptor: chmod() by name would follow a link the
    # compilation swapped in meanwhile.
    fd = os.open(directory, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        os.chmod(f"/proc/self/fd/{fd}", mode | stat.S_IRWXU)
    finally:
        os.close(fd)
