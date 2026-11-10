from pathlib import Path

import pytest

from texdrop.settings import Settings, SettingsError


def test_defaults():
    settings = Settings.from_env({})
    assert settings.data_dir == Path("/data")
    assert settings.users_file == Path("/data/users.json")
    assert settings.timezone.key == "UTC"
    assert settings.auto_delete_days == 0
    assert settings.compile_timeout_seconds == 900
    assert settings.max_upload_bytes == 100 * 1024**2
    assert settings.max_extracted_bytes == 1000 * 1024**2
    # No limits of our own: whatever the container may use.
    assert (settings.compile_memory_bytes, settings.compile_disk_bytes, settings.max_user_storage_bytes) == (0, 0, 0)
    assert (settings.unpack_timeout_seconds, settings.unpack_memory_bytes) == (300, 4096 * 1024**2)
    assert (settings.enable_mcp, settings.link_expiry_seconds) == (False, 3600)  # off unless asked for
    assert settings.forwarded_allow_ips == "127.0.0.1"  # a proxy on this machine, so none in Docker
    assert (settings.host, settings.port) == ("0.0.0.0", 8000)


def test_values_from_environment():
    settings = Settings.from_env(
        {
            "DATA_DIR": "/srv/latex",
            "USERS_FILE": "/srv/shared/users.json",
            "TZ": "Europe/Athens",
            "AUTO_DELETE_DAYS": "15",
            "COMPILE_TIMEOUT_MINUTES": "150",
            "MAX_UPLOAD_MB": " 20 ",
            "COMPILE_MEMORY_MB": "4096",
            "COMPILE_DISK_MB": "2048",
            "MAX_USER_STORAGE_MB": "500",
            "UNPACK_TIMEOUT_MINUTES": "2",
            "UNPACK_MEMORY_MB": "0",
            "ENABLE_MCP": "1",
            "LINK_EXPIRY_MINUTES": "15",
            "FORWARDED_ALLOW_IPS": "10.0.0.1",
            "HOST": "127.0.0.1",
            "PORT": "8200",
        }
    )
    assert settings.data_dir == Path("/srv/latex")
    assert settings.users_file == Path("/srv/shared/users.json")
    assert settings.timezone.key == "Europe/Athens"
    assert settings.auto_delete_days == 15
    assert settings.compile_timeout_seconds == 150 * 60
    assert settings.max_upload_bytes == 20 * 1024**2
    assert settings.compile_memory_bytes == 4096 * 1024**2
    assert settings.compile_disk_bytes == 2048 * 1024**2
    assert settings.max_user_storage_bytes == 500 * 1024**2
    assert (settings.unpack_timeout_seconds, settings.unpack_memory_bytes) == (120, 0)
    assert (settings.enable_mcp, settings.link_expiry_seconds) == (True, 15 * 60)
    assert settings.forwarded_allow_ips == "10.0.0.1"
    assert (settings.host, settings.port) == ("127.0.0.1", 8200)


def test_empty_values_mean_default():
    settings = Settings.from_env({"AUTO_DELETE_DAYS": "", "TZ": "", "DATA_DIR": ""})
    assert (settings.auto_delete_days, settings.data_dir) == (0, Path("/data"))


def test_relative_data_dir_is_made_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # compilations run elsewhere, where a relative path would point to nothing
    settings = Settings.from_env({"DATA_DIR": "data"})
    assert (settings.data_dir, settings.users_file) == (tmp_path / "data", tmp_path / "data" / "users.json")
    assert Settings.from_env({"USERS_FILE": "users.json"}).users_file == tmp_path / "users.json"


@pytest.mark.parametrize(("value", "expected"), [("", False), ("0", False), ("no", False), ("1", True), ("True", True)])
def test_allow_unsandboxed_flag(value, expected):
    assert Settings.from_env({"ALLOW_UNSANDBOXED": value}).allow_unsandboxed is expected


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("AUTO_DELETE_DAYS", "-1", "at least 0"),
        ("AUTO_DELETE_DAYS", "1.5", "must be an integer"),
        ("COMPILE_TIMEOUT_MINUTES", "0", "at least 1"),
        ("MAX_UPLOAD_MB", "lots", "must be an integer"),
        ("COMPILE_MEMORY_MB", "-1", "at least 0"),
        ("COMPILE_DISK_MB", "2g", "must be an integer"),
        ("MAX_USER_STORAGE_MB", "-5", "at least 0"),
        ("UNPACK_TIMEOUT_MINUTES", "0", "at least 1"),
        ("UNPACK_MEMORY_MB", "4g", "must be an integer"),
        ("PORT", "0", "at least 1"),
        ("PORT", "65536", "at most 65535"),
        ("TZ", "Mars/Olympus", "IANA time zone"),
        ("TZ", "../../etc/passwd", "IANA time zone"),
        ("ALLOW_UNSANDBOXED", "maybe", "must be 1 or 0"),
        ("ENABLE_MCP", "on", "must be 1 or 0"),
        ("LINK_EXPIRY_MINUTES", "0", "at least 1"),
        ("LINK_EXPIRY_MINUTES", "1h", "must be an integer"),
        # uvicorn would ignore these without a word: a network with host bits, a name, a list with *.
        ("FORWARDED_ALLOW_IPS", "172.17.0.1/16", "FORWARDED_ALLOW_IPS must be IP addresses or networks"),
        ("FORWARDED_ALLOW_IPS", "proxy.example.com", "got 'proxy.example.com'"),
        ("FORWARDED_ALLOW_IPS", "10.0.0.1,*", "or \\* alone, got '\\*'"),
        ("FORWARDED_ALLOW_IPS", "10.0.0.1,,10.0.0.2", "got ''"),
    ],
)
def test_invalid_values_are_rejected(name, value, message):
    with pytest.raises(SettingsError, match=message):
        Settings.from_env({name: value})


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", "127.0.0.1"), ("*", "*"), (" 172.17.0.1 ", "172.17.0.1"), ("172.17.0.1, 172.18.0.0/16,::1", "172.17.0.1,172.18.0.0/16,::1")],
)
def test_the_trusted_proxies(value, expected):
    assert Settings.from_env({"FORWARDED_ALLOW_IPS": value}).forwarded_allow_ips == expected
