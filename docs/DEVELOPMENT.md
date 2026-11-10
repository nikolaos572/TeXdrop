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
that says what it does: `settings`, `users` (tokens), `watched` (the users file, re-read
when it changes), `store` (SQLite), `storage` (file layout), `archive` (safe extraction and
main-file detection), `unpack` (runs `archive` in a confined helper process), `sandbox`,
`compiler`, `jobs` (job lifecycle), `worker` (queue), `cleanup` (auto-delete), `auth`,
`sessions` (the web page's logins), `api`, `links` (transfer links), `mcp_server` (the MCP
server for AI agents, whose tools call `api`'s code), `web`, `logs`, `errors`, `app`
(wiring), `accounts` (the commands' changes to the users file) and `__main__` (the server,
and the `adduser` and `reset-token` commands).
The web page is plain HTML, CSS and JavaScript in `src/texdrop/static/`, with no build step.
What keeps documents apart is described in [SECURITY.md](SECURITY.md).

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt -e ".[dev]"   # needs libarchive (Debian: libarchive13t64)
.venv/bin/python -m pytest -m "not docker"                  # unit and integration tests
.venv/bin/python -m pytest -m docker                         # builds the image and tests the container
```

`requirements.txt` holds the exact versions the Docker image installs, so installing it with
the project runs the code and the tests on the same packages that ship. Tests that compile
real documents run when `latexmk` is installed (the `run-tests.yml` workflow installs a TeX
Live subset). Set `TEST_IMAGE` to test an existing image instead of building one. The GitHub
workflows are started manually: **Run Test Suite** runs both test groups, and **Build and
Export Docker Image** builds the image and saves it as an artifact.

## Updating dependencies

`pyproject.toml` lists the versions TeXdrop accepts. `requirements.txt` pins the exact version
of every package the image installs, the indirect ones and setuptools (which builds TeXdrop)
included; the image installs only that list, and its build fails if the list is incomplete
(`tests/test_dependencies.py` also checks that it fits `pyproject.toml`). To move to newer
versions, change a range in `pyproject.toml` if needed, and make the list again in the Python
of the image, which comes with TeX Live's:

```bash
docker run --rm -v "$PWD":/src:ro texlive/texlive:latest sh -c '
  apt-get update -qq > /dev/null && apt-get install -y -qq --no-install-recommends python3-venv > /dev/null 2>&1 &&
  mkdir /tmp/p && cp -r /src/pyproject.toml /src/README.md /src/src /tmp/p && python3 -m venv /tmp/v &&
  /tmp/v/bin/pip install -q --disable-pip-version-check --root-user-action=ignore setuptools &&
  /tmp/v/bin/pip install -q --disable-pip-version-check --root-user-action=ignore --no-build-isolation /tmp/p &&
  { grep "^#" /src/requirements.txt; /tmp/v/bin/pip freeze --all --exclude pip --exclude texdrop; }' > requirements.new
mv requirements.new requirements.txt
```

Then reinstall (`.venv/bin/pip install -r requirements.txt -e ".[dev]"`) and run both test
groups, Docker included, before committing the new list.
