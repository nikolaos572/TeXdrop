# Agent guidelines

TeXdrop is a FastAPI service with a web page that compiles uploaded LaTeX
archives to PDF, one job at a time, in a sandbox (Landlock and seccomp). Python 3.11+,
SQLite for the job queue, no frontend build step.

- `src/texdrop/`: one module per concern, each starting with a docstring that says what
  it does; `static/` is the web page (plain HTML, CSS and JavaScript).
- `tests/`: pytest; `test_docker.py` builds the image and tests the container.
- `docs/`: deployment, configuration (every environment variable), API, security, development.

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # needs libarchive
.venv/bin/python -m pytest -m "not docker"                  # unit and integration tests
.venv/bin/python -m pytest -m docker                         # the Docker image
```

## Guidelines

- Keep it simple: the smallest change that solves the problem, in the style of the surrounding code.
- One responsibility per module; no duplicated logic.
- Never trust client input. Every check the web UI makes is repeated by the server, through the same code path.
- Treat compiled documents as hostile: they run in the sandbox, and nothing they produce is trusted.
- Everything works the same with and without Docker; settings are environment variables, read in `settings.py`.
- Every behaviour change comes with tests; run `python -m pytest -m "not docker"` (and `-m docker` for image changes).
- Discuss design changes before implementing them; update `README.md` and `docs/` when behaviour or configuration changes.
