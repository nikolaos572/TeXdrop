# Configuration

The users and their tokens are in a file in the data directory, `users.json`, managed with
commands; everything else is an environment variable. How to run the service is in
[DEPLOYMENT.md](DEPLOYMENT.md).

## Users

Two commands manage the users. Run them on the running container, at any time, without a
restart:

```bash
docker exec texdrop python -m texdrop adduser alice       # adds the user and prints their token
docker exec texdrop python -m texdrop reset-token alice   # gives the user a new token and prints it
```

With Docker Compose, they start with `docker compose exec texdrop` instead of
`docker exec texdrop`. Without Docker, set the same environment as the server's:
`DATA_DIR=~/latex-data .venv/bin/python -m texdrop adduser alice`.

A user name is 1-32 of `a-z 0-9 _ -`, starting with a letter or a digit, and is also the
name of the user's directory. All users are equal: each one sees only their own jobs, and
there is no administrator account.

A token is what its user logs in with on the web page, and what their scripts and agents
send. `reset-token` is for a token that is lost or leaked: the old token stops working at
once, and every browser logged in with it is logged out, so the user has to log in again and
to give the new one to their scripts and agents. Both commands print the token alone on
standard output and their message on standard error, so a script can write `token=$(docker exec texdrop python -m texdrop adduser alice)`. They
exit with status 1, and change nothing, if the user already exists (`adduser`) or does not
(`reset-token`), if the name is invalid, or if the file is invalid or cannot be written.

To remove a user, delete their line from the file (see below). Their jobs stay, in
`users/<user>` and in the database, until `AUTO_DELETE_DAYS` removes them, and a new user
added later with the same name would see them: to remove them at once, have the user press
**Delete all** on the web page first.

### The users file

The commands keep the users in `data/users.json` (the `USERS_FILE` setting), which you can
also edit by hand (see [`users.example.json`](../users.example.json)):

```json
{"users": {"alice": "<token for alice>", "bob": "<token for bob>"}}
```

A token is 16-256 letters, digits or `._~+/=-`; make one with `openssl rand -hex 32`, as the
commands do. Two users cannot share a token.

The file is re-read when it changes, so users can be added or removed without a restart; a
removed user's token stops working at once. An invalid file stops the server at startup;
an invalid edit while it runs is ignored (with an error in the log), and the previous
content stays in use. Without the file the server starts, logs a warning, and refuses every
request.

The file `adduser` creates can be read only by its owner, as it holds secrets: the
container's user, uid 61001, so use `sudo` to edit it on the host. A file that already exists
keeps its mode. It also keeps its owner if that is the user running the command, or if the
command runs as root; otherwise the user running the command becomes its owner. Compilations
can never read it: the server refuses to start if `USERS_FILE` lies in a directory that they
can read, as it does for `DATA_DIR`.

Older versions of TeXdrop kept the users in `data/config.json`. At its first start, the
server renames that file to `users.json` (and says so in the log), unless `users.json`
exists already or `USERS_FILE` names another file.

The format is that of [PixDrop](https://github.com/nikolaos572/PixDrop)'s `users.json`, so
one file can serve both services: point `USERS_FILE` at it, or mount it as
`/data/users.json`:

```bash
docker run ... -v /srv/pixdrop/data/users.json:/data/users.json:ro ... texdrop
```

(Docker mounts the file itself: an editor that saves by writing a new file and renaming it
leaves the container with the old one until it restarts.) The commands keep whatever else
the file holds. They cannot change a file mounted with `:ro`, and say so: add the user where
the file comes from, or mount it without `:ro`, for uid 61001 to write it.

## Environment variables

Set them with `-e NAME=value` on the `docker run` command line, under `environment:` in
`compose.yaml`, or in the environment of `python -m texdrop`:

```yaml
    environment:
      COMPILE_TIMEOUT_MINUTES: "30"
      COMPILE_DISK_MB: "4096"
```

Sizes are in MB (1 MB = 1024 × 1024 bytes). The
compilation and storage limits are off by default: a compilation may use whatever the
container may use, and the [container's limits](DEPLOYMENT.md#container-limits) are the only
bound. Server Info and `GET /api/info` show the values in effect. An invalid value stops the
server at startup, with a message that names it.

| Variable | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `/data` | Where the service keeps everything: the users file, the jobs and the log. The Docker image uses the volume `/data`; without Docker, set it to a directory of your choice. |
| `USERS_FILE` | `$DATA_DIR/users.json` | The [users](#users) file. |
| `TZ` | `UTC` | Time zone of the dates the API and the web page show, and of the log (an IANA name such as `Europe/Athens`). Everything is stored in UTC. |
| `AUTO_DELETE_DAYS` | `0` | Every day at 02:00 UTC, delete jobs (archive, PDF, log) that finished more than this many days ago. `0` never deletes. |
| `COMPILE_TIMEOUT_MINUTES` | `15` | A compilation running longer is stopped and fails; the next job starts. |
| `MAX_UPLOAD_MB` | `100` | Largest accepted upload. An archive may unpack to at most 10 times this size. |
| `COMPILE_MEMORY_MB` | `0` (no limit) | Memory that each process of a compilation may reserve (its address space). A document that needs more fails with an out-of-memory error. pdfLaTeX, LuaLaTeX, Biber and Ghostscript run within 256 MB, XeLaTeX within 512 MB. |
| `COMPILE_DISK_MB` | `0` (no limit) | Disk space of a compilation: the unpacked project, everything the compilation writes, and about 2 MB of caches. Measured every 2 seconds; a compilation that goes over is stopped and fails. |
| `MAX_USER_STORAGE_MB` | `0` (no limit) | Storage of each user's jobs (archives, PDFs and logs). An upload that does not fit is refused until the user deletes old jobs. The PDF and log of a job come on top, so a user can end up a little above it. |
| `UNPACK_TIMEOUT_MINUTES` | `5` | Time to inspect or unpack one archive (real projects need seconds). |
| `UNPACK_MEMORY_MB` | `4096` | Memory the process that inspects or unpacks an archive may reserve; `0` for no limit. Real projects need under 64 MB. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Your reverse proxy's IP address or network, as the server sees it, such as `172.17.0.1` for a proxy on the Docker host or `172.18.0.0/16` for one on the same Docker network; several separated by commas. Their `X-Forwarded-For` and `X-Forwarded-Proto` headers are trusted: for the client's address (in the log, and for the MCP server's transfer links) and for the scheme (`https` URLs and the cookie's `Secure` flag). The headers of other peers are ignored, so that no one can claim another address; until you name the proxy, every request seems to come from it. `*` trusts any peer, and also Cloudflare's `CF-Connecting-IP`: then the port must be reachable only through the proxy. See [Reverse proxies](DEPLOYMENT.md#reverse-proxies). |
| `ENABLE_MCP` | `0` | Set to `1` to serve the [MCP server](MCP.md) at `/mcp`, through which AI agents (Claude Code, Antigravity, ...) use the service with a user's token, and its transfer links under `/api/links/`. |
| `LINK_EXPIRY_MINUTES` | `60` | A transfer link of the MCP server stops working this long after it was made (it also stops once used). |
| `ALLOW_UNSANDBOXED` | `0` | Set to `1` to start even when the sandbox lacks protections: on kernels before 6.12 or without Landlock, without the system-call filter or `/proc`, or as root (see [Older kernels](SECURITY.md#older-kernels)). Without Landlock, documents could read every file the server can read, including all tokens. |
| `HOST`, `PORT` | `0.0.0.0`, `8000` | Where the server listens. With Docker, publish the port with `-p` instead; the image's health check follows `PORT` should you change it (with `--network host`, say). |

To bound memory, prefer the container's memory limit: it counts the memory actually used,
while `COMPILE_MEMORY_MB` counts what a program reserves, which is more.

## Fixed limits

These are not configurable.

| | |
|---|---|
| Unpacked size of an archive | 10 × `MAX_UPLOAD_MB` |
| Entries in an archive | 20,000 |
| A single file written by a compilation | 2 GB |
| Waiting for a job with `GET /api/jobs/{id}?wait=` | 600 seconds |
| The log file | 10 MB, plus 4 older files |
