"""Layout of the data directory (the volume mounted into the container).

    <data>/users.json                        users and tokens (USERS_FILE)
    <data>/sessions.json                     the web page's sessions (sessions.py)
    <data>/jobs.db                           job metadata (SQLite)
    <data>/users/<user>/inputs/<id><ext>     uploaded archives
    <data>/users/<user>/outputs/<id>.pdf     compiled documents
    <data>/users/<user>/outputs/<id>.log     compilation logs
    <data>/logs/server.log                   the server's log (written by the logs module)
    <data>/tmp/                              uploads in progress and compile work dirs

It also measures and removes compile work dirs, whose content is hostile, and writes the
files with secrets that the administrator's commands change.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import shutil
import stat
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .users import NAME_PATTERN

USERS_FILE = "users.json"
OLD_USERS_FILE = "config.json"  # where TeXdrop kept the users before it had USERS_FILE
PRIVATE_MODE = 0o600  # of a new file with secrets


class Storage:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.db_path = root / "jobs.db"
        self.tmp_dir = root / "tmp"
        self.sessions_file = root / "sessions.json"

    def prepare(self) -> None:
        """Create the layout and discard leftovers of an interrupted run."""
        (self.root / "users").mkdir(parents=True, exist_ok=True)
        remove_tree(self.tmp_dir)
        self.tmp_dir.mkdir()

    def adopt_old_users_file(self, users_file: Path) -> Path | None:
        """Rename the users file of older versions, config.json, to users.json, when the users
        file is the default one and does not exist yet. Returns the old path if it did."""
        old = self.root / OLD_USERS_FILE
        if users_file != self.root / USERS_FILE or os.path.lexists(users_file) or not old.is_file():
            return None
        try:
            old.rename(users_file)
        except OSError as exc:
            raise ConfigError(f"cannot rename the users file {old} to {users_file} ({exc.strerror}): rename it yourself") from exc
        return old

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


@contextmanager
def locked_file(path: Path) -> Iterator[Path]:
    """Let the administrator's commands on a file take turns, so that none undoes another's
    change. Gives the file's real path: a link stays a link, and what it points to changes.
    The lock is on the directory because the file itself gets replaced."""
    path = path.resolve()
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield path
    finally:
        os.close(descriptor)  # releases the lock


def write_secret_json(path: Path, value: dict[str, Any], *, fresh: bool = False) -> None:
    """Replace a file with secrets (users.json, sessions.json) atomically, so that the server
    never reads a part of it. The new file keeps the owner and mode of the old one. Without
    one, or when `fresh`, it is private to the directory's owner, so that the server can still
    read it when root runs the command."""
    current = None
    if not fresh:
        with suppress(FileNotFoundError):
            current = path.stat()
    owner = current or path.parent.stat()
    mode = stat.S_IMODE(current.st_mode) if current else PRIVATE_MODE
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")  # private
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            with suppress(OSError):  # only root can give a file to another user
                os.fchown(file.fileno(), owner.st_uid, owner.st_gid)
            os.fchmod(file.fileno(), mode)
            dump_json(value, file)
        try:
            os.replace(temporary, path)
        except OSError as exc:
            if exc.errno != errno.EBUSY:
                raise
            _write_over(path, value)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _write_over(path: Path, value: dict[str, Any]) -> None:
    """Write over a file Docker mounted by itself, which cannot be replaced."""
    with path.open("r+", encoding="utf-8") as file:
        dump_json(value, file)
        file.truncate()


def dump_json(value: dict[str, Any], file: Any) -> None:
    """Write `value` to an open file, and make sure that it reaches the disk."""
    json.dump(value, file, indent=2, ensure_ascii=False)
    file.write("\n")
    file.flush()
    os.fsync(file.fileno())
