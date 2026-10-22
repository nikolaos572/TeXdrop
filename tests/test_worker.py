import time

from fastapi.testclient import TestClient
from helpers import MINIMAL_TEX, TOKENS, run_queue, submit

from texdrop.app import create_app
from texdrop.store import Status


def wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def test_jobs_run_in_submission_order_across_users(app, client, make_archive):
    bob = TestClient(app, headers={"Authorization": f"Bearer {TOKENS['bob']}"})
    first = submit(client, make_archive({"main.tex": MINIMAL_TEX})).json()
    second = submit(bob, make_archive({"main.tex": MINIMAL_TEX})).json()
    third = submit(client, make_archive({"main.tex": MINIMAL_TEX})).json()
    assert [first["queue_position"], second["queue_position"], third["queue_position"]] == [1, 2, 3]

    run_queue(app)

    service = app.state.service
    jobs = [service.get("alice", first["id"]), service.get("bob", second["id"]), service.get("alice", third["id"])]
    assert all(job.status is Status.DONE for job in jobs)
    assert jobs[0].started_at <= jobs[1].started_at <= jobs[2].started_at


def test_failed_extraction_is_recorded_with_a_log(app, client, make_archive):
    job = submit(client, make_archive({"main.tex": MINIMAL_TEX})).json()
    app.state.service.input_file(app.state.service.get("alice", job["id"])).write_bytes(b"corrupted")
    run_queue(app)
    stored = app.state.service.get("alice", job["id"])
    assert stored.status is Status.FAILED
    assert "unsupported file type" in stored.error
    assert client.get(f"/api/jobs/{job['id']}/log").text.startswith("unsupported file type")


def test_work_files_are_cleaned_up(app, client, make_archive, settings):
    submit(client, make_archive({"main.tex": MINIMAL_TEX}))
    run_queue(app)
    assert list((settings.data_dir / "tmp").iterdir()) == []


def test_background_worker_compiles_and_server_stays_responsive(settings, fake_compiler, make_archive):
    with TestClient(create_app(settings, fake_compiler), headers={"Authorization": f"Bearer {TOKENS['alice']}"}) as client:
        slow = submit(client, make_archive({"main.tex": "SLEEP"})).json()
        quick = submit(client, make_archive({"main.tex": MINIMAL_TEX})).json()
        wait_for(lambda: client.get(f"/api/jobs/{slow['id']}").json()["status"] == "processing")

        start = time.monotonic()
        response = client.get("/api/jobs")
        assert time.monotonic() - start < 1.0  # answered while a compilation is running
        statuses = {job["id"]: (job["status"], job["queue_position"]) for job in response.json()["jobs"]}
        assert statuses == {slow["id"]: ("processing", None), quick["id"]: ("queued", 1)}

        # Deleting the running job stops it; the next one starts right away.
        assert client.delete(f"/api/jobs/{slow['id']}").status_code == 204
        done = client.get(f"/api/jobs/{quick['id']}", params={"wait": 20}).json()
        assert done["status"] == "done"


def test_shutdown_leaves_the_running_job_for_the_next_start(settings, fake_compiler, make_archive):
    app = create_app(settings, fake_compiler)
    with TestClient(app, headers={"Authorization": f"Bearer {TOKENS['alice']}"}) as client:
        job = submit(client, make_archive({"main.tex": "SLEEP"})).json()
        wait_for(lambda: client.get(f"/api/jobs/{job['id']}").json()["status"] == "processing")
    assert app.state.service.get("alice", job["id"]).status is Status.PROCESSING  # not failed
    assert app.state.service.requeue_interrupted() == 1


def test_interrupted_jobs_are_requeued_on_startup(settings, fake_compiler, make_archive):
    app = create_app(settings, fake_compiler)
    client = TestClient(app, headers={"Authorization": f"Bearer {TOKENS['alice']}"})
    job = submit(client, make_archive({"main.tex": MINIMAL_TEX})).json()
    app.state.service.claim_next()  # the server "crashed" while compiling it

    with TestClient(create_app(settings, fake_compiler), headers=client.headers) as restarted:
        assert restarted.get(f"/api/jobs/{job['id']}", params={"wait": 20}).json()["status"] == "done"
