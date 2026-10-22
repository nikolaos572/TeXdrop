import sqlite3
import threading
from contextlib import closing

import pytest

from texdrop.store import JobStore, Status


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path / "jobs.db")


def add(store, user, now=1000.0):
    with store.transaction(write=True) as jobs:
        return jobs.insert(user=user, filename="p.zip", extension=".zip", main="main.tex", engine="pdflatex", now=now)


def claim(store, now=2000.0):
    with store.transaction(write=True) as jobs:
        return jobs.claim_next(now)


def test_database_uses_wal(store, tmp_path):
    with closing(sqlite3.connect(tmp_path / "jobs.db")) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_insert_and_get(store):
    job = add(store, "alice", now=1234.5)
    assert job.status is Status.QUEUED
    assert (job.user, job.main, job.submitted_at, job.queue_position) == ("alice", "main.tex", 1234.5, 1)
    with store.transaction(write=False) as jobs:
        assert jobs.get("alice", job.id) == job
        assert jobs.get("bob", job.id) is None  # other users cannot see it


def test_queue_is_first_come_first_served_across_users(store):
    first, second = add(store, "alice"), add(store, "bob")
    assert claim(store).id == first.id
    third = add(store, "carol")  # arrives while the first is processing: must not overtake the second
    assert claim(store).id == second.id
    assert claim(store).id == third.id
    assert claim(store) is None


def test_queue_positions(store):
    ids = [add(store, user).id for user in ("alice", "bob", "alice", "bob")]
    claim(store)
    with store.transaction(write=False) as jobs:
        alice = {job.id: job.queue_position for job in jobs.list("alice")}
        bob = {job.id: job.queue_position for job in jobs.list("bob")}
    assert alice == {ids[0]: None, ids[2]: 2}  # the first one is processing
    assert bob == {ids[1]: 1, ids[3]: 3}


def test_list_is_newest_first(store):
    ids = [add(store, "alice").id for _ in range(3)]
    with store.transaction(write=False) as jobs:
        assert [job.id for job in jobs.list("alice")] == ids[::-1]


def test_list_a_page_and_count(store):
    ids = [add(store, "alice").id for _ in range(5)]
    add(store, "bob")
    with store.transaction(write=False) as jobs:
        assert [job.id for job in jobs.list("alice", offset=1, limit=2)] == [ids[3], ids[2]]
        assert jobs.list("alice", offset=5, limit=2) == []
        assert (jobs.count("alice"), jobs.count("bob"), jobs.count("carol")) == (5, 1, 0)


def test_count_with_status_covers_all_users(store):
    add(store, "alice"), add(store, "bob"), add(store, "bob")
    claim(store)
    with store.transaction(write=False) as jobs:
        counts = [jobs.count_with_status(status) for status in (Status.QUEUED, Status.PROCESSING, Status.DONE)]
    assert counts == [2, 1, 0]


def test_finish(store):
    job = add(store, "alice")
    claim(store, now=2000.0)
    with store.transaction(write=True) as jobs:
        assert jobs.finish(job.id, status=Status.FAILED, error="boom", has_pdf=True, now=3000.0)
        assert not jobs.finish(job.id, status=Status.DONE, error=None, has_pdf=True, now=3001.0)  # only once
        stored = jobs.get("alice", job.id)
    assert (stored.status, stored.error, stored.has_pdf) == (Status.FAILED, "boom", True)
    assert (stored.started_at, stored.finished_at) == (2000.0, 3000.0)


def test_finish_after_delete_reports_false(store):
    job = add(store, "alice")
    claim(store)
    with store.transaction(write=True) as jobs:
        jobs.delete("alice", job.id)
        assert not jobs.finish(job.id, status=Status.DONE, error=None, has_pdf=True, now=1.0)


def test_delete_one_or_all_of_a_users_jobs(store):
    a1, a2, b1 = add(store, "alice"), add(store, "alice"), add(store, "bob")
    with store.transaction(write=True) as jobs:
        assert jobs.delete("bob", a1.id) == []  # not bob's
        assert [job.id for job in jobs.delete("alice", a1.id)] == [a1.id]
        assert [job.id for job in jobs.delete("alice")] == [a2.id]
        assert [job.id for job in jobs.list("bob")] == [b1.id]


def test_ids_are_never_reused(store):
    first = add(store, "alice")
    with store.transaction(write=True) as jobs:
        jobs.delete("alice")
    assert add(store, "alice").id > first.id


def test_requeue_interrupted(store):
    job = add(store, "alice")
    claim(store)
    with store.transaction(write=True) as jobs:
        assert jobs.requeue_interrupted() == 1
        assert jobs.get("alice", job.id).status is Status.QUEUED
    assert claim(store).id == job.id


def test_delete_finished_before(store):
    old, recent, pending = add(store, "alice"), add(store, "alice"), add(store, "alice")
    for job, finished in ((old, 100.0), (recent, 500.0)):
        claim(store)
        with store.transaction(write=True) as jobs:
            jobs.finish(job.id, status=Status.DONE, error=None, has_pdf=False, now=finished)
    with store.transaction(write=True) as jobs:
        assert [job.id for job in jobs.delete_finished_before(200.0)] == [old.id]
        assert {job.id for job in jobs.list("alice")} == {recent.id, pending.id}


def test_failed_transaction_is_rolled_back(store):
    with pytest.raises(RuntimeError), store.transaction(write=True) as jobs:
        jobs.insert(user="alice", filename="p", extension=".zip", main="m.tex", engine="pdflatex", now=1.0)
        raise RuntimeError("moving the file failed")
    with store.transaction(write=False) as jobs:
        assert jobs.list("alice") == []


def test_concurrent_writers_get_unique_ordered_ids(store):
    ids: list[int] = []
    lock = threading.Lock()

    def submit_many():
        for _ in range(20):
            job_id = add(store, "alice").id
            with lock:
                ids.append(job_id)

    threads = [threading.Thread(target=submit_many) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(ids)) == 160
    claimed = [claim(store).id for _ in range(160)]
    assert claimed == sorted(ids)
