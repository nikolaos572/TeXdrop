# API documentation

Everything the service does is available through a JSON API over HTTP: upload a LaTeX
project, follow its compilation, download the PDF and the log, and manage your jobs. The
web page uses the same API, so anything it can do, a program can do too.

This document describes the whole API: authentication, every endpoint with its
parameters, responses and errors, how jobs behave, and complete examples. The server also
serves its machine-readable description (see [OpenAPI](#openapi)).

**Contents**

1. [Quick start](#quick-start)
2. [Good to know](#good-to-know)
3. [Concepts](#concepts)
4. [Authentication](#authentication)
5. [Requests and responses](#requests-and-responses)
6. [Archives and the main file](#archives-and-the-main-file)
7. [Jobs](#jobs)
8. [Endpoints](#endpoints)
9. [Examples](#examples)
10. [Writing a client](#writing-a-client)
11. [OpenAPI](#openapi)

## Quick start

```bash
URL=http://localhost:8000; AUTH="Authorization: Bearer $TOKEN"

# Queue a project (the main file and the engine are optional)
curl -H "$AUTH" -F file=@paper.zip -F engine=pdflatex $URL/api/jobs
# {"id":42,"filename":"paper.zip","main":"main.tex","engine":"pdflatex","status":"queued","queue_position":1,...}

# Wait up to 50 seconds for it to finish (repeat while the status is queued or processing)
curl -H "$AUTH" "$URL/api/jobs/42?wait=50"
# {"id":42,...,"status":"done","error":null,"has_pdf":true,"has_log":true}

# Download the PDF and the log
curl -H "$AUTH" -o paper.pdf $URL/api/jobs/42/pdf
curl -H "$AUTH" -o paper.log $URL/api/jobs/42/log
```

## Good to know

A few things that are easy to miss when building a client; the sections below have the
details.

- **No calls from other web sites.** The API sends no CORS headers, so a web page on
  another domain cannot call it from the browser. Call it from your own server, or put your
  page and the API behind the same reverse proxy ([more](#requests-and-responses)).
- **Keep `wait` below your proxy's timeout.** `GET /api/jobs/{id}?wait=N` keeps the request
  open for up to `N` seconds. nginx, for example, ends requests after 60 seconds by
  default, so use `wait=50` and ask again until the job is finished
  ([more](#waiting-for-a-job)).
- **Uploads need a `Content-Length` header.** Streamed (chunked) uploads are refused with
  `411`; send the archive as a file, as `curl -F` and most HTTP libraries do
  ([more](#requests-and-responses)).
- **`detail` is a list for `422`.** Other errors have a message in `detail`, but invalid
  parameters give a list of problems ([more](#errors)).
- **No `HEAD` requests.** They get `405 Method Not Allowed`; use `GET`.
- **Downloads can be resumed.** The archive, PDF and log downloads honour a `Range` header
  and answer `206 Partial Content` ([more](#get-apijobsidlog)).

## Concepts

- **User and token.** The administrator gives each user a name and a secret token (see
  [CONFIGURATION.md](CONFIGURATION.md#users)). Every request is made as one user, and a user
  sees only their own jobs.
- **Project archive.** A LaTeX project is uploaded as one archive: `.zip`, `.7z`, `.tar`,
  `.tar.gz` (`.tgz`), `.tar.bz2` or `.tar.xz`, with its figures, bibliography files, classes
  and so on.
- **Main file.** The `.tex` file to compile. The server detects it when it can; otherwise you
  name it.
- **Engine.** `pdflatex` (the default), `xelatex` or `lualatex`. The server compiles with
  `latexmk`, which runs the engine as often as needed and runs BibTeX or Biber when the
  document has a bibliography.
- **Job.** One submitted archive, with its compilation and results. Its id is a positive
  integer, unique on the server.
- **Queue.** Jobs of all users are compiled one at a time, strictly in the order they were
  submitted. A job waits in the queue while others compile.

## Authentication

Send the token in the `Authorization` header of every request (except
[`GET /api/health`](#get-apihealth)):

```
Authorization: Bearer 4c1f0a6e9b...
```

- The word `Bearer` is not case-sensitive. A token has 16 to 256 characters, letters,
  digits or `._~+/=-`.
- A missing, malformed or unknown token gets `401 Unauthorized` with the header
  `WWW-Authenticate: Bearer` and `{"detail": "missing or invalid token"}`. The same happens
  when the administrator removes the token while you use it: the users file is re-read
  when it changes.
- Tokens are never accepted in the URL (no `?token=`), and never appear in the server's log.
- To check a token, call [`GET /api/session`](#get-apisession), which returns the user name.

**Browsers.** The web page logs in with [`POST /api/session`](#post-apisession), which keeps
the token in an HTTP-only cookie named `latex_token`. Requests authenticated by that cookie
(and not by the header) must carry the header `X-Requested-With` (any value) when they
change something (`POST`, `PUT`, `PATCH`, `DELETE`); otherwise they get `403 Forbidden`
with `{"detail": "cookie-authenticated requests need a X-Requested-With header"}`. This
stops other web sites from acting with a visitor's cookie. When a request has both, the
`Authorization` header is used and the cookie is ignored. Programs should use the header.

## Requests and responses

- **Base URL.** All endpoints are under `/api` on the server, for example
  `http://localhost:8000/api/jobs`. Behind a reverse proxy the base URL is the proxy's.
- **Request bodies.** Uploads are `multipart/form-data` (as `curl -F` sends them). The only
  JSON body is the one of [`POST /api/session`](#post-apisession). Every `POST`, `PUT` and
  `PATCH` request must have a `Content-Length` header; without it the server answers
  `411 Length Required` (`{"detail": "Content-Length required"}`). Ordinary HTTP clients send
  it; do not use chunked uploads.
- **Responses** are JSON (`application/json`), except the file downloads and `204 No
  Content`, which has no body. Fields are always present; those that may be empty are
  `null`. New fields may be added in later versions, so ignore fields you do not know.
- **Dates** are ISO 8601 strings with the offset of the server's time zone (the `TZ`
  setting, shown by [`GET /api/info`](#get-apiinfo)), to the second, for example
  `"2026-10-22T17:17:09+03:00"`.
- **Sizes** in the limits are in MB, where 1 MB = 1024 × 1024 bytes. Storage use is in bytes.
- **Methods.** Only the methods listed here are accepted; others get `405 Method Not
  Allowed`. `HEAD` is not supported: use `GET`.
- **Cross-origin requests (CORS)** are not supported: a web page served from another
  origin cannot call the API from the browser. Call it from your server, or serve your page
  from the same origin through a reverse proxy.
- **Rate limits.** There are none, but compilations are queued: submitting many jobs at
  once makes everyone wait.
- Responses carry `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` and
  `Referrer-Policy: no-referrer`.

### Errors

Every error except `500` has a JSON body with a `detail` field. For most errors `detail` is
a message meant for people, which a client can show as it is:

```json
{"detail": "several .tex files found, choose the main file: a.tex, b.tex"}
```

For `422` (a parameter is missing or has an invalid value) `detail` is a list with one
entry per problem. `loc` says where the value is (`path`, `query` or `body`, then its name),
`msg` what is wrong, and `input` what was received:

```json
{"detail": [{"type": "enum", "loc": ["body", "engine"], "msg": "Input should be 'pdflatex', 'xelatex' or 'lualatex'", "input": "pdftex", "ctx": {"expected": "'pdflatex', 'xelatex' or 'lualatex'"}}]}
```

The messages may be reworded in later versions: base decisions on the status code, not
on the text.

| Status | Meaning |
|---|---|
| `200 OK`, `201 Created`, `204 No Content` | Success. |
| `206 Partial Content` | Part of a file, as asked with a `Range` header. |
| `400 Bad Request` | The archive or the main file cannot be used, or the user's storage is full. `detail` says why. |
| `401 Unauthorized` | No token, or a token the server does not know. |
| `403 Forbidden` | A cookie-authenticated change without `X-Requested-With`. |
| `404 Not Found` | No such job (or a job of another user), or the file does not exist (yet). |
| `405 Method Not Allowed` | The endpoint does not accept this method. |
| `411 Length Required` | A `POST` without `Content-Length`. |
| `413 Content Too Large` | The upload is larger than `max_upload_mb`. |
| `422 Unprocessable Content` | A missing or invalid parameter; `detail` is a list. |
| `500 Internal Server Error` | A fault in the server (body: the text `Internal Server Error`). It is logged; retrying may help. |

When the server is restarted (for an upgrade, say), connections are refused for a few
seconds, and requests still open after 5 seconds of the shutdown, such as a long
[wait](#get-apijobsid), are cut off without a response. Retry them; queued jobs are kept,
and a job that was compiling is compiled again from the start.

## Archives and the main file

### What an archive may contain

The server checks every archive the same way, when it is inspected, when it is submitted
and when it is compiled:

- The format is recognised from the file's content, not from its name. The supported
  formats, as the API names them, are `zip`, `7z`, `tar`, `tar.gz`, `tar.bz2` and `tar.xz`.
- Only regular files and folders. Symbolic links, hard links and special files are
  refused.
- Paths must be relative and stay inside the project: no absolute paths, no `..`, no
  control characters.
- At most 20,000 entries, and at most `max_extracted_mb` (10 times the upload limit) in all
  once unpacked.
- The archive must contain at least one file. Empty folders are ignored, and so are macOS
  metadata (`__MACOSX/`, `.DS_Store`, `._*` files).

If all files are in one folder (a zipped folder, such as `paper/main.tex` and
`paper/refs.bib`), that folder is the project's root, and paths are given relative to it
(`main.tex`, not `paper/main.tex`).

### Candidate main files

The `tex_files` the API lists are the archive's files that can be compiled as the main
file: files ending in `.tex` (in any case) whose path, relative to the project's root, is
made of names that begin with a letter, digit or `_` and contain only letters, digits and
`_ .,+=@()-` (names are passed to `latexmk`, which runs commands through a shell). Other
files, such as `my%file.tex`, are still unpacked and can be included by the main file, but
cannot be the main file. The list is sorted.

### Main file detection

When no main file is given, the server picks:

1. the only `.tex` file at the top of the project, if there is exactly one; otherwise
2. the only `.tex` file at the top of the project with a `\documentclass` line (one that is
   not commented out with `%`, within the file's first 64 KB).

If neither gives exactly one file, there is no detected main file: name it with `main`.
Files in subfolders are never detected, but can be named.

### Naming the main file

`main` is a path relative to the project's root, such as `main.tex` or
`chapters/thesis.tex`, with `/` between folders. Surrounding spaces are ignored, and so is
the root folder in front (`paper/main.tex` is accepted for `main.tex` in a zipped folder
`paper`). It must be one of the archive's `tex_files`; an empty `main` means "detect it".

The compilation runs in the main file's folder, so relative `\input`, `\includegraphics`
and `\bibliography` paths work as they do on your computer.

## Jobs

### The job object

Endpoints that return jobs return this object:

```json
{
  "id": 42,
  "filename": "paper.zip",
  "main": "main.tex",
  "engine": "pdflatex",
  "status": "failed",
  "queue_position": null,
  "submitted_at": "2026-10-22T17:17:10+03:00",
  "started_at": "2026-10-22T17:17:10+03:00",
  "finished_at": "2026-10-22T17:17:12+03:00",
  "error": "./main.tex:3: Undefined control sequence.",
  "has_pdf": false,
  "has_log": true
}
```

| Field | Type | Meaning |
|---|---|---|
| `id` | integer | The job's id, from 1 up. Ids increase with every submission on the server (of any user) and are never reused. |
| `filename` | string | The uploaded file's name, as the client sent it, reduced to its last component, with characters other than letters, digits and `_ .,+=@()[]-` replaced by `_`, without spaces or dots at either end, and at most 200 characters. `upload.zip` (or the format's extension) if nothing is left. |
| `main` | string | The main file, relative to the project's root. |
| `engine` | string | `pdflatex`, `xelatex` or `lualatex`. |
| `status` | string | `queued`, `processing`, `done` or `failed` (see below). |
| `queue_position` | integer or null | For a queued job, its place in the queue of all users: `1` runs next (after the job compiling now). `null` for other statuses. |
| `submitted_at` | string | When the job was submitted. |
| `started_at` | string or null | When its compilation started; `null` while it is queued. |
| `finished_at` | string or null | When it became `done` or `failed`; `null` before. |
| `error` | string or null | For a failed job, why (see [Errors of a job](#errors-of-a-job)); otherwise `null`. |
| `has_pdf` | boolean | Whether a PDF can be downloaded. Also `true` for a failed job when LaTeX produced a PDF despite the errors. |
| `has_log` | boolean | Whether the log can be downloaded: `true` once the job is `done` or `failed`. |

### Statuses

```
queued ──> processing ──> done
                     └──> failed
```

- `queued`: waiting for its turn. `queue_position` says how many jobs are ahead.
- `processing`: being unpacked and compiled. One job at a time on the whole server.
- `done`: `latexmk` succeeded and produced the PDF. Warnings (undefined references, overfull
  boxes) do not make a job fail; the log has them.
- `failed`: unpacking or compiling went wrong. `error` says why; `has_pdf` says whether a
  PDF was produced anyway (as on Overleaf, LaTeX goes on after most errors).

`done` and `failed` are final: a job never changes after that, until it is deleted. If the
server is restarted while a job is `processing`, the job becomes `queued` again (with
`started_at` back to `null`) and is compiled again from the start.

### Errors of a job

`error` has one to five lines, separated by `\n`:

- `lualatex was killed, most likely because the memory ran out.`, for each program that
  was stopped for lack of memory.
- The first distinct error lines of the log: TeX errors, with the file and line where TeX
  found them (`./main.tex:3: Undefined control sequence.`) or starting with `!`, and Biber
  errors (`ERROR - ...`). A missing package, for example, gives:
  ```
  ! LaTeX Error: File `nosuchpackage.sty' not found.
  ./main.tex:3: Emergency stop.
  ./main.tex:3:  ==> Fatal error occurred, no output PDF file produced!
  ```
- `latexmk failed with exit code 12`, when no error line could be found.
- `Compilation timed out after 15 minutes.`, after `compile_timeout_minutes`.
- `Compilation stopped: its files took more than 1024 MB of disk.`, beyond `compile_disk_mb`.
- An archive message, if the archive could not be unpacked for the compilation (normally
  caught at submission).
- `Internal error while compiling; see the server log.`, for a fault in the server.

### Results

- **The PDF** is named after the main file (`main.pdf` for `main.tex`).
- **The log** is the transcript of `latexmk`: everything the engine, BibTeX or Biber, and
  `latexmk` itself printed, run after run (not the `.log` file TeX writes). TeX's lines are
  not wrapped at 79 characters. It is mostly UTF-8, but TeX may print bytes that are not. For a job that failed
  before compiling, the log is the error message.
- Other files the compilation produces (`.aux`, `.bbl`, `.synctex.gz`, ...) are not kept.

### Lifetime and storage

- A job stays until you delete it, or until the server deletes it automatically:
  `auto_delete_days` after it finished, if that setting is not `0` (the deletion runs
  daily at 02:00 UTC). Download what you need.
- Deleting a job deletes its archive, PDF and log. Deleting a `processing` job stops its
  compilation.
- If `max_user_storage_mb` is not `0`, the archives, PDFs and logs of a user's jobs may
  take at most that much. A submission that does not fit is refused with `400` until older
  jobs are deleted. The PDF and log of a job come on top, so a user can end up a little
  above the limit.

## Endpoints

| Endpoint | |
|---|---|
| [`GET /api/health`](#get-apihealth) | Whether the server is up (no token). |
| [`GET /api/info`](#get-apiinfo) | The server's settings and limits, the queue, your account. |
| [`POST /api/inspect`](#post-apiinspect) | Check an archive and list its candidate main files. |
| [`POST /api/jobs`](#post-apijobs) | Submit an archive for compilation. |
| [`GET /api/jobs`](#get-apijobs) | Your jobs, newest first, a page at a time. |
| [`GET /api/jobs/{id}`](#get-apijobsid) | One job, optionally waiting until it finishes. |
| [`DELETE /api/jobs/{id}`](#delete-apijobsid) | Delete a job. |
| [`DELETE /api/jobs`](#delete-apijobs) | Delete all your jobs. |
| [`GET /api/jobs/{id}/input`](#get-apijobsidinput) | Download the uploaded archive. |
| [`GET /api/jobs/{id}/pdf`](#get-apijobsidpdf) | Download the PDF. |
| [`GET /api/jobs/{id}/log`](#get-apijobsidlog) | Download the log. |
| [`GET /api/session`](#get-apisession) | Who you are. |
| [`POST /api/session`](#post-apisession), [`DELETE /api/session`](#delete-apisession) | Log a browser in and out (the web page). |

In paths, `{id}` is a job id: an integer from 1 to 9223372036854775807. Anything else gets
`422`.

### `GET /api/health`

Whether the server is running. Needs no token; meant for monitoring and health checks.

**Response** `200`:

```json
{"status": "ok"}
```

### `GET /api/info`

The server's settings and limits, the state of the shared queue, and your account. These
are the values the server applies, so a client can check an upload against them before
sending it.

**Response** `200`:

```json
{
  "server": {
    "version": "1.0.0",
    "tex_live": "TeX Live 2026",
    "engines": ["pdflatex", "xelatex", "lualatex"],
    "default_engine": "pdflatex",
    "timezone": "Europe/Athens",
    "auto_delete_days": 0,
    "compile_timeout_minutes": 15.0,
    "max_upload_mb": 100,
    "max_extracted_mb": 1000,
    "compile_memory_mb": 0,
    "compile_disk_mb": 0,
    "max_user_storage_mb": 0,
    "unpack_timeout_minutes": 5.0,
    "unpack_memory_mb": 4096
  },
  "queue": {"queued": 3, "processing": 1},
  "account": {"user": "alice", "jobs": 12, "storage_bytes": 18350080}
}
```

| Field | Type | Meaning |
|---|---|---|
| `server.version` | string | The service's version. |
| `server.tex_live` | string or null | The TeX Live release, such as `"TeX Live 2026"`; `null` if unknown. |
| `server.engines` | list of strings | The accepted values of `engine`. |
| `server.default_engine` | string | The engine used when none is given. |
| `server.timezone` | string | The IANA time zone of the dates in responses. |
| `server.auto_delete_days` | integer | Finished jobs are deleted this many days after they finished; `0`: never. |
| `server.compile_timeout_minutes` | number | A compilation running longer fails. |
| `server.max_upload_mb` | integer | The largest archive accepted. |
| `server.max_extracted_mb` | integer | The largest total size of an archive's unpacked files. |
| `server.compile_memory_mb` | integer | Memory each program of a compilation may reserve; `0`: no limit. |
| `server.compile_disk_mb` | integer | Disk a compilation may use; `0`: no limit. |
| `server.max_user_storage_mb` | integer | Storage of each user's jobs; `0`: no limit. |
| `server.unpack_timeout_minutes` | number | Time allowed to inspect or unpack an archive. |
| `server.unpack_memory_mb` | integer | Memory allowed to inspect or unpack an archive; `0`: no limit. |
| `queue.queued` | integer | Jobs of all users waiting. |
| `queue.processing` | integer | Jobs of all users compiling now (`0` or `1`). |
| `account.user` | string | Your user name. |
| `account.jobs` | integer | Your jobs, of any status. |
| `account.storage_bytes` | integer | Bytes your archives, PDFs and logs take. |

### `POST /api/inspect`

Checks an archive as a submission would, and lists its candidate main files, without
creating a job or storing anything. Use it to let a person choose the main file before
submitting (the web page does).

**Request**: `multipart/form-data` with

| Field | Required | |
|---|---|---|
| `file` | yes | The archive. |

```bash
curl -H "$AUTH" -F file=@paper.zip $URL/api/inspect
```

**Response** `200`:

```json
{"format": "zip", "tex_files": ["main.tex", "sections/intro.tex"], "main": "main.tex"}
```

| Field | Type | Meaning |
|---|---|---|
| `format` | string | `zip`, `7z`, `tar`, `tar.gz`, `tar.bz2` or `tar.xz`, recognised from the content. |
| `tex_files` | list of strings | The [candidate main files](#candidate-main-files), relative to the project's root, sorted. May be empty. |
| `main` | string or null | The [detected main file](#main-file-detection), or `null` when there is none. |

An archive without any `.tex` file is not an error here (`tex_files` is empty), but it
cannot be submitted.

**Errors**

- `400`, with one of these `detail`s:
  - `unsupported file type: upload a .zip, .7z, .tar, .tar.gz (.tgz), .tar.bz2 or .tar.xz archive`
  - `the archive contains no files`
  - `the archive has more than 20000 entries`
  - `the archive unpacks to more than 1000 MB`
  - `links are not allowed in the archive: 'figures/plot.pdf'`
  - `special files are not allowed in the archive: 'dev/null'`
  - `unsafe path in the archive: '../outside.tex'`
  - `cannot read the zip archive: ...` (a damaged archive; libarchive's reason follows)
  - `unpacking the archive took longer than 5 minutes`
  - `unpacking the archive ran out of memory` (it needs more than `unpack_memory_mb`)
  - `the archive could not be unpacked` (the unpacking process failed, for example for lack of memory)
- `413`: `the upload exceeds the 100 MB limit` (judged from `Content-Length`, before the
  upload is read) or `the file exceeds the 100 MB limit` (found while reading it).
- `422`: no `file` field.

### `POST /api/jobs`

Submits an archive for compilation. The archive is checked (as by
[`POST /api/inspect`](#post-apiinspect)) and the main file resolved before the job is
created, so a job is only created for a usable archive with a valid main file.

**Request**: `multipart/form-data` with

| Field | Required | |
|---|---|---|
| `file` | yes | The archive. Its file name becomes the job's `filename`. |
| `main` | no | The main file, relative to the project's root (see [Naming the main file](#naming-the-main-file)), at most 1024 characters. Omitted or empty: detected. |
| `engine` | no | `pdflatex` (default), `xelatex` or `lualatex`. |

```bash
curl -H "$AUTH" -F file=@thesis.tar.gz -F main=thesis.tex -F engine=lualatex $URL/api/jobs
```

**Response** `201`: the new [job](#the-job-object), with status `queued` and its
`queue_position`:

```json
{"id": 43, "filename": "thesis.tar.gz", "main": "thesis.tex", "engine": "lualatex", "status": "queued", "queue_position": 2, "submitted_at": "2026-10-22T17:20:31+03:00", "started_at": null, "finished_at": null, "error": null, "has_pdf": false, "has_log": false}
```

**Errors**

- `400`: any of the archive errors of [`POST /api/inspect`](#post-apiinspect), or:
  - `the archive contains no .tex file`
  - `several .tex files found, choose the main file: a.tex, b.tex` (no main file could be
    detected; up to 20 names are listed)
  - `main file 'c.tex' is not a .tex file in the archive; choose one of: a.tex, b.tex`
  - `main file 'notes.md' is not a .tex file in the archive; the archive has no usable .tex file`
  - `not enough storage: your jobs take 49.5 MB of your 50.0 MB and this archive needs 1.2 MB; delete old jobs first`
- `413`: as for [`POST /api/inspect`](#post-apiinspect).
- `422`: no `file`, an `engine` that is not one of the three, or a `main` longer than 1024
  characters.

A `POST` is not repeated safely: if the connection breaks before the response arrives, the
job may or may not have been created. List your newest jobs
([`GET /api/jobs`](#get-apijobs)) before submitting again.

### `GET /api/jobs`

Your jobs, newest first (highest id first), a page at a time.

**Query parameters**

| Parameter | Default | |
|---|---|---|
| `page` | `1` | The page, from 1 to 1,000,000. Page 1 has the newest jobs. |
| `per_page` | `5` | Jobs per page, from 1 to 100. |

```bash
curl -H "$AUTH" "$URL/api/jobs?page=1&per_page=20"
```

**Response** `200`:

```json
{
  "jobs": [{"id": 43, "status": "queued", ...}, {"id": 42, "status": "done", ...}],
  "total": 57,
  "page": 1,
  "per_page": 20
}
```

| Field | Type | Meaning |
|---|---|---|
| `jobs` | list of [jobs](#the-job-object) | This page's jobs. A page after the last one is empty (not an error). |
| `total` | integer | How many jobs you have in all, on every page. |
| `page`, `per_page` | integer | As requested (or their defaults). |

The number of pages is `ceil(total / per_page)`. Jobs submitted or deleted between two
requests shift the pages; to go through all jobs, see [Listing all jobs](#listing-all-jobs).

**Errors**: `422` for a `page` or `per_page` out of range or not an integer.

### `GET /api/jobs/{id}`

One of your jobs. With `wait`, the server holds the request until the job finishes or the
time is up, so a client learns of the result as soon as it is there, without polling
quickly.

**Query parameters**

| Parameter | Default | |
|---|---|---|
| `wait` | `0` | Seconds to wait for the job to become `done` or `failed`, from 0 to 600. |

**Response** `200`: the [job](#the-job-object). It is returned as soon as the job is
finished (checked every second), when `wait` seconds have passed (then the job may still be
`queued` or `processing`: ask again), or at once with `wait=0`.

```bash
curl -H "$AUTH" "$URL/api/jobs/42?wait=50"
```

Behind a reverse proxy, keep `wait` below the proxy's read timeout (60 seconds by default
for nginx), or the proxy ends the request with a `504`. Asking again in a loop with
`wait=50` works everywhere, and also survives a restart of the server (see
[Waiting for a job](#waiting-for-a-job)). Give your HTTP client a timeout longer than `wait`.

**Errors**: `404` (`no such job`) for a job that does not exist, belongs to another user, or
was deleted; `422` for an invalid `id` or `wait`.

### `DELETE /api/jobs/{id}`

Deletes one of your jobs, whatever its status, with its archive, PDF and log. A `queued`
job leaves the queue; a `processing` job's compilation is stopped.

**Response** `204`, without a body.

**Errors**: `404` (`no such job`); `422` for an invalid `id`.

### `DELETE /api/jobs`

Deletes all your jobs, whatever their status (other users' jobs are not affected).

**Response** `200`:

```json
{"deleted": 12}
```

`deleted` is the number of jobs deleted, `0` if you had none.

### `GET /api/jobs/{id}/input`

Downloads the archive as it was uploaded.

**Response** `200`, `application/octet-stream`, with
`Content-Disposition: attachment; filename="paper.zip"` (the job's `filename`; names
beyond ASCII are given as `filename*=utf-8''...`).

**Errors**: `404` (`no such job`, or `file not found`); `422` for an invalid `id`.

### `GET /api/jobs/{id}/pdf`

Downloads the PDF of a job whose `has_pdf` is `true`.

**Response** `200`, `application/pdf`, with `Content-Disposition: inline;
filename="main.pdf"` (named after the main file).

**Errors**: `404` with `this job has no PDF` (the job is not finished, or produced none) or
`no such job`; `422` for an invalid `id`.

### `GET /api/jobs/{id}/log`

Downloads the [log](#results) of a finished job (`has_log` is `true`).

**Response** `200`, `text/plain; charset=utf-8`, with `Content-Disposition: inline;
filename="main.log"`.

**Errors**: `404` with `the log is available once the job has finished` (the job is
`queued` or `processing`) or `no such job`; `422` for an invalid `id`.

The three downloads also send `Content-Length`, `ETag` and `Last-Modified`, and honour a
`Range` header (`Range: bytes=0-1023` gets `206 Partial Content`), so interrupted downloads
can be resumed.

### `GET /api/session`

Returns the user the request is authenticated as. Useful to check a token.

**Response** `200`:

```json
{"user": "alice"}
```

**Errors**: `401` for a missing or unknown token.

### `POST /api/session`

Logs a browser in: checks a token and, if it is valid, stores it in the `latex_token`
cookie (`HttpOnly`, `SameSite=Strict`, `Path=/`, valid for 30 days, and `Secure` when the
server is reached over HTTPS). Used by the web page's login form; programs do not need it.

**Request**: JSON

```json
{"token": "4c1f0a6e9b..."}
```

**Response** `200`: `{"user": "alice"}`, with the `Set-Cookie` header.

**Errors**: `401` (`invalid token`); `422` for a body without `token`, or a token longer
than 256 characters.

### `DELETE /api/session`

Logs a browser out: deletes the cookie. Needs no token.

**Response** `204`, without a body.

## Examples

### Compiling from the shell

A script that submits a project, waits for it, and saves the PDF and the log next to the
archive. It needs `curl` and [`jq`](https://jqlang.org/), and the URL and the token in
`LATEX_URL` and `LATEX_TOKEN`:

```bash
#!/bin/bash
# Compile a LaTeX project: ./compile.sh paper.zip [main.tex] [engine]
set -euo pipefail
archive=$1 main=${2:-} engine=${3:-pdflatex}
auth="Authorization: Bearer $LATEX_TOKEN"
# Downloads and status requests, retried for a minute should the server restart meanwhile
get() { curl -sS --fail --retry 12 --retry-delay 5 --retry-all-errors -H "$auth" "$@"; }

job=$(curl -sS --fail-with-body -H "$auth" -F "file=@$archive" --form-string "main=$main" \
  --form-string "engine=$engine" "$LATEX_URL/api/jobs") || { echo "refused: $job" >&2; exit 1; }
id=$(jq -r .id <<< "$job")
status=$(jq -r .status <<< "$job")
while [ "$status" = queued ] || [ "$status" = processing ]; do
  job=$(get "$LATEX_URL/api/jobs/$id?wait=50")
  status=$(jq -r .status <<< "$job")
done

output=$(sed -E 's/(\.tar)?\.(zip|7z|tar|tgz|gz|bz2|xz)$//' <<< "$archive")   # paper.tar.gz -> paper
get -o "$output.log" "$LATEX_URL/api/jobs/$id/log"
if [ "$(jq -r .has_pdf <<< "$job")" = true ]; then
  get -o "$output.pdf" "$LATEX_URL/api/jobs/$id/pdf"
fi
echo "job $id: $status"
if [ "$status" = failed ]; then jq -r .error <<< "$job" >&2; exit 1; fi
```

```console
$ ./compile.sh paper.zip
job 42: done
$ ./compile.sh broken.zip
job 43: failed
./main.tex:3: Undefined control sequence.
```

### Compiling from Python

The same in Python, with [requests](https://requests.readthedocs.io/) (`pip install
requests`). `compile_project()` can be used from other code; it returns the finished job.

```python
"""Compile a LaTeX project: python compile.py paper.zip [main.tex] [engine]

Needs LATEX_URL and LATEX_TOKEN in the environment. Saves the PDF and the log
next to the archive, and exits with the error if the compilation failed.
"""

import os
import re
import sys
import time
from pathlib import Path

import requests

URL = os.environ["LATEX_URL"].rstrip("/")
session = requests.Session()
session.headers["Authorization"] = f"Bearer {os.environ['LATEX_TOKEN']}"


def error_message(response: requests.Response) -> str:
    """Why the server refused a request. `detail` is a message, or a list for invalid parameters (422)."""
    try:
        detail = response.json()["detail"]
    except (ValueError, KeyError):
        return f"{response.status_code} {response.reason}"
    if isinstance(detail, list):
        return "; ".join(f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in detail)
    return detail


def get(path: str, **params) -> requests.Response:
    """A GET request, retried for a minute should the server restart meanwhile."""
    for _ in range(12):
        try:
            response = session.get(URL + path, params=params, timeout=90)
            if response.status_code < 500:
                return response
        except (requests.ConnectionError, requests.Timeout):
            pass
        time.sleep(5)
    raise RuntimeError(f"the server does not answer GET {path}")


def compile_project(archive: Path, main: str | None = None, engine: str = "pdflatex") -> dict:
    """Submit the archive, wait for the result, and save the PDF and the log next to it."""
    with archive.open("rb") as file:
        response = session.post(
            f"{URL}/api/jobs", files={"file": (archive.name, file)}, data={"main": main or "", "engine": engine}
        )
    if response.status_code != 201:
        raise RuntimeError(error_message(response))
    job = response.json()
    while job["status"] in ("queued", "processing"):
        response = get(f"/api/jobs/{job['id']}", wait=50)
        if response.status_code != 200:
            raise RuntimeError(error_message(response))  # deleted meanwhile
        job = response.json()

    stem = re.sub(r"(\.tar)?\.(zip|7z|tar|tgz|gz|bz2|xz)$", "", archive.name)  # paper.tar.gz -> paper
    archive.with_name(f"{stem}.log").write_bytes(get(f"/api/jobs/{job['id']}/log").content)
    if job["has_pdf"]:
        archive.with_name(f"{stem}.pdf").write_bytes(get(f"/api/jobs/{job['id']}/pdf").content)
    return job


if __name__ == "__main__":
    finished = compile_project(Path(sys.argv[1]), *sys.argv[2:4])
    print(f"job {finished['id']}: {finished['status']}")
    if finished["status"] == "failed":
        sys.exit(finished["error"])
```

### Letting a person choose the main file

An application with a user interface can do what the web page does: inspect the archive
first, offer its `tex_files` with the detected `main` preselected, then submit with the
chosen file.

```bash
curl -H "$AUTH" -F file=@project.zip $URL/api/inspect
# {"format":"zip","tex_files":["paper.tex","response-to-reviewers.tex"],"main":null}
curl -H "$AUTH" -F file=@project.zip -F main=paper.tex $URL/api/jobs
```

The archive is uploaded twice. When the main file is known in advance, submit directly:
the submission checks everything the inspection does, and `400` explains what is wrong.

### Waiting for a job

There are two ways to learn that a job has finished:

- **Waiting** (recommended): call `GET /api/jobs/{id}?wait=50` in a loop until the status is
  `done` or `failed`. Each call returns within a second of the job finishing. Between
  calls there is no need to pause.
- **Polling**: call `GET /api/jobs/{id}` every few seconds. The web page does this for the
  job list (every 2 seconds while a job is queued or compiling).

In both cases treat connection errors and `5xx` statuses as temporary (the server may be
restarting) and try again after a few seconds, as the examples above do. A `404` means the
job was deleted.

How long a job takes depends on the queue: the jobs ahead of it (`queue_position`, and
`queue` in [`GET /api/info`](#get-apiinfo)) compile first, each for up to
`compile_timeout_minutes`.

### Listing all jobs

Request pages until a page has fewer jobs than `per_page`:

```python
def all_jobs() -> list[dict]:
    jobs, page = [], 1
    while True:
        listed = get("/api/jobs", page=page, per_page=100).json()
        jobs += listed["jobs"]
        if len(listed["jobs"]) < listed["per_page"]:
            return jobs
        page += 1
```

Jobs submitted meanwhile push older jobs to later pages, so a job can appear twice (drop
duplicates by `id`), and jobs deleted meanwhile pull them forward, so one can be missed.

### Cleaning up

Jobs are kept until they are deleted, and they count towards the user's storage when
`max_user_storage_mb` is set. An application that only needs the results can delete each
job once it has downloaded them:

```bash
curl -H "$AUTH" -X DELETE $URL/api/jobs/42          # one job: 204
curl -H "$AUTH" -X DELETE $URL/api/jobs             # all your jobs: {"deleted": 12}
```

### Checking the limits before uploading

[`GET /api/info`](#get-apiinfo) gives the limits in effect. A client can refuse a file that
is too large, and warn when the user's storage is nearly full, before uploading anything:

```python
info = get("/api/info").json()
size = archive.stat().st_size
if size > info["server"]["max_upload_mb"] * 1024**2:
    raise RuntimeError(f"the archive is larger than {info['server']['max_upload_mb']} MB")
limit = info["server"]["max_user_storage_mb"] * 1024**2
if limit and info["account"]["storage_bytes"] + size > limit:
    raise RuntimeError("not enough storage left: delete old jobs first")
```

## Writing a client

- Send the token in the `Authorization` header, and keep it secret: it gives full access to
  the user's jobs. Use HTTPS when the server is not on a trusted network.
- Treat `401` as "the token is wrong or was revoked": ask for a new one rather than retry.
- Retry requests that failed with a connection error, a timeout or a `5xx` status after a
  few seconds; they are temporary. Do not retry `4xx` errors unchanged: `detail` says what
  to change.
- Do not resubmit (`POST /api/jobs`) automatically after a broken connection: check the job
  list first, or you may compile the project twice.
- Read `detail` as a string, or as a list for `422`. Show it to people; do not parse it.
- Ignore unknown fields in responses: later versions may add some.
- Set HTTP timeouts above `wait` for waiting requests, and above the upload time for
  submissions.
- Download the PDF and the log before deleting a job, and before `auto_delete_days` have
  passed.
- Job ids are integers that fit in 64 bits. Ids grow by one per submission, so they stay
  far below the 2<sup>53</sup> limit of JavaScript numbers.
- Calls from a web page on another origin do not work (no CORS): call the API from a
  server.

## OpenAPI

The server describes its API in the OpenAPI format, for tools and client generators:

| URL | |
|---|---|
| `/openapi.json` | The OpenAPI document (JSON). |
| `/docs` | Interactive documentation (Swagger UI), where requests can be tried after entering a token under **Authorize**. Linked from the web page's Server Info. |
| `/redoc` | The same documentation, as one page (ReDoc). |

The two documentation pages load their scripts from a public CDN, so they need Internet
access in the browser. This document is the reference for what the OpenAPI description does
not say: how jobs behave, the rules for archives and main files, and the error messages.
