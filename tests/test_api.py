import dataclasses
import random

import pytest
from fastapi.testclient import TestClient
from helpers import MINIMAL_TEX, TOKENS, run_queue, submit

from texdrop import __version__
from texdrop.app import create_app


def inspect(client, archive):
    with archive.open("rb") as file:
        return client.post("/api/inspect", files={"file": (archive.name, file)})


# --- authentication ---


def test_health_needs_no_token(app):
    assert TestClient(app).get("/api/health").json() == {"status": "ok"}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-token-0123456789"}, {"Authorization": "Basic abc"}])
@pytest.mark.parametrize(
    ("method", "path"), [("GET", "/api/jobs"), ("DELETE", "/api/jobs"), ("GET", "/api/jobs/1/pdf"), ("GET", "/api/info")]
)
def test_requests_without_a_valid_token_are_rejected(app, headers, method, path):
    response = TestClient(app).request(method, path, headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_token_is_never_accepted_in_the_url(app):
    assert TestClient(app).get("/api/jobs", params={"token": TOKENS["alice"]}).status_code == 401


def test_cookie_authenticates_reads_but_changes_need_the_csrf_header(app):
    browser = TestClient(app, cookies={"latex_token": TOKENS["alice"]})
    assert browser.get("/api/jobs").status_code == 200
    assert browser.delete("/api/jobs").status_code == 403
    assert browser.delete("/api/jobs", headers={"X-Requested-With": "fetch"}).status_code == 200


# --- inspect ---


def test_inspect_single_main(client, make_archive):
    response = inspect(client, make_archive({"paper/main.tex": MINIMAL_TEX, "paper/s.tex": "x"}, "7z"))
    assert response.json() == {"format": "7z", "tex_files": ["main.tex", "s.tex"], "main": "main.tex"}


def test_inspect_ambiguous_and_empty(client, make_archive):
    assert inspect(client, make_archive({"a.tex": MINIMAL_TEX, "b.tex": MINIMAL_TEX})).json()["main"] is None
    assert inspect(client, make_archive({"a.txt": "x"})).json()["tex_files"] == []


def test_inspect_rejects_non_archives(client, tmp_path):
    path = tmp_path / "paper.zip"
    path.write_text("not an archive")
    response = inspect(client, path)
    assert response.status_code == 400
    assert "unsupported file type" in response.json()["detail"]


def test_inspect_leaves_no_files_behind(client, make_archive, settings):
    inspect(client, make_archive({"main.tex": MINIMAL_TEX}))
    assert list((settings.data_dir / "tmp").iterdir()) == []


# --- submit ---


def test_submit_detects_the_main_file(client, make_archive):
    response = submit(client, make_archive({"main.tex": MINIMAL_TEX}, "tar.xz"))
    assert response.status_code == 201
    job = response.json()
    assert (job["main"], job["engine"], job["status"], job["queue_position"]) == ("main.tex", "pdflatex", "queued", 1)
    assert job["submitted_at"].endswith("+03:00")  # shown in the configured time zone (Europe/Athens)
    assert (job["has_pdf"], job["has_log"], job["finished_at"]) == (False, False, None)


@pytest.mark.parametrize(
    ("files", "form", "message"),
    [
        ({"a.tex": MINIMAL_TEX, "b.tex": MINIMAL_TEX}, {}, "several .tex files found"),
        ({"main.tex": MINIMAL_TEX}, {"main": "other.tex"}, "is not a .tex file in the archive"),
        ({"main.tex": MINIMAL_TEX}, {"main": "../../config.json"}, "is not a .tex file in the archive"),
        ({"notes.md": "x"}, {}, "no .tex file"),
        ({"notes.md": "x"}, {"main": "notes.md"}, "no usable .tex file"),
    ],
)
def test_submit_repeats_the_browser_checks(client, make_archive, files, form, message):
    response = submit(client, make_archive(files), **form)
    assert response.status_code == 400
    assert message in response.json()["detail"]
    assert client.get("/api/jobs").json()["jobs"] == []


def test_submit_with_explicit_main_and_engine(client, make_archive):
    archive = make_archive({"a.tex": MINIMAL_TEX, "b.tex": MINIMAL_TEX})
    job = submit(client, archive, main=" b.tex ", engine="lualatex").json()
    assert (job["main"], job["engine"]) == ("b.tex", "lualatex")


@pytest.mark.parametrize("form", [{"engine": "luatex; rm -rf /"}, {"main": "x" * 2000}])
def test_submit_validates_form_fields(client, make_archive, form):
    assert submit(client, make_archive({"main.tex": MINIMAL_TEX}), **form).status_code == 422


def test_submit_rejects_large_uploads(client, make_archive, settings):
    big = make_archive({"main.tex": MINIMAL_TEX, "data.bin": bytes(range(256)) * 12_000}, "tar")  # ~3 MB > 2 MB
    response = submit(client, big)
    assert response.status_code == 413
    assert list((settings.data_dir / "tmp").iterdir()) == []


def test_body_without_length_is_rejected(client):
    response = client.post("/api/jobs", content=iter([b"chunked"]), headers={"Content-Type": "application/zip"})
    assert response.status_code == 411


# --- list, get, downloads ---


def test_job_lifecycle_and_downloads(client, app, make_archive):
    archive = make_archive({"paper/main.tex": MINIMAL_TEX})
    job = submit(client, archive).json()
    assert client.get(f"/api/jobs/{job['id']}/pdf").status_code == 404
    assert client.get(f"/api/jobs/{job['id']}/log").status_code == 404
    run_queue(app)

    done = client.get(f"/api/jobs/{job['id']}").json()
    assert (done["status"], done["has_pdf"], done["has_log"], done["error"]) == ("done", True, True, None)
    assert client.get("/api/jobs").json() == {"jobs": [done], "total": 1, "page": 1, "per_page": 5}

    pdf = client.get(f"/api/jobs/{job['id']}/pdf")
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.headers["content-disposition"] == 'inline; filename="main.pdf"'
    assert pdf.content.startswith(b"%PDF")

    log = client.get(f"/api/jobs/{job['id']}/log")
    assert log.headers["content-type"] == "text/plain; charset=utf-8"
    assert "fake-latexmk" in log.text

    original = client.get(f"/api/jobs/{job['id']}/input")
    assert original.content == archive.read_bytes()
    assert original.headers["content-disposition"] == f'attachment; filename="{archive.name}"'


def test_failed_job_reports_errors_and_keeps_partial_pdf(client, app, make_archive):
    job = submit(client, make_archive({"main.tex": "PARTIAL FAIL"})).json()
    run_queue(app)
    failed = client.get(f"/api/jobs/{job['id']}").json()
    assert failed["status"] == "failed"
    assert failed["error"] == "./main.tex:3: Undefined control sequence."
    assert failed["has_pdf"] is True


def test_users_cannot_see_each_others_jobs(client, app, make_archive):
    job = submit(client, make_archive({"main.tex": MINIMAL_TEX})).json()
    run_queue(app)
    bob = TestClient(app, headers={"Authorization": f"Bearer {TOKENS['bob']}"})
    assert bob.get("/api/jobs").json()["total"] == 0
    for suffix in ("", "/input", "/pdf", "/log"):
        assert bob.get(f"/api/jobs/{job['id']}{suffix}").status_code == 404
    assert bob.delete(f"/api/jobs/{job['id']}").status_code == 404
    assert client.get(f"/api/jobs/{job['id']}").status_code == 200


def test_jobs_are_listed_a_page_at_a_time(client, app, make_archive):
    ids = [submit(client, make_archive({"main.tex": MINIMAL_TEX})).json()["id"] for _ in range(12)]
    bob = TestClient(app, headers={"Authorization": f"Bearer {TOKENS['bob']}"})
    submit(bob, make_archive({"main.tex": MINIMAL_TEX}))  # not counted for alice

    def page(**params):
        listed = client.get("/api/jobs", params=params).json()
        return [job["id"] for job in listed["jobs"]], listed["total"]

    assert page() == (ids[:6:-1], 12)  # the newest 5
    assert page(page=2) == (ids[6:1:-1], 12)
    assert page(page=3) == (ids[1::-1], 12)
    assert page(page=4) == ([], 12)
    assert page(page=1, per_page=10) == (ids[:1:-1], 12)
    assert page(page=1_000_000, per_page=100) == ([], 12)


@pytest.mark.parametrize(
    "params",
    [
        {"page": 0},
        {"page": -1},
        {"page": 1_000_001},
        {"page": 10**30},
        {"page": "1; DROP TABLE jobs"},
        {"per_page": 0},
        {"per_page": 101},
        {"per_page": "all"},
    ],
)
def test_list_validates_paging(client, params):
    assert client.get("/api/jobs", params=params).status_code == 422


@pytest.mark.parametrize("path", ["/api/jobs/abc", "/api/jobs/1.5/pdf", "/api/jobs/..%2F..%2Fconfig.json"])
def test_job_ids_must_be_integers(client, path):
    assert client.get(path).status_code in (404, 422)


@pytest.mark.parametrize("job_id", [0, -1, 2**63, 10**30])
def test_job_ids_out_of_range_are_invalid(client, job_id):
    for suffix in ("", "/input", "/pdf", "/log"):
        assert client.get(f"/api/jobs/{job_id}{suffix}").status_code == 422
    assert client.delete(f"/api/jobs/{job_id}").status_code == 422


def test_largest_job_id_is_valid(client):
    assert client.get(f"/api/jobs/{2**63 - 1}").status_code == 404


def test_wait_returns_when_the_time_is_up(client, make_archive):
    job = submit(client, make_archive({"main.tex": MINIMAL_TEX})).json()
    response = client.get(f"/api/jobs/{job['id']}", params={"wait": 1})
    assert response.json()["status"] == "queued"
    assert client.get(f"/api/jobs/{job['id']}", params={"wait": 100_000}).status_code == 422


# --- delete ---


def test_delete_one_job(client, app, make_archive, settings):
    job = submit(client, make_archive({"main.tex": MINIMAL_TEX})).json()
    run_queue(app)
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 204
    assert client.get(f"/api/jobs/{job['id']}").status_code == 404
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 404
    assert list((settings.data_dir / "users" / "alice" / "inputs").iterdir()) == []
    assert list((settings.data_dir / "users" / "alice" / "outputs").iterdir()) == []


def test_delete_all_jobs_whatever_their_status(client, app, make_archive):
    submit(client, make_archive({"main.tex": MINIMAL_TEX}))
    run_queue(app)
    submit(client, make_archive({"main.tex": MINIMAL_TEX}))
    bob = TestClient(app, headers={"Authorization": f"Bearer {TOKENS['bob']}"})
    submit(bob, make_archive({"main.tex": MINIMAL_TEX}))

    assert client.delete("/api/jobs").json() == {"deleted": 2}
    assert client.get("/api/jobs").json()["total"] == 0
    assert bob.get("/api/jobs").json()["total"] == 1


# --- info ---


def test_info_reports_settings_queue_and_usage(client, app, make_archive, settings):
    submit(client, make_archive({"main.tex": MINIMAL_TEX}))
    run_queue(app)
    submit(client, make_archive({"main.tex": MINIMAL_TEX}))
    bob = TestClient(app, headers={"Authorization": f"Bearer {TOKENS['bob']}"})
    submit(bob, make_archive({"main.tex": MINIMAL_TEX}))

    info = client.get("/api/info").json()
    server = info["server"]
    assert server["version"] == __version__
    assert (server["engines"], server["default_engine"]) == (["pdflatex", "xelatex", "lualatex"], "pdflatex")
    assert (server["timezone"], server["auto_delete_days"]) == ("Europe/Athens", 0)
    assert (server["compile_timeout_minutes"], server["max_upload_mb"], server["max_extracted_mb"]) == (0.5, 2, 20)
    assert (server["compile_memory_mb"], server["compile_disk_mb"], server["max_user_storage_mb"]) == (0, 0, 0)
    assert (server["unpack_timeout_minutes"], server["unpack_memory_mb"]) == (5, 4096)
    assert (server["mcp_path"], server["link_expiry_minutes"]) == (None, 60)  # MCP is off unless ENABLE_MCP=1
    assert info["queue"] == {"queued": 2, "processing": 0}  # of all users
    files = [path for path in (settings.data_dir / "users" / "alice").rglob("*") if path.is_file()]
    assert len(files) == 4  # two archives, a PDF and a log
    assert info["account"] == {"user": "alice", "jobs": 2, "storage_bytes": sum(path.stat().st_size for path in files)}

    client.delete("/api/jobs")
    assert client.get("/api/info").json()["account"] == {"user": "alice", "jobs": 0, "storage_bytes": 0}


def test_storage_limit_refuses_uploads_that_do_not_fit(settings, fake_compiler, make_archive):
    app = create_app(dataclasses.replace(settings, max_user_storage_bytes=1024**2), fake_compiler)
    alice = TestClient(app, headers={"Authorization": f"Bearer {TOKENS['alice']}"})
    bob = TestClient(app, headers={"Authorization": f"Bearer {TOKENS['bob']}"})

    def archive():  # about 600 KB that does not compress
        return make_archive({"main.tex": MINIMAL_TEX, "noise.bin": random.Random(0).randbytes(600 * 1024)})

    first = submit(alice, archive())
    assert first.status_code == 201
    refused = submit(alice, archive())
    assert refused.status_code == 400
    assert refused.json()["detail"].startswith("not enough storage: your jobs take 600 KB of your 1.0 MB")
    assert refused.json()["detail"].endswith("delete old jobs first")
    assert submit(bob, archive()).status_code == 201  # each user has their own
    assert alice.get("/api/info").json()["server"]["max_user_storage_mb"] == 1

    alice.delete(f"/api/jobs/{first.json()['id']}")
    assert submit(alice, archive()).status_code == 201


def test_info_for_a_user_who_never_submitted(app):
    bob = TestClient(app, headers={"Authorization": f"Bearer {TOKENS['bob']}"})
    assert bob.get("/api/info").json()["account"] == {"user": "bob", "jobs": 0, "storage_bytes": 0}


def test_security_headers(client):
    headers = client.get("/api/jobs").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
