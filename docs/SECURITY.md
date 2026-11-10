# Security

Every uploaded document is treated as hostile: TeX is a programming language, and a
document can read files, run the programs TeX Live allows, and use as much time, memory and
disk as it is given. This page describes what keeps documents and users apart, and how to
[report a vulnerability](#reporting-a-vulnerability).

## Protections

- The container runs as the unprivileged user `app`, uid and gid 61001, never as root. The
  number is above the range Linux gives to real accounts (up to 60000), so it matches no
  login of the host: should someone break out of the container, they would hold an id that
  owns nothing there, rather than one such as uid 1000, which often has passwordless `sudo`.
  The server makes itself and everything it starts unable to gain privileges (as Docker's
  `no-new-privileges` does), and the image has an init process that reaps processes
  left behind, so a plain `docker run` is as safe as `compose.yaml`.
- Every compilation runs in a [Landlock](https://docs.kernel.org/userspace-api/landlock.html)
  sandbox (no privileges needed). It can read the system and TeX Live but no other
  file: not other users' jobs, not `users.json`, not the database. It can write only
  its own work directory, and cannot signal processes outside it. TeX's own file
  restrictions are no substitute (`\input{/abs/path}` and Lua's `io.open` read any
  file). The sandbox has all its protections on Linux 6.12 or newer (x86-64 or ARM64);
  elsewhere the server names what is missing and refuses to start unless
  `ALLOW_UNSANDBOXED=1` is set (see [Older kernels](#older-kernels)).
  Check the Docker host with `cat /sys/kernel/security/lsm`: the list must contain
  `landlock`.
- A system-call filter (seccomp, on x86-64 and ARM64) keeps compilations off the
  network: they cannot open any socket but a local UNIX one, so neither TCP nor UDP.
- A supervisor process outside the sandbox kills whatever a compilation leaves
  behind, after every job and when it is stopped, also processes that detached
  themselves into a new session. Compilations are the first processes the kernel's
  out-of-memory killer stops, ahead of the server.
- Archives are unpacked (and inspected) by a separate helper process, never by the
  server, which can read every user's files. The helper confines itself the same way
  before it reads the archive: it can read only the archive and system files, write
  only into the job's directory, cannot use the network or start processes, and gets
  5 minutes and 4 GB of memory by default (`UNPACK_TIMEOUT_MINUTES`, `UNPACK_MEMORY_MB`).
  The server checks what it reports.
- TeX runs with restricted `\write18` (only TeX Live's whitelisted programs, which
  is enough for `minted`), `openout_any=p`, a clean environment, and `latexmk -norc`
  (a project cannot inject a `latexmkrc`). Files it produces are copied out without
  following links.
- Archives are recognised by content, may contain only regular files and folders
  with relative paths, and are limited in size and entry count. Main file names are
  limited to characters that are safe on a shell command line.
- Tokens are compared in constant time and accepted only in the `Authorization`
  header, never in URLs. `python -m texdrop adduser` and `reset-token` make random 256-bit
  tokens. The users file holds the tokens as they are, so `adduser` creates it readable only
  by its owner; keep it that way.
- The web page's cookie holds a session, not the token: a random 256-bit id, which the
  server keeps in `sessions.json` only as its SHA-256, with that of the token it was started
  with. Logging out ends the session on the server, so the cookie stops working everywhere,
  also a copy of it taken before. A session also ends after 30 days, and as soon as its user
  is removed or given a new token (`reset-token` logs every browser of the user out). The
  cookie is `HttpOnly`, `SameSite=Strict`, `Secure` over HTTPS, and changes made with it
  require a header that other sites cannot send. Pages are served with a strict Content
  Security Policy.
- With `PUBLIC_BASE_URL` set, a `POST`, `PUT`, `PATCH` or `DELETE` whose `Origin` header is
  not exactly its scheme, host and port is refused (`403`) before anything else happens, the
  token and the body included (without it, the server cannot tell its own address behind a
  proxy, so `Origin` is not checked; the cookie's protections still apply). That stops forged requests from every other
  site, sibling subdomains such as `blog.example.com` included (`SameSite` cookies take those
  for the same site), also to the MCP server and the transfer links. Browsers always send
  `Origin` with these requests; requests without it (curl, scripts, agents) cannot be forged
  by a web page, so they pass, and still need a token.
- The [MCP server](MCP.md) for AI agents is off unless `ENABLE_MCP=1`. It accepts the
  token only in the `Authorization` header (not the cookie), and its tools run the API's
  code, with its checks. Files move through transfer links, whose URLs hold a 256-bit
  random secret instead of the token: a link works only from the address that asked for
  it, for one complete transfer of one file, and for `LINK_EXPIRY_MINUTES`. Every refusal
  gets the same `404`; the server keeps only the secrets' SHA-256 and never logs them.
- Use HTTPS (a reverse proxy) when the service is reachable beyond a trusted network.

Time, memory, disk and storage can be limited further with the settings in
[CONFIGURATION.md](CONFIGURATION.md) and the [container's limits](DEPLOYMENT.md#container-limits).

## Older kernels

The sandbox uses each protection the kernel offers. When one is missing, the server
does not start: it logs an error that names each missing protection, then how to proceed.
With `ALLOW_UNSANDBOXED=1` it starts on any kernel and logs a "reduced protection"
warning for each one instead. Every kind of document compiles the same everywhere; what
changes is what a hostile document could do.

| Kernel | What compilations cannot do |
|---|---|
| before 5.13, or Landlock disabled | Nothing is isolated: documents can read and change every file the server can, including all tokens. |
| 5.13 | Read or write files outside their own directory (except system files, which they can read). |
| 6.2 | Also empty (truncate) outside files through Landlock; before, the system-call filter blocks it. |
| 6.7 | Also open TCP connections through Landlock; the system-call filter blocks all networking anyway. |
| 6.12 | Also send signals to processes outside the sandbox (the server, other processes of its user) or reach local services through abstract UNIX sockets. |

Independent of the kernel version:

- The system-call filter (no network, no `io_uring`) needs x86-64 or ARM64 and a kernel that
  allows it (seccomp, standard since Linux 3.5). Without it documents can use the network.
- Processes a document leaves behind are found through `/proc`; without it only those still in
  the compilation's process group are killed.
- Run the server as an unprivileged user: as root, documents run as root too.

## Reporting a vulnerability

Report vulnerabilities privately, with
[Report a vulnerability](https://github.com/nikolaos572/TeXdrop/security/advisories/new)
on the repository's Security tab, never in a public issue. Please include:

- what an attacker can do, and what they need (a user's token, or nothing);
- a document or archive that shows it, and the steps to reproduce it;
- the TeXdrop version or commit, the kernel version (`uname -r`), and whether the service
  runs in Docker.

The report is confirmed and the fix prepared in private. Once it is released, the issue is
published as a security advisory that credits the reporter (unless they prefer not to be
named). Please keep the issue private until then. Fixes go into the latest version only.

These are vulnerabilities:

- A document or archive that gets past a protection described above: it reads or changes
  another job's files, `users.json` or the database, writes outside its own directory, uses
  the network, signals processes outside its sandbox, or keeps running after its job ends.
- Access to another user's jobs, or to the API without a valid token.
- A transfer link of the MCP server that works from another address, after it expired or
  was used up, for another file, or for another user.
- Script injection into the web page, or a change another site can make with a user's cookie.
- A way around a configured limit (compilation time, memory or disk, upload size, storage)
  or a [fixed limit](CONFIGURATION.md#fixed-limits).

These are not:

- What a document can do with `ALLOW_UNSANDBOXED=1` or on a kernel before 6.12: the missing
  protections are listed under [Older kernels](#older-kernels).
- A document using the time, memory and disk it is allowed: set the limits in
  [CONFIGURATION.md](CONFIGURATION.md) and the [container's limits](DEPLOYMENT.md#container-limits).
- Tokens or transfer links read off a connection without HTTPS.
- Transfer links bound to the wrong address because of the deployment: with
  `FORWARDED_ALLOW_IPS=*` (the default) while TeXdrop's port can be reached without the proxy,
  or behind a proxy that `FORWARDED_ALLOW_IPS` does not name.
- What an AI agent does with the errors and logs of the documents it compiles.
- Bugs in TeX Live, Ghostscript or the other programs in the image: report them to their
  maintainers. They are a TeXdrop vulnerability when they let a document get past the sandbox.
