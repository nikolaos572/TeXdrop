"""Test helpers shared by several test modules (fixtures are in conftest.py)."""

from __future__ import annotations

import io
import os
import tarfile
import zipfile
from pathlib import Path

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


def run_queue(app: FastAPI) -> None:
    """Process every queued job, oldest first, in the calling thread."""
    while job := app.state.service.claim_next():
        app.state.worker.process(job)


def submit(client: TestClient, archive: Path, **form: str):
    with archive.open("rb") as file:
        return client.post("/api/jobs", files={"file": (archive.name, file)}, data=form)
