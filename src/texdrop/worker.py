"""The queue worker: compiles jobs one at a time, oldest first, in a background thread.

The compilation itself is a separate process (latexmk and the TeX engine), so
it runs on its own CPU core while this thread just waits for it; the web server
keeps answering requests in the meantime.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable
from pathlib import Path

from . import logs
from .archive import ArchiveError
from .compiler import LatexCompiler
from .jobs import JobService
from .storage import Storage, remove_tree
from .store import Job

log = logging.getLogger(__name__)

POLL_SECONDS = 5.0  # safety net; submissions wake the worker immediately


class Worker:
    def __init__(self, service: JobService, compiler: LatexCompiler, storage: Storage) -> None:
        self._service = service
        self._compiler = compiler
        self._storage = storage
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._current: tuple[int, threading.Event] | None = None  # running job id and its cancel flag
        self._thread = threading.Thread(target=self._loop, name="compile-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self.cancel(None)
        self._thread.join(timeout=30)

    def wake(self) -> None:
        """Tell the worker a job was queued."""
        self._wake.set()

    def cancel(self, job_ids: Iterable[int] | None) -> None:
        """Stop the running compilation if it belongs to one of `job_ids` (None: any)."""
        with self._lock:
            if self._current and (job_ids is None or self._current[0] in set(job_ids)):
                self._current[1].set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                job = self._service.claim_next()
                if job is not None:
                    self.process(job)
                    continue
            except Exception:
                log.exception("worker error")
            self._wake.wait(POLL_SECONDS)

    def process(self, job: Job) -> None:
        """Compile one claimed job and record its result."""
        cancelled = threading.Event()
        with self._lock:
            self._current = (job.id, cancelled)
        workdir = self._storage.tmp_dir / f"job-{job.id}"
        outputs = self._storage.tmp_dir / f"job-{job.id}-output"  # outside the sandbox's reach
        start = time.monotonic()
        try:
            pdf, error = self._compile(job, workdir, outputs, cancelled)
            took = time.monotonic() - start
            if self._stop.is_set():
                outcome = "interrupted by the shutdown, runs again at the next start"
            elif not self._service.complete(job, pdf=pdf, log=outputs.with_suffix(".log"), error=error):
                outcome = "deleted while compiling"
            elif error is None:
                outcome = f"done in {took:.1f} s"
            else:
                reason = logs.one_line(error.partition("\n")[0])  # the error's first line
                outcome = f"failed after {took:.1f} s: {reason}"
            log.info("job %d (%s): %s with %s, %s", job.id, job.user, job.main, job.engine, outcome)
        finally:
            with self._lock:
                self._current = None
            remove_tree(workdir)
            for path in (outputs.with_suffix(".log"), outputs.with_suffix(".pdf")):
                path.unlink(missing_ok=True)

    def _compile(
        self, job: Job, workdir: Path, outputs: Path, cancelled: threading.Event
    ) -> tuple[Path | None, str | None]:
        """Extract and compile; returns the PDF (if any) and the error (if any). Always writes a log."""
        try:
            info = self._service.extract(job, workdir / "project")
            if job.main not in info.tex_files:
                raise ArchiveError(f"main file {job.main!r} is missing from the archive")
            result = self._compiler.run(
                workdir, workdir / "project" / info.root, job.main, job.engine, outputs, cancelled
            )
            return result.pdf, result.error
        except ArchiveError as exc:
            error = str(exc)
        except Exception:
            log.exception("job %d: unexpected error", job.id)
            error = "Internal error while compiling; see the server log."
        outputs.with_suffix(".log").write_text(error + "\n", encoding="utf-8")
        return None, error
