from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from helpers import FAKE_LATEXMK, TOKENS, write_archive

from texdrop.app import create_app
from texdrop.compiler import LatexCompiler
from texdrop.settings import Settings

ArchiveFactory = Callable[..., Path]


@pytest.fixture
def make_archive(tmp_path: Path) -> ArchiveFactory:
    """make_archive({"main.tex": "..."}, "tar.gz") -> path of a new archive."""
    counter = iter(range(1_000_000))

    def make(files: dict[str, str | bytes], fmt: str = "zip") -> Path:
        return write_archive(tmp_path / f"archive-{next(counter)}.{fmt}", files, fmt)

    return make


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"users": TOKENS}))
    return Settings(
        data_dir=data,
        timezone=ZoneInfo("Europe/Athens"),
        auto_delete_days=0,
        compile_timeout_seconds=30,
        max_upload_bytes=2 * 1024**2,
        allow_unsandboxed=True,  # the sandbox is still used where available; test_sandbox.py covers it
    )


@pytest.fixture
def data(tmp_path: Path) -> Path:
    """A data directory with the test users, for the real server (helpers.start_server)."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"users": TOKENS}))
    return data


@pytest.fixture
def fake_compiler(tmp_path: Path) -> LatexCompiler:
    return LatexCompiler(30, latexmk=("sh", "-c", FAKE_LATEXMK, "latexmk"), template=tmp_path / "no-template")


@pytest.fixture
def app(settings: Settings, fake_compiler: LatexCompiler) -> FastAPI:
    """The app without its background threads (tests drive the queue with helpers.run_queue)."""
    return create_app(settings, fake_compiler)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """A client authenticated as alice."""
    return TestClient(app, headers={"Authorization": f"Bearer {TOKENS['alice']}"})
