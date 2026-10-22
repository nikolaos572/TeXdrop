"""Run the server: python -m texdrop"""

import logging
import os
import sys
import time

import uvicorn

from . import __version__, logs, sandbox
from .app import create_app
from .settings import Settings, SettingsError
from .users import ConfigError

log = logging.getLogger(__name__)

# At shutdown, requests still running after this long (a client waiting with ?wait=, say)
# are cut off, so that the server stops before docker stop kills it (after 10 seconds).
SHUTDOWN_SECONDS = 5


def main() -> None:
    # The log's times are in TZ, as the dates the API shows, so in UTC without it (as in
    # Docker) rather than in the system's time zone.
    os.environ.setdefault("TZ", "UTC")
    time.tzset()
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


if __name__ == "__main__":
    main()
