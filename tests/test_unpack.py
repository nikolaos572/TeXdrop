import json
import subprocess
import sys
import time

import pytest
from helpers import MINIMAL_TEX

from texdrop import sandbox, unpack
from texdrop.archive import FORMATS, ArchiveError, ArchiveInfo, extract_archive, inspect_archive
from texdrop.unpack import Unpacker

LIMIT = 10 * 1024**2


@pytest.mark.parametrize("fmt", ["zip", "7z", "tar", "tar.gz", "tar.bz2", "tar.xz"])
def test_the_helper_gives_the_same_results(make_archive, tmp_path, fmt):
    archive = make_archive({"paper/main.tex": MINIMAL_TEX, "paper/sec/a.tex": "A", "paper/fig.pdf": b"%PDF-1.4"}, fmt)
    assert Unpacker(LIMIT).inspect(archive) == inspect_archive(archive, LIMIT)
    info = Unpacker(LIMIT).extract(archive, tmp_path / "out")
    assert info == extract_archive(archive, tmp_path / "in-process", LIMIT)
    assert (info.root, info.tex_files, info.main) == ("paper/", ("main.tex", "sec/a.tex"), "main.tex")
    assert (tmp_path / "out" / "paper" / "fig.pdf").read_bytes() == b"%PDF-1.4"


def test_rejections_come_back_as_archive_errors(make_archive, tmp_path):
    not_an_archive = tmp_path / "paper.zip"
    not_an_archive.write_text("hello")
    with pytest.raises(ArchiveError, match="unsupported file type"):
        Unpacker(LIMIT).inspect(not_an_archive)
    with pytest.raises(ArchiveError, match="unsafe path"):
        Unpacker(LIMIT).extract(make_archive({"../evil.tex": "x"}, "tar"), tmp_path / "out")
    with pytest.raises(ArchiveError, match="unpacks to more than"):
        Unpacker(4000).inspect(make_archive({"main.tex": MINIMAL_TEX, "big.bin": b"\0" * 5000}))


def test_memory_limit_of_the_helper(make_archive):
    archive = make_archive({"main.tex": MINIMAL_TEX}, "tar.xz")
    assert Unpacker(LIMIT, memory_bytes=0).inspect(archive).main == "main.tex"  # 0: no limit
    with pytest.raises(ArchiveError, match="^unpacking the archive ran out of memory$"):
        Unpacker(LIMIT, memory_bytes=1024**2).inspect(archive)  # less than the helper already uses


# Runs the helper's main() with extract_archive() replaced by attempts to escape.
CONFINEMENT_PROBE = """
import os, socket, sys, threading
from texdrop import unpack

def start_process():
    if os.fork() == 0:
        os._exit(0)

def escape(archive, destination, max_bytes):
    attempts = {
        "read the archive": archive.read_bytes,
        "read another file": (archive.parent / "secret.txt").read_bytes,
        "write into the destination": lambda: (destination / "a.txt").write_text("x"),
        "write elsewhere": lambda: (archive.parent / "b.txt").write_text("x"),
        "open a UDP socket": lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM),
        "start a process": start_process,
        "start a thread": lambda: threading.Thread(target=print).start(),
    }
    for name, attempt in attempts.items():
        try:
            attempt()
            print(name, "allowed")
        except Exception:
            print(name, "denied")
    raise unpack.ArchiveError("probe done")

unpack.extract_archive = escape
unpack.main(sys.argv[1:])
"""


@pytest.mark.skipif(not sandbox.landlock_abi(), reason="Landlock is not available on this kernel")
@pytest.mark.skipif(not sandbox.syscall_filter_supported(), reason="no system-call filter here")
def test_the_helper_is_confined(make_archive, tmp_path):
    archive = make_archive({"main.tex": MINIMAL_TEX})
    (tmp_path / "secret.txt").write_text("TOP-SECRET")
    (tmp_path / "out").mkdir()
    result = subprocess.run(
        [sys.executable, "-I", "-c", CONFINEMENT_PROBE, str(archive), str(LIMIT), str(4 * 1024**3), str(tmp_path / "out")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    *attempts, report = result.stdout.splitlines() or [""]
    assert attempts == [
        "read the archive allowed",
        "read another file denied",
        "write into the destination allowed",
        "write elsewhere denied",
        "open a UDP socket denied",
        "start a process denied",
        "start a thread denied",
    ], result.stderr
    assert json.loads(report) == {"error": "probe done"}


GOOD_REPORT = {"format": "zip", "root": "paper/", "tex_files": ["main.tex", "sec/a.tex"], "main": "main.tex"}


def test_report_of_the_helper():
    info = unpack._read_report(json.dumps(GOOD_REPORT).encode())
    assert info == ArchiveInfo(FORMATS["zip"], "paper/", ("main.tex", "sec/a.tex"), "main.tex")
    assert unpack._read_report(json.dumps(GOOD_REPORT | {"root": "", "main": None}).encode()).main is None
    with pytest.raises(ArchiveError, match="^the archive has no files$"):
        unpack._read_report(b'{"error": "the archive has no files"}')


@pytest.mark.parametrize(
    "report",
    [
        b"",
        b"not json",
        b"\xff\xfe",
        b"[]",
        b'"text"',
        b'{"error": 5}',
        *(
            json.dumps(GOOD_REPORT | change).encode()
            for change in [
                {"format": "rar"},
                {"format": ["zip"]},
                {"root": "../"},
                {"root": "a/../"},
                {"root": "/etc/"},
                {"root": "paper"},
                {"root": "a\nb/"},
                {"root": 5},
                {"tex_files": ["a$(reboot).tex"]},
                {"tex_files": ["../../x.tex"]},
                {"tex_files": "main.tex"},
                {"tex_files": [1]},
                {"tex_files": 1},
                {"main": "other.tex"},
                {"main": ["main.tex"]},
            ]
        ),
    ],
)
def test_untrusted_reports_are_refused(report):
    with pytest.raises(unpack._BadReport):
        unpack._read_report(report)


def test_a_failing_helper_is_an_error(make_archive, monkeypatch, caplog):
    script = "import sys; print('{\"root\": \"../\"}'); print('crashed', file=sys.stderr); sys.exit(3)"
    monkeypatch.setattr(unpack, "_command", lambda *args: [sys.executable, "-c", script])
    with pytest.raises(ArchiveError, match="^the archive could not be unpacked$"):
        Unpacker(LIMIT).inspect(make_archive({"main.tex": MINIMAL_TEX}))
    assert "archive helper failed (exit code 3): crashed" in caplog.text


def test_a_slow_helper_is_stopped(make_archive, monkeypatch):
    monkeypatch.setattr(unpack, "_command", lambda *args: [sys.executable, "-c", "import time; time.sleep(60)"])
    start = time.monotonic()
    with pytest.raises(ArchiveError, match="^unpacking the archive took longer than 0.05 minutes$"):
        Unpacker(LIMIT, timeout_seconds=3).inspect(make_archive({"main.tex": MINIMAL_TEX}))
    assert time.monotonic() - start < 10
