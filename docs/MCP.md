# MCP server

AI agents such as Claude Code or Antigravity (`agy`) can use TeXdrop by themselves: with
the MCP server on, an agent connects to `https://<your server>/mcp` with a user's token,
finds the operations TeXdrop offers, and compiles projects, follows their jobs and
downloads the PDFs and logs. It works with the same jobs as the web page and the API: a job
an agent submits appears on the web page, and the other way round.

[MCP](https://modelcontextprotocol.io) (Model Context Protocol) is the standard way for AI
agents to discover and call a service's operations, called tools.

**Contents**

1. [Turning it on](#turning-it-on)
2. [Connecting an agent](#connecting-an-agent)
3. [How an agent compiles a project](#how-an-agent-compiles-a-project)
4. [Tools](#tools)
5. [Transfer links](#transfer-links)
6. [Behind a reverse proxy](#behind-a-reverse-proxy)
7. [Security](#security)

## Turning it on

The MCP server is off by default. Turn it on with `ENABLE_MCP=1`:

```bash
docker run -d --name texdrop -p 8000:8000 -v "$PWD/data:/data" \
  -e ENABLE_MCP=1 \
  --cap-drop ALL --security-opt no-new-privileges --restart unless-stopped \
  texdrop
```

(or `ENABLE_MCP: "1"` under `environment:` in `compose.yaml`, or `ENABLE_MCP=1` in the
environment of `python -m texdrop`). The server then logs `MCP server at /mcp, transfer
links valid for 60 minute(s)` at startup, and **Server Info** on the web page shows the
address to give to agents, such as `https://texdrop.example.lan/mcp`.

| Variable | Default | Meaning |
|---|---|---|
| `ENABLE_MCP` | `0` | `1` serves the MCP server at `/mcp`, and the [transfer links](#transfer-links) under `/api/links/`. Off, both answer `404`. |
| `LINK_EXPIRY_MINUTES` | `60` | A transfer link not used by then stops working. |

## Connecting an agent

An agent needs the server's address with `/mcp` at the end, and a user's token, which it
sends in the `Authorization` header. It also needs to run shell commands, to pack the
project into an archive and to run the `curl` commands the tools give it.

**Claude Code**: add the server once, for all your projects:

```bash
claude mcp add --transport http --scope user texdrop https://texdrop.example.lan/mcp \
  --header "Authorization: Bearer <your token>"
```

or, for one project, in its `.mcp.json`, with the token taken from an environment variable
(so that the file can be shared without it):

```json
{
  "mcpServers": {
    "texdrop": {
      "type": "http",
      "url": "https://texdrop.example.lan/mcp",
      "headers": {"Authorization": "Bearer ${TEXDROP_TOKEN}"}
    }
  }
}
```

**Antigravity** (`agy`), whose flags come before the name:

```bash
agy mcp add --header "Authorization: Bearer <your token>" texdrop https://texdrop.example.lan/mcp
```

which writes the server into `~/.gemini/config/mcp_config.json`:

```json
{
  "mcpServers": {
    "texdrop": {
      "disabled": false,
      "headers": {"Authorization": "Bearer <your token>"},
      "serverUrl": "https://texdrop.example.lan/mcp"
    }
  }
}
```

`claude mcp list` checks the connection (`texdrop: ... - ✔ Connected`), and `/mcp` in a
session of either agent shows the server and its tools. Other agents work the same way if
they support MCP's Streamable HTTP transport with a custom header; the server needs no
OAuth.

Then ask the agent, for example, *"Compile the paper in this folder with TeXdrop and fix
the errors"*.

## How an agent compiles a project

The server tells the agent how to use it when it connects, so this happens without
instructions from you:

1. The agent packs the project's folder into a `.zip` (or `.tar.gz`, `.tar.bz2`, `.tar.xz`,
   `.tar` or `.7z`) archive.
2. It calls `upload_link` and runs the command it returns, such as
   `curl -sS --fail-with-body -F file=@paper.zip https://texdrop.example.lan/api/links/kV3...`,
   which prints the new job, with its id.
3. It calls `get_job` until the job is `done` or `failed`. A failed job's `error` has the
   first lines of TeX's errors, which is usually enough to fix the document.
4. It calls `download_link` for the PDF (or the full log) and runs the command it returns,
   such as `curl -sS --fail-with-body -o main.pdf https://texdrop.example.lan/api/links/Qe9...`.

## Tools

Each tool runs the code of an API endpoint, with the same checks, limits and answers
(described in [API.md](API.md)).

| Tool | Does | As the API's |
|---|---|---|
| `server_info` | The server's settings and limits, the queue, your account. | [`GET /api/info`](API.md#get-apiinfo) |
| `upload_link` | A link to upload a project, and the `curl` command that does it. | [`POST /api/jobs`](API.md#post-apijobs) |
| `list_jobs` | Your jobs, newest first, a page at a time (`page`, `per_page`). | [`GET /api/jobs`](API.md#get-apijobs) |
| `get_job` | One job; waits until it finishes, for at most `wait_seconds` (default and maximum 50). | [`GET /api/jobs/{id}`](API.md#get-apijobsid) |
| `download_link` | A link to download a job's `pdf`, `log` or `input` (the uploaded archive), and the `curl` command that saves it. | [`GET /api/jobs/{id}/pdf`](API.md#get-apijobsidpdf), ... |
| `delete_job` | Delete a job and its files, stopping its compilation. | [`DELETE /api/jobs/{id}`](API.md#delete-apijobsid) |
| `delete_all_jobs` | Delete all your jobs, also those from the web page and the API. | [`DELETE /api/jobs`](API.md#delete-apijobs) |

The tools carry hints that agents use to decide when to ask you: `server_info`, `list_jobs`
and `get_job` only read, and the two delete tools are destructive, so agents ask before
running them. Refusals reach the agent as tool errors with the API's message, such as
`no such job`. There is no tool for [`POST /api/inspect`](API.md#post-apiinspect): an upload
without a detectable main file is refused with the reason, and the agent tries again with
the main file named.

## Transfer links

A tool call carries only text (JSON) that the model writes and reads, so a project cannot
be uploaded, nor a PDF saved, through one. The agent moves files with `curl` instead,
through links that `upload_link` and `download_link` hand out. A link's URL holds a random
secret that stands in for the token, for one transfer:

- **Only from the address that asked for it.** The link works only for requests from the
  same IP address as the tool call that made it, normally the computer the agent runs on.
- **For a limited time.** It stops working `LINK_EXPIRY_MINUTES` (60) after it was made. A
  transfer that started in time may end after that.
- **For one complete transfer.** A job created from an upload, or a download of the whole
  file, uses the link up. A transfer that fails or is cut off leaves it valid, for another
  try: an upload that is refused (an invalid archive, a missing main file, a full storage)
  or interrupted, or a download cut off before its end. A download is complete when the
  server has sent the whole file: behind a reverse proxy, when the proxy has received it,
  as no server can know more.
- **Always renewable.** The agent can ask for new links at any time, as many as it needs:
  an expired or used link is never a dead end.

Whatever the reason a link is refused (it does not exist, expired, was used, is used from
another address, or its user was removed), the answer is the same `404`, which tells the
agent to ask for a new link:

```json
{"detail": "no such link: links expire, work once and only from the address they were made for; ask for a new one"}
```

The real reason goes to the server's log, for the administrator:

```
2026-10-22 16:05:11 +0300 INFO    10.0.0.9 - POST /api/links/*** 404 2 ms: link for 10.0.0.5 used from 10.0.0.9
```

The links' endpoints are described in [API.md](API.md#transfer-links-apilinkssecret).

## Behind a reverse proxy

The MCP server needs nothing special from a reverse proxy (nginx, nginx proxy manager,
Caddy, Traefik, ...): its answers are plain JSON, without event streams or WebSockets. Two
things matter, as for the rest of the service:

- **`FORWARDED_ALLOW_IPS` must name the proxy** (see
  [CONFIGURATION.md](CONFIGURATION.md#environment-variables)), and TeXdrop's port must be
  reachable only from the proxy. The server then takes each client's address from the
  proxy's `X-Forwarded-For`, which links are bound to, and the scheme from
  `X-Forwarded-Proto`, which the links' URLs use. Without it, every request seems to come
  from the proxy, so links would work from any client of the proxy, and their URLs would
  start with `http://` while agents connect with `https://`. With Docker, the container
  sees a proxy on the same machine coming from the Docker network's gateway (often
  `172.17.0.1`), or from the proxy container's address when both are on the same Docker
  network.
- **Timeouts and sizes.** `get_job` answers within 50 seconds, below nginx's default limit
  of 60. Uploads through links are as large as the archives: raise the proxy's limit
  (`client_max_body_size` for nginx) if it is below `MAX_UPLOAD_MB`.

## Security

- **Off unless turned on.** Without `ENABLE_MCP=1`, neither `/mcp` nor `/api/links/` exists.
- **The same token and the same rights.** The agent uses a user's token and can do what
  that user can do through the API, on all their jobs, including deleting them all; the
  delete tools are marked destructive so that agents ask first. The endpoint accepts the
  token only in the `Authorization` header, never the web page's cookie, so another web
  site cannot call it from your browser.
- **The token stays out of the model.** The agent's MCP client adds the header itself: the
  token is not in the tools' descriptions, arguments or results, nor in the commands the
  agent runs. It is stored in the client's configuration file, in plain text: keep that
  file private (and out of version control: use `${TEXDROP_TOKEN}` in `.mcp.json`).
- **Links are short-lived and narrow.** Their secrets have 256 random bits, work only from
  one address, for one transfer of one file, and for `LINK_EXPIRY_MINUTES`. The server keeps
  only their SHA-256, and their secrets never appear in its log (`/api/links/***`).
- **Encryption.** Over plain `http://`, the token, the links, the projects and the PDFs
  can be read by anyone who can see the network's traffic. Use HTTPS (a reverse proxy with
  a certificate) beyond a trusted network.
- **What compilations print reaches the agent.** The errors and the log a document produces
  are read by the agent. When compiling documents you did not write, keep in mind that they
  can print instructions aimed at it.

See also [SECURITY.md](SECURITY.md).
