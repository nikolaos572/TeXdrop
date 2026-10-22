# Security

Every uploaded document is treated as hostile: TeX is a programming language, and a
document can read files, run the programs TeX Live allows, and use as much time, memory and
disk as it is given. This page describes what keeps documents and users apart.

## Protections

- The container runs as the unprivileged user `app` (uid 1001), never as root. The
  server makes itself and everything it starts unable to gain privileges (as Docker's
  `no-new-privileges` does), and the image has an init process that reaps processes
  left behind, so a plain `docker run` is as safe as `compose.yaml`.
- Every compilation runs in a [Landlock](https://docs.kernel.org/userspace-api/landlock.html)
  sandbox (no privileges needed). It can read the system and TeX Live but no other
  file: not other users' jobs, not `config.json`, not the database. It can write only
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
  header or the cookie, never in URLs. The cookie is `HttpOnly`, `SameSite=Strict`,
  `Secure` over HTTPS, and changes made with it require a header that other sites
  cannot send. Pages are served with a strict Content Security Policy.
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
