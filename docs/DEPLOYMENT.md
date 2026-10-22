# Deployment

How to build and run the service. The users file and the settings are described in
[CONFIGURATION.md](CONFIGURATION.md).

## Requirements

- Linux, with or without Docker. The Docker image takes about 9 GB (the full TeX Live).
- For the full sandbox: Linux 6.12 or newer, on x86-64 or ARM64, with Landlock enabled
  (`cat /sys/kernel/security/lsm` must list `landlock`). Elsewhere the server names what is
  missing and starts only with `ALLOW_UNSANDBOXED=1` (see
  [Older kernels](SECURITY.md#older-kernels)).

## Building

```bash
docker build --pull -t texdrop .
```

Rebuild now and then with `--pull`: that brings in the security fixes of TeX Live and of the
programs it runs (Ghostscript), and of the archive library.

Like Overleaf, the image has Ghostscript (EPS figures, the LaTeX + dvipdf route) but no Java,
so packages that ask Java for information (`texosquery`, used for example by `datetime2` for
the locale) get none, and their documents compile without it.

## Running

The container runs as uid 1001, which must own the data directory:

```bash
mkdir data                                   # then create data/config.json (see CONFIGURATION.md)
sudo chown -R 1001:1001 data

docker run -d --name texdrop -p 8000:8000 -v "$PWD/data:/data" \
  -e TZ=Europe/Athens -e AUTO_DELETE_DAYS=15 \
  --cap-drop ALL --security-opt no-new-privileges --restart unless-stopped \
  texdrop
```

or `docker compose up -d` with [`compose.yaml`](../compose.yaml). Everything the service stores
is in the data directory, so the container can be removed and started again from a new
image (`docker rm -f texdrop`, then the same `docker run`) without losing anything.

`docker run` and compose are equivalent: `compose.yaml` only writes down the same options,
and every setting is an environment variable, which `-e` sets as well. Safety does not
depend on extra options either: the image has its own init process (tini), which passes
`docker stop` on to the server and reaps processes left behind in the container, and the
server makes itself and everything it starts unable to gain privileges. `--init`, `--cap-drop ALL` and
`--security-opt no-new-privileges` are therefore not needed; the last two add a second
layer at no cost. Only the command line (or `compose.yaml`) can give the port, the data
directory, the restart policy (without `--restart`, the container stays stopped after a
reboot) and the [container's limits](#container-limits).

At startup the server checks the sandbox and logs "every protection of the sandbox is
active". If a protection is missing, it names it and does not start (see
[Older kernels](SECURITY.md#older-kernels)). At shutdown (`docker stop`), it stops the
running compilation, which starts again from the beginning at the next start, and cuts off
requests still open after 5 seconds, such as a client waiting for a job with `?wait=`.

Use HTTPS (a reverse proxy in front of the service) when it is reachable beyond a trusted
network, and tell the server about the proxy with `FORWARDED_ALLOW_IPS`
(see [CONFIGURATION.md](CONFIGURATION.md#environment-variables)).

To let AI agents such as Claude Code use the service, add `-e ENABLE_MCP=1`: see
[MCP.md](MCP.md).

### Without Docker

Install Python 3.11+, libarchive (Debian/Ubuntu: `libarchive13t64`), TeX Live with `latexmk`,
and Ghostscript for EPS figures. TeX may be installed anywhere, also in a home directory:
the server finds it through `PATH` and lets compilations read its directories (logged at
startup). Then, as an unprivileged user (best a dedicated one):

```bash
python3 -m venv .venv && .venv/bin/pip install .
mkdir -p ~/latex-data                        # then create ~/latex-data/config.json
DATA_DIR=~/latex-data PORT=8000 .venv/bin/python -m texdrop
```

The data directory must not lie inside a system directory (`/usr`, `/etc`, `/opt`, ...) or
TeX's directories, which compilations can read; the server refuses to start otherwise.
Without the Docker image's prepared caches, Biber and the first LuaLaTeX run of each job
take a few seconds longer.

## Data directory

```
data/
├── config.json                        users and tokens (you create this)
├── jobs.db                            job metadata (SQLite)
├── users/<user>/inputs/<id>.zip       uploaded archives
├── users/<user>/outputs/<id>.pdf      compiled documents
├── users/<user>/outputs/<id>.log      compilation logs
├── logs/server.log                    the server's log (older parts in server.log.1 to .4)
└── tmp/                               work space, emptied at startup
```

## Container limits

Docker can limit the whole container: its memory, CPU and number of processes. These are
`docker run` options (or `compose.yaml` keys), not environment variables. By default there
are none, and the container may use the whole machine.

| `docker run` | `compose.yaml` | Limits |
|---|---|---|
| `--memory 6g --memory-swap 6g` | `mem_limit: 6g` and `memswap_limit: 6g` | Memory in use (and memory plus swap: the same value means no swap) |
| `--cpus 4` | `cpus: 4` | CPU cores |
| `--pids-limit 1024` | `pids_limit: 1024` | Processes and threads |

For example, on a machine with 8 GB of memory that also runs other services:

```bash
docker run -d --name texdrop -p 8000:8000 -v "$PWD/data:/data" \
  --memory 6g --memory-swap 6g --cpus 4 --pids-limit 1024 \
  --cap-drop ALL --security-opt no-new-privileges --restart unless-stopped \
  texdrop
```

When the container's memory runs out, the kernel stops a process in it. Compilations are
marked as its first choice, so the compilation fails (its error names the program that was
stopped) and the server keeps running. A process limit stops a document that starts
processes without end; leave room for the server, which uses up to about 50 threads. One
compilation runs at a time, mostly on one core, so a CPU limit mainly protects the other
services on the machine.

## Logs

The server logs to its console, which `docker logs texdrop` shows (without Docker: the
terminal, or the journal of the service manager), and to `logs/server.log` in the data
directory, which stays when the container is replaced. At 10 MB the file becomes
`server.log.1`, the previous `.1` becomes `.2`, and so on; files beyond `server.log.4` are
deleted, so the log takes at most 50 MB.

Each line has the time (in the `TZ` time zone), the level and one event:

```
2026-10-22 16:02:59 +0300 INFO    TeXdrop 1.0.0 starting, data in /data
2026-10-22 16:02:59 +0300 INFO    every protection of the sandbox is active
2026-10-22 16:05:11 +0300 INFO    10.0.0.5 alice POST /api/jobs 201 212 ms
2026-10-22 16:05:20 +0300 INFO    job 42 (alice): main.tex with pdflatex, done in 8.3 s
2026-10-22 16:05:23 +0300 INFO    10.0.0.5 alice GET /api/jobs/42/pdf 200 3 ms
2026-10-22 16:07:02 +0300 INFO    10.0.0.7 - POST /api/session 401 1 ms: invalid token
2026-10-22 16:09:40 +0300 INFO    job 43 (bob): paper.tex with xelatex, failed after 2.1 s: ./paper.tex:12: Undefined control sequence.
```

- Starting and stopping: the version, what the sandbox can use, and why the server does not
  start when a setting or the system is not right.
- One line per request that changes something (an upload, a deletion, a login) or downloads
  a file: the client's address, the user (`-` when unknown), the request, its status and
  duration, and why it failed. Successful reads are not logged: the web page repeats them
  every few seconds, and Docker its health check every 30 seconds. Behind a reverse proxy,
  the address is the proxy's unless `FORWARDED_ALLOW_IPS` names it.
- One line per finished job, with the first line of the error when it failed (the job's log
  has the rest).
- Unexpected errors, at level `ERROR` and with their traceback.

Tokens never appear in the log, nor do the secrets of the MCP server's transfer links
(logged as `/api/links/***`, with the reason when a link is refused).
