import dataclasses
import logging
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from texdrop.app import create_app
from texdrop.settings import SettingsError
from texdrop.users import ConfigError

INCOMPLETE = [(0, True, "no Landlock"), (3, True, "signals"), (6, False, "UDP")]  # Landlock ABI, filter


@pytest.mark.parametrize(("abi", "works", "expected"), INCOMPLETE)
def test_refuses_to_start_with_an_incomplete_sandbox(
    settings, fake_compiler, monkeypatch, caplog, abi, works, expected
):
    monkeypatch.setattr("texdrop.sandbox.landlock_abi", lambda: abi)
    monkeypatch.setattr("texdrop.sandbox.syscall_filter_works", lambda: works)
    with pytest.raises(SettingsError, match="Linux 6.12 or newer.*ALLOW_UNSANDBOXED=1"):
        create_app(dataclasses.replace(settings, allow_unsandboxed=False), fake_compiler)
    errors = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert any(error.startswith("missing protection: ") and expected in error for error in errors)


@pytest.mark.parametrize(("abi", "works", "expected"), INCOMPLETE)
def test_override_starts_with_reduced_protection(settings, fake_compiler, monkeypatch, caplog, abi, works, expected):
    monkeypatch.setattr("texdrop.sandbox.landlock_abi", lambda: abi)
    monkeypatch.setattr("texdrop.sandbox.syscall_filter_works", lambda: works)
    caplog.set_level(logging.INFO)
    with TestClient(create_app(settings, fake_compiler)) as client:  # ALLOW_UNSANDBOXED=1
        assert client.get("/api/health").status_code == 200
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert any(warning.startswith("reduced protection: ") and expected in warning for warning in warnings)
    assert warnings[-1] == "running with reduced protection, as ALLOW_UNSANDBOXED=1 allows"
    assert "every protection of the sandbox is active" not in caplog.text


@pytest.mark.skipif(os.geteuid() == 0, reason="running as root is itself a missing protection")
def test_starts_with_the_full_sandbox(settings, fake_compiler, monkeypatch, caplog):
    monkeypatch.setattr("texdrop.sandbox.landlock_abi", lambda: 6)
    monkeypatch.setattr("texdrop.sandbox.syscall_filter_works", lambda: True)
    caplog.set_level(logging.INFO)
    with TestClient(create_app(dataclasses.replace(settings, allow_unsandboxed=False), fake_compiler)):
        pass
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []
    assert "every protection of the sandbox is active" in caplog.text


@pytest.mark.parametrize("data_dir", ["/opt/latex-data", "/usr/local/data", "/etc/latex"])
def test_data_dir_must_not_be_readable_by_compilations(settings, fake_compiler, data_dir):
    with pytest.raises(SettingsError, match="directory that compilations can read"):
        create_app(dataclasses.replace(settings, data_dir=Path(data_dir)), fake_compiler)


def test_data_dir_must_not_be_inside_tex(settings, fake_compiler, monkeypatch):
    monkeypatch.setattr("texdrop.app.tex_directories", lambda: (str(settings.data_dir.parent),))
    with pytest.raises(SettingsError, match="directory that compilations can read"):
        create_app(settings, fake_compiler)


def test_invalid_users_file_stops_startup(settings, fake_compiler):
    (settings.data_dir / "config.json").write_text('{"users": {"Bad Name": "0123456789abcdef"}}')
    with pytest.raises(ConfigError):
        create_app(settings, fake_compiler)


def test_startup_creates_the_layout_and_clears_leftovers(settings, fake_compiler):
    leftover = settings.data_dir / "tmp" / "job-7"
    leftover.mkdir(parents=True)
    create_app(settings, fake_compiler)
    assert (settings.data_dir / "users").is_dir()
    assert list((settings.data_dir / "tmp").iterdir()) == []


def test_api_documentation_is_available(app):
    paths = TestClient(app).get("/openapi.json").json()["paths"]
    assert {"/api/jobs", "/api/jobs/{job_id}", "/api/inspect", "/api/jobs/{job_id}/pdf"} <= set(paths)
