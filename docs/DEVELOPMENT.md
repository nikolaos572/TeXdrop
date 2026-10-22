# Development

## How it works

One process runs [FastAPI](https://fastapi.tiangolo.com/) under uvicorn plus a
background worker thread. The worker takes the oldest queued job, has a helper process
extract it into a fresh directory, and runs `latexmk` there as a separate process. That process does
the CPU work on its own core while the thread just waits, so requests are answered
immediately even during long compilations. Exactly one job compiles at a time.

Job metadata is kept in SQLite (part of Python, a single file, no server). It gives
atomic queue operations, so a job deleted while compiling or two users submitting at
the same moment cannot corrupt the state. It uses WAL mode with one short-lived
connection per operation, and files are moved inside the same transactions. Jobs
interrupted by a restart are queued again.

The code is in `src/texdrop/`, one module per concern, each starting with a docstring
that says what it does: `settings`, `users` (tokens), `store` (SQLite), `storage` (file
layout), `archive` (safe extraction and main-file detection), `unpack` (runs `archive` in a
confined helper process), `sandbox`, `compiler`, `jobs` (job lifecycle), `worker` (queue),
`cleanup` (auto-delete), `auth`, `api`, `web`, `logs` and `app` (wiring). The web page is
plain HTML, CSS and JavaScript in `src/texdrop/static/`, with no build step.
What keeps documents apart is described in [SECURITY.md](SECURITY.md).

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # needs libarchive (Debian: libarchive13t64)
.venv/bin/python -m pytest -m "not docker"                  # unit and integration tests
.venv/bin/python -m pytest -m docker                         # builds the image and tests the container
```

Tests that compile real documents run when `latexmk` is installed (the
`run-tests.yml` workflow installs a TeX Live subset). The GitHub workflows are started
manually: **Run Test Suite** runs both test groups, and **Build and Export Docker
Image** builds the image and saves it as an artifact.
