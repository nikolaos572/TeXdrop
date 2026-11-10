"""Run the server, or manage the users.

    python -m texdrop                          the server
    python -m texdrop adduser NAME             add a user and print their new token; with
                                               Docker:
                                               docker exec texdrop python -m texdrop adduser NAME
    python -m texdrop reset-token NAME         give a user a new token and print it (the old
                                               one stops working)
"""

import argparse
import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

import uvicorn

from . import __version__, logs, sandbox
from .accounts import add_user, reset_token
from .app import create_app
from .errors import ConfigError
from .settings import Settings, SettingsError
from .storage import Storage

log = logging.getLogger(__name__)

# At shutdown, requests still running after this long (a client waiting with ?wait=, say)
# are cut off, so that the server stops before docker stop kills it (after 10 seconds).
SHUTDOWN_SECONDS = 5


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m texdrop", description="TeXdrop: compile LaTeX projects to PDF.")
    commands = parser.add_subparsers(dest="command")
    add_parser = commands.add_parser("adduser", help="add a user and print their new token")
    add_parser.add_argument("name", help="the user's name: 1-32 of a-z, 0-9, '_' and '-', starting with a letter or a digit")
    reset_parser = commands.add_parser("reset-token", help="give a user a new token and print it (the old one stops working)")
    reset_parser.add_argument("name", help="the user's name")
    arguments = parser.parse_args(argv)
    # The log's times are in TZ, as the dates the API shows, so in UTC without it (as in
    # Docker) rather than in the system's time zone.
    os.environ.setdefault("TZ", "UTC")
    time.tzset()
    if arguments.command == "adduser":
        sys.exit(print_new_token(add_user, arguments.name, f"Added user {arguments.name}. Their token:"))
    if arguments.command == "reset-token":
        sys.exit(print_new_token(reset_token, arguments.name, f"New token of user {arguments.name} (the old one no longer works):"))
    serve()


def serve() -> None:
    logs.log_to_console()
    logs.log_to_file(Settings.data_dir_from_env() / "logs")  # before the other settings, which may be invalid
    log.info("TeXdrop %s starting, data in %s", __version__, Settings.data_dir_from_env())
    try:
        settings = Settings.from_env()
        sandbox.forbid_new_privileges()  # for the server and everything it starts, also under a plain docker run
        app = create_app(settings)
    except (SettingsError, ConfigError) as exc:
        log.error("cannot start: %s", exc)
        sys.exit(1)
    except Exception:
        log.exception("cannot start")
        sys.exit(1)
    # One process: the queue worker is a thread inside it, so a second process would
    # compile in parallel. The async server stays responsive while the worker waits.
    # Logging is set up above; requests are logged by logs.access_log.
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=SHUTDOWN_SECONDS,
    )


def print_new_token(change: Callable[[Path, str], str], name: str, done: str) -> int:
    """Change the users file, and print the new token alone on standard output (for scripts),
    with `done` on standard error; returns the exit status."""
    try:
        settings = Settings.from_env()
        Storage(settings.data_dir).adopt_old_users_file(settings.users_file)  # as the server would at its next start
        token = change(settings.users_file, name)
    except (OSError, ValueError) as exc:  # SettingsError and ConfigError are ValueErrors
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(done, file=sys.stderr)
    print(token)
    return 0


if __name__ == "__main__":
    main()
