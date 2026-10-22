"""Runtime settings, read once from environment variables at startup."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class SettingsError(ValueError):
    """An environment variable has an invalid value."""


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    timezone: ZoneInfo
    auto_delete_days: int  # 0 keeps jobs forever
    compile_timeout_seconds: float
    max_upload_bytes: int
    compile_memory_bytes: int = 0  # address space of each process of a compilation; 0: no limit
    compile_disk_bytes: int = 0  # a compilation's work directory; 0: no limit
    max_user_storage_bytes: int = 0  # each user's archives, PDFs and logs; 0: no limit
    unpack_timeout_seconds: float = 300  # inspecting or unpacking one archive
    unpack_memory_bytes: int = 4 * 1024**3  # address space of the archive helper; 0: no limit
    allow_unsandboxed: bool = False  # start even where the sandbox lacks some of its protections
    host: str = "0.0.0.0"  # where the server listens (without Docker; Docker maps the port instead)
    port: int = 8000

    @property
    def max_extracted_bytes(self) -> int:
        """Upper bound for the total size of an archive's unpacked files."""
        return 10 * self.max_upload_bytes

    @staticmethod
    def data_dir_from_env(env: Mapping[str, str] = os.environ) -> Path:
        """DATA_DIR alone, which is valid even when other variables are not. Made absolute,
        since compilations run in another working directory."""
        return Path(env.get("DATA_DIR", "").strip() or "/data").absolute()

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        return cls(
            data_dir=cls.data_dir_from_env(env),
            timezone=_timezone(env.get("TZ", "UTC")),
            auto_delete_days=_integer(env, "AUTO_DELETE_DAYS", default=0, minimum=0),
            compile_timeout_seconds=60 * _integer(env, "COMPILE_TIMEOUT_MINUTES", default=15, minimum=1),
            max_upload_bytes=1024**2 * _integer(env, "MAX_UPLOAD_MB", default=100, minimum=1),
            compile_memory_bytes=1024**2 * _integer(env, "COMPILE_MEMORY_MB", default=0, minimum=0),
            compile_disk_bytes=1024**2 * _integer(env, "COMPILE_DISK_MB", default=0, minimum=0),
            max_user_storage_bytes=1024**2 * _integer(env, "MAX_USER_STORAGE_MB", default=0, minimum=0),
            unpack_timeout_seconds=60 * _integer(env, "UNPACK_TIMEOUT_MINUTES", default=5, minimum=1),
            unpack_memory_bytes=1024**2 * _integer(env, "UNPACK_MEMORY_MB", default=4096, minimum=0),
            allow_unsandboxed=_flag(env, "ALLOW_UNSANDBOXED"),
            host=env.get("HOST", "").strip() or "0.0.0.0",
            port=_integer(env, "PORT", default=8000, minimum=1, maximum=65535),
        )


def _integer(env: Mapping[str, str], name: str, *, default: int, minimum: int, maximum: int | None = None) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{name} must be an integer, got {raw!r}") from None
    if value < minimum:
        raise SettingsError(f"{name} must be at least {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise SettingsError(f"{name} must be at most {maximum}, got {value}")
    return value


def _flag(env: Mapping[str, str], name: str) -> bool:
    raw = env.get(name, "").strip().lower()
    if raw in ("", "0", "false", "no"):
        return False
    if raw in ("1", "true", "yes"):
        return True
    raise SettingsError(f"{name} must be 1 or 0, got {raw!r}")


def _timezone(name: str) -> ZoneInfo:
    # TZ may be a POSIX path like ":/etc/localtime"; only IANA names are supported here.
    try:
        return ZoneInfo(name.strip() or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        raise SettingsError(f"TZ must be an IANA time zone such as 'Europe/Athens', got {name!r}") from None
