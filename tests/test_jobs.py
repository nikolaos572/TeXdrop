import pytest
from helpers import MINIMAL_TEX

from texdrop.archive import ArchiveError
from texdrop.errors import InvalidInput
from texdrop.jobs import JobService, safe_filename
from texdrop.storage import Storage
from texdrop.store import JobStore, Status
from texdrop.unpack import Unpacker


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def storage(tmp_path):
    storage = Storage(tmp_path / "data")
    storage.prepare()
    return storage


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(storage, clock):
    return JobService(JobStore(storage.db_path), storage, Unpacker(10 * 1024**2), clock)


def test_submit_stores_the_archive(service, storage, make_archive):
    upload = make_archive({"paper/main.tex": MINIMAL_TEX}, "tar.gz")
    job = service.submit("alice", upload, "C:\\Users\\me\\paper.tar.gz", None, "xelatex")
    assert (job.filename, job.extension, job.main, job.engine) == ("paper.tar.gz", ".tar.gz", "main.tex", "xelatex")
    assert not upload.exists()
    assert service.input_file(job) == storage.root / "users" / "alice" / "inputs" / f"{job.id}.tar.gz"
    assert service.input_file(job).is_file()


def test_submit_without_filename_gets_a_default(service, make_archive):
    assert service.submit("alice", make_archive({"main.tex": MINIMAL_TEX}, "7z"), None, None, "pdflatex").filename == "upload.7z"


@pytest.mark.parametrize(
    ("files", "main", "engine", "error"),
    [
        ({"main.tex": MINIMAL_TEX}, None, "context", InvalidInput),
        ({"a.tex": MINIMAL_TEX, "b.tex": MINIMAL_TEX}, None, "pdflatex", ArchiveError),
        ({"main.tex": MINIMAL_TEX}, "missing.tex", "pdflatex", ArchiveError),
        ({"notes.txt": "x"}, None, "pdflatex", ArchiveError),
    ],
)
def test_invalid_submissions_create_no_job(service, storage, make_archive, files, main, engine, error):
    upload = make_archive(files)
    with pytest.raises(error):
        service.submit("alice", upload, "p.zip", main, engine)
    assert service.list("alice") == ([], 0)
    assert not (storage.root / "users" / "alice").exists()


def test_complete_stores_outputs(service, storage, make_archive, tmp_path):
    job = service.submit("alice", make_archive({"main.tex": MINIMAL_TEX}), "p.zip", None, "pdflatex")
    claimed = service.claim_next()
    (tmp_path / "out.pdf").write_bytes(b"%PDF")
    (tmp_path / "out.log").write_text("log")
    assert service.complete(claimed, pdf=tmp_path / "out.pdf", log=tmp_path / "out.log", error=None)
    stored = service.get("alice", job.id)
    assert (stored.status, stored.has_pdf) == (Status.DONE, True)
    assert service.pdf_file(stored).read_bytes() == b"%PDF"
    assert service.log_file(stored).read_text() == "log"


def test_complete_after_delete_discards_outputs(service, storage, make_archive, tmp_path):
    job = service.submit("alice", make_archive({"main.tex": MINIMAL_TEX}), "p.zip", None, "pdflatex")
    claimed = service.claim_next()
    service.delete("alice", job.id)
    (tmp_path / "out.log").write_text("log")
    assert not service.complete(claimed, pdf=None, log=tmp_path / "out.log", error="x")
    assert not service.log_file(job).exists()


def test_delete_removes_files(service, make_archive, tmp_path):
    jobs = [service.submit("alice", make_archive({"main.tex": MINIMAL_TEX}), "p.zip", None, "pdflatex") for _ in range(3)]
    other = service.submit("bob", make_archive({"main.tex": MINIMAL_TEX}), "p.zip", None, "pdflatex")
    assert [job.id for job in service.delete("alice", jobs[0].id)] == [jobs[0].id]
    assert not service.input_file(jobs[0]).exists()
    assert len(service.delete("alice")) == 2
    assert not any(service.input_file(job).exists() for job in jobs)
    assert service.input_file(other).exists()


def test_delete_expired(service, make_archive, clock, tmp_path):
    def finished_job():
        job = service.submit("alice", make_archive({"main.tex": MINIMAL_TEX}), "p.zip", None, "pdflatex")
        (tmp_path / "log").write_text("log")
        service.complete(service.claim_next(), pdf=None, log=tmp_path / "log", error="failed")
        return job

    old = finished_job()
    clock.now += 10 * 86_400
    recent = finished_job()
    queued = service.submit("alice", make_archive({"main.tex": MINIMAL_TEX}), "p.zip", None, "pdflatex")
    clock.now += 6 * 86_400

    assert service.delete_expired(15) == 1
    assert {job.id for job in service.list("alice")[0]} == {recent.id, queued.id}
    assert not service.log_file(old).exists() and not service.input_file(old).exists()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("paper.zip", "paper.zip"),
        ("../../etc/passwd", "passwd"),
        ("C:\\temp\\thesis v2.zip", "thesis v2.zip"),
        ('evil"; rm -rf.zip', "evil__ rm -rf.zip"),
        ("εργασία.zip", "εργασία.zip"),
        ("...", ""),
        (None, ""),
    ],
)
def test_safe_filename(name, expected):
    assert safe_filename(name) == expected
