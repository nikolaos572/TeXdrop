# Deployment

How to build and run the service. The users and the settings are described in
[CONFIGURATION.md](CONFIGURATION.md).

## Requirements

- Linux, with or without Docker. The Docker image takes about 6 GB (the full TeX Live).
- For the full sandbox: Linux 6.12 or newer, on x86-64 or ARM64, with Landlock enabled
  (`cat /sys/kernel/security/lsm` must list `landlock`). Elsewhere the server names what is
  missing and starts only with `ALLOW_UNSANDBOXED=1` (see
  [Older kernels](SECURITY.md#older-kernels)).

## Building

```bash
docker build --pull -t texdrop .
```

Rebuild now and then with `--pull`: that brings in the security fixes of TeX Live and of the
programs it runs (Ghostscript), of Python and of the archive library. The Python packages are
pinned to exact versions in `requirements.txt`, so every build installs the same ones; they
change with new versions of TeXdrop (see
[Updating dependencies](DEVELOPMENT.md#updating-dependencies)).

Like Overleaf, the image has Ghostscript (EPS figures, the LaTeX + dvipdf route) but no Java,
so packages that ask Java for information (`texosquery`, used for example by `datetime2` for
the locale) get none, and their documents compile without it.

## Running

```bash
docker run -d --name texdrop \
  --restart unless-stopped \
  --cap-drop ALL --security-opt no-new-privileges \
  -p 8000:8000 \
  -e TZ=Europe/Athens \
  -e AUTO_DELETE_DAYS=15 \
  -v texdrop-data:/data \
  texdrop

docker exec texdrop python -m texdrop adduser alice    # prints alice's token
```

Until a user exists, the server refuses every request; [CONFIGURATION.md](CONFIGURATION.md#users)
has more on the users. Everything the service stores is in its data directory, the volume
mounted at `/data`, so the container can be removed and started again from a new image
(`docker rm -f texdrop`, then the same `docker run`) without losing anything. Mount it at
`/data` and nowhere else: a volume mounted at another path stays empty, and the data ends up
in an anonymous volume that a new container does not reuse.

### A directory on the host

The command above keeps the data in the named volume `texdrop-data`, which Docker creates and
gives to the container's user on first use (as root, `docker volume inspect -f
'{{.Mountpoint}}' texdrop-data` shows where it is). To keep the data in a directory of your
choice instead, which the rest of these docs call `data/`, give it to the container's user
first: uid and gid **61001**, a number that matches no account of the host (no user or group
with that number needs to exist). Otherwise Docker creates the directory owned by root, and
the server cannot write to it.

```bash
mkdir data
sudo chown -R 61001:61001 data
docker run ... -v "$PWD/data:/data" ... texdrop
```

[`compose.yaml`](../compose.yaml) does this: `docker compose up -d`, then the same command
with `docker compose exec texdrop` instead of `docker exec texdrop`. Images before November
2026 ran as uid 1001: when you upgrade from one, give the data directory to 61001 once, with
the same `sudo chown -R`, before you start the new container. With rootless Docker or
`userns-remap`, the host sees the container's ids shifted, so a `chown` on the host gives the
wrong owner; give the directory to 61001 from inside a container instead:

```bash
docker run --rm --user 0 -v "$PWD/data:/data" texdrop chown -R 61001:61001 /data
```

### Options

`docker run` and compose are equivalent: `compose.yaml` only writes down the same options,
and every setting is an environment variable, which `-e` sets as well. Safety does not
depend on extra options either: the image has its own init process (tini), which passes
`docker stop` on to the server and reaps processes left behind in the container, the server
runs as an unprivileged user and needs no capabilities, and it makes itself and everything it
starts unable to gain privileges. `--init` (which adds the same init process), `--cap-drop ALL`
and `--security-opt no-new-privileges` are therefore not needed, and they do no harm. The last
two add a second layer at no cost, as in the commands of these docs and in `compose.yaml`:
they also cover the commands you start with `docker exec`, which the server's own setting
does not reach. Only the command line (or `compose.yaml`) can give the port, the data
directory, the restart policy (without `--restart`, the container stays stopped after a
reboot) and the [container's limits](#container-limits).

At startup the server checks the sandbox and logs "every protection of the sandbox is
active". If a protection is missing, it names it and does not start (see
[Older kernels](SECURITY.md#older-kernels)). At shutdown (`docker stop`), it stops the
running compilation, which starts again from the beginning at the next start, and cuts off
requests still open after 5 seconds, such as a client waiting for a job with `?wait=`.

Use HTTPS (a reverse proxy in front of the service) when it is reachable beyond a trusted
network; it works without settings, and better with `PUBLIC_BASE_URL` (see
[Reverse proxies](#reverse-proxies)).

To let AI agents such as Claude Code use the service, add `-e ENABLE_MCP=1`: see
[MCP.md](MCP.md).

### Without Docker

Install Python 3.11+, libarchive (Debian/Ubuntu: `libarchive13t64`), TeX Live with `latexmk`,
and Ghostscript for EPS figures. TeX may be installed anywhere, also in a home directory:
the server finds it through `PATH` and lets compilations read its directories (logged at
startup). Then, as an unprivileged user (best a dedicated one):

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt .   # the versions the image has
mkdir -p ~/latex-data
DATA_DIR=~/latex-data .venv/bin/python -m texdrop adduser alice    # prints alice's token
DATA_DIR=~/latex-data PORT=8000 .venv/bin/python -m texdrop
```

The data directory must not lie inside a system directory (`/usr`, `/etc`, `/opt`, ...) or
TeX's directories, which compilations can read; the server refuses to start otherwise.
Without the Docker image's prepared caches, Biber and the first LuaLaTeX run of each job
take a few seconds longer.

## Data directory

```
data/
├── users.json                         users and tokens (python -m texdrop adduser creates this)
├── sessions.json                      the web page's sessions, as SHA-256 digests (the server writes this)
├── jobs.db                            job metadata (SQLite)
├── users/<user>/inputs/<id>.zip       uploaded archives
├── users/<user>/outputs/<id>.pdf      compiled documents
├── users/<user>/outputs/<id>.log      compilation logs
├── logs/server.log                    the server's log (older parts in server.log.1 to .4)
└── tmp/                               work space, emptied at startup
```

## Reverse proxies

TeXdrop works behind a reverse proxy (nginx, Caddy, Traefik, Nginx Proxy Manager, ...) as it
is: nothing has to be set. The proxy has to send the usual headers, which Caddy, Traefik and
Nginx Proxy Manager do by themselves and plain nginx does with the `proxy_set_header` lines of
the example below. Four things matter:

- **The address.** Set `PUBLIC_BASE_URL` to the address users reach, such as
  `https://texdrop.example.com`. It is not required, but recommended: the MCP server's
  transfer links then point there, whatever the proxy sends, the web page's cookie is marked
  `Secure`, and browsers may send changes (a login, an upload) only from pages at exactly that
  address, so that no other site can forge them. Without it, the links follow each request's
  `Host` and scheme. TeXdrop must be served at the root of its address (a domain or subdomain
  of its own): the web page uses paths such as `/api/jobs`, so it cannot live under a path
  like `https://example.com/texdrop/`, and a `PUBLIC_BASE_URL` with a path stops the server
  at startup.
- **The client's address.** It is logged, and binds the MCP server's transfer links to the
  agent's computer. By default (`FORWARDED_ALLOW_IPS=*`) any peer may be
  a proxy: the client's address is the first address of the proxy's `X-Forwarded-For`
  header, else Cloudflare's `CF-Connecting-IP`, else the connection's. Have the proxy set
  `X-Forwarded-For` to the client's address rather than add to one the client sent (nginx:
  `$remote_addr`, not `$proxy_add_x_forwarded_for`), since the first address is the one used.

  The catch: anyone who can reach TeXdrop's port without the proxy can claim any address
  this way. Publish the port only to the proxy (`-p 127.0.0.1:8000:8000`, or no `-p` with the
  proxy on the same Docker network), or name the proxy in `FORWARDED_ALLOW_IPS`, as the
  container sees it:
  - for a proxy on the same machine that forwards to the published port, usually
    `172.17.0.1`, the gateway of Docker's default network;
  - for a proxy in a container on the same Docker network, that network's subnet, such as
    `172.18.0.0/16` (see `docker network inspect <network>`), since the proxy container's
    own address can change.

  uvicorn then takes the last address of `X-Forwarded-For` that is not one of the proxies,
  and ignores the headers of every other peer, `CF-Connecting-IP` included, so that no one
  can claim another address. Separate several proxies or networks with commas; an entry that
  is not an IP address or network stops the server at startup instead of being ignored, and
  the startup log says whose `X-Forwarded-For` is used. A wrong address makes every request
  seem to come from the proxy: the log then shows the proxy's address on every line.
- **The scheme.** The proxies that `FORWARDED_ALLOW_IPS` trusts (by default any peer) are also
  trusted for `X-Forwarded-Proto`: the server then knows that the client used HTTPS, marks the
  web page's cookie `Secure`, and, without `PUBLIC_BASE_URL`, hands out `https://` transfer
  links.
- **Timeouts and sizes.** A request waiting for a job (`?wait=`) stays open until the job
  finishes; keep `wait` below the proxy's timeout (nginx: 60 seconds by default), as the MCP
  server does (50 seconds). Uploads are as large as the archives: raise the proxy's limit on
  request bodies (nginx: `client_max_body_size`, 1 MB by default) to `MAX_UPLOAD_MB` + 1 MB.
  TeXdrop refuses request bodies without a `Content-Length` (`411`): the proxy must pass
  bodies on with their `Content-Length`, as nginx does.

For example, with nginx on the same machine:

```nginx
server {
    listen 443 ssl;
    server_name texdrop.example.com;
    # ssl_certificate and ssl_certificate_key ...

    client_max_body_size 101m;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_read_timeout 120s;
    }
}
```

```bash
docker run -d --name texdrop -p 127.0.0.1:8000:8000 -v "$PWD/data:/data" \
  -e TZ=Europe/Athens -e PUBLIC_BASE_URL=https://texdrop.example.com \
  -e FORWARDED_ALLOW_IPS=172.17.0.1 \
  --cap-drop ALL --security-opt no-new-privileges --restart unless-stopped \
  texdrop
```

Publishing the port on `127.0.0.1` only also keeps anyone from going around the proxy.

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
  the address comes from the proxy's `X-Forwarded-For` (see
  [Reverse proxies](#reverse-proxies)).
- One line per finished job, with the first line of the error when it failed (the job's log
  has the rest).
- Unexpected errors, at level `ERROR` and with their traceback.

Tokens never appear in the log, nor do the secrets of the MCP server's transfer links
(logged as `/api/links/***`, with the reason when a link is refused).
