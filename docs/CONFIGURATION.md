# Configuration

The users and their tokens are in a file in the data directory; everything else is an
environment variable. How to run the service is in [DEPLOYMENT.md](DEPLOYMENT.md).

## Users

Users are defined in `data/config.json` (see [`config.example.json`](../config.example.json)):

```json
{"users": {"alice": "<token for alice>", "bob": "<token for bob>"}}
```

A user name is 1-32 of `a-z 0-9 _ -` and is also the name of the user's directory. A token
is 16-256 letters, digits or `._~+/=-`; make one with `openssl rand -hex 32`. The file is
re-read when it changes, so users can be added or removed without a restart. An invalid
file stops the server at startup; an invalid edit while it runs is ignored (with an error
in the log).

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
| `TZ` | `UTC` | Time zone of the dates the API and the web page show, and of the log (an IANA name such as `Europe/Athens`). Everything is stored in UTC. |
| `AUTO_DELETE_DAYS` | `0` | Every day at 02:00 UTC, delete jobs (archive, PDF, log) that finished more than this many days ago. `0` never deletes. |
| `COMPILE_TIMEOUT_MINUTES` | `15` | A compilation running longer is stopped and fails; the next job starts. |
| `MAX_UPLOAD_MB` | `100` | Largest accepted upload. An archive may unpack to at most 10 times this size. |
| `COMPILE_MEMORY_MB` | `0` (no limit) | Memory that each process of a compilation may reserve (its address space). A document that needs more fails with an out-of-memory error. pdfLaTeX, LuaLaTeX, Biber and Ghostscript run within 256 MB, XeLaTeX within 512 MB. |
| `COMPILE_DISK_MB` | `0` (no limit) | Disk space of a compilation: the unpacked project, everything the compilation writes, and about 2 MB of caches. Measured every 2 seconds; a compilation that goes over is stopped and fails. |
| `MAX_USER_STORAGE_MB` | `0` (no limit) | Storage of each user's jobs (archives, PDFs and logs). An upload that does not fit is refused until the user deletes old jobs. The PDF and log of a job come on top, so a user can end up a little above it. |
| `UNPACK_TIMEOUT_MINUTES` | `5` | Time to inspect or unpack one archive (real projects need seconds). |
| `UNPACK_MEMORY_MB` | `4096` | Memory the process that inspects or unpacks an archive may reserve; `0` for no limit. Real projects need under 64 MB. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | IPs of reverse proxies whose `X-Forwarded-*` headers are trusted (needed for secure cookies behind an HTTPS proxy). With Docker, the container sees a proxy on the same machine coming from the Docker network's gateway (often `172.17.0.1`), not from `127.0.0.1`: name that address, or use `*` when the port is published only on `127.0.0.1` (`-p 127.0.0.1:8000:8000`). |
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
