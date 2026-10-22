import sqlite3
import threading
from contextlib import closing

import pytest

from texdrop.links import UPLOAD, LinkRefused, LinkService
from texdrop.store import JobStore

LIFETIME = 3600


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def links(tmp_path, clock):
    JobStore(tmp_path / "jobs.db")  # the database the links share with the jobs
    return LinkService(tmp_path / "jobs.db", LIFETIME, clock)


def test_a_link_works_for_its_client_and_transfer(links):
    secret, expires_at = links.create("alice", "10.0.0.5", "pdf", 42)
    assert expires_at == 1000.0 + LIFETIME
    link = links.check(secret, "10.0.0.5", upload=False)
    assert (link.user, link.file, link.job_id, link.client) == ("alice", "pdf", 42, "10.0.0.5")


def test_only_from_the_address_that_asked_for_it(links):
    secret, _ = links.create("alice", "10.0.0.5", UPLOAD)
    with pytest.raises(LinkRefused, match="^link for 10.0.0.5 used from 10.0.0.6$"):
        links.check(secret, "10.0.0.6", upload=True)


def test_only_for_its_kind_of_transfer(links):
    upload, _ = links.create("alice", "10.0.0.5", UPLOAD)
    download, _ = links.create("alice", "10.0.0.5", "log", 1)
    with pytest.raises(LinkRefused, match="^upload link used for a download$"):
        links.check(upload, "10.0.0.5", upload=False)
    with pytest.raises(LinkRefused, match="^log link used for an upload$"):
        links.check(download, "10.0.0.5", upload=True)


def test_unknown_secrets_are_refused(links):
    secret, _ = links.create("alice", "10.0.0.5", UPLOAD)
    for wrong in ("", "x", secret[:-1], secret + "x", "' OR 1=1 --"):
        with pytest.raises(LinkRefused, match="^unknown link$"):
            links.check(wrong, "10.0.0.5", upload=True)


def test_links_expire(links, clock):
    secret, expires_at = links.create("alice", "10.0.0.5", UPLOAD)
    clock.now = expires_at - 1
    links.check(secret, "10.0.0.5", upload=True)
    clock.now = expires_at
    with pytest.raises(LinkRefused, match="^expired link$"):
        links.check(secret, "10.0.0.5", upload=True)


def test_a_transfer_checked_in_time_may_end_after_the_expiry(links, clock):
    secret, expires_at = links.create("alice", "10.0.0.5", UPLOAD)
    link = links.check(secret, "10.0.0.5", upload=True)  # when the upload starts
    clock.now = expires_at + 60  # a slow upload
    with links.using(link):
        pass
    with pytest.raises(LinkRefused, match="^unknown link$"):
        links.check(secret, "10.0.0.5", upload=True)


def test_a_completed_transfer_uses_the_link_up(links):
    secret, _ = links.create("alice", "10.0.0.5", UPLOAD)
    with links.using(links.check(secret, "10.0.0.5", upload=True)):
        pass
    with pytest.raises(LinkRefused, match="^unknown link$"):
        links.check(secret, "10.0.0.5", upload=True)


def test_a_failed_transfer_leaves_the_link_valid(links):
    secret, _ = links.create("alice", "10.0.0.5", UPLOAD)
    link = links.check(secret, "10.0.0.5", upload=True)
    with pytest.raises(ValueError), links.using(link):
        raise ValueError("bad archive")
    assert links.check(secret, "10.0.0.5", upload=True) == link


def test_two_transfers_at_once_cannot_both_use_a_link(links):
    secret, _ = links.create("alice", "10.0.0.5", UPLOAD)
    first = links.check(secret, "10.0.0.5", upload=True)
    second = links.check(secret, "10.0.0.5", upload=True)  # checked while the first one is uploading
    with links.using(first), pytest.raises(LinkRefused, match="^link used up meanwhile$"), links.using(second):
        pass
    with pytest.raises(LinkRefused, match="^link used up meanwhile$"), links.using(second):  # and after it
        pass


def test_concurrent_transfers_one_succeeds(links):
    secret, _ = links.create("alice", "10.0.0.5", UPLOAD)
    link = links.check(secret, "10.0.0.5", upload=True)
    barrier = threading.Barrier(8)
    succeeded = []

    def transfer():
        barrier.wait()
        try:
            with links.using(link):
                succeeded.append(True)
        except LinkRefused:
            pass

    threads = [threading.Thread(target=transfer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert succeeded == [True]


def test_a_download_is_used_up_once_complete(links):
    secret, _ = links.create("alice", "10.0.0.5", "pdf", 7)
    link = links.check(secret, "10.0.0.5", upload=False)
    links.check(secret, "10.0.0.5", upload=False)  # checking does not use it
    links.use_up(link)
    with pytest.raises(LinkRefused, match="^unknown link$"):
        links.check(secret, "10.0.0.5", upload=False)


def test_secrets_are_random_and_not_stored(links, tmp_path):
    secrets = {links.create("alice", "10.0.0.5", UPLOAD)[0] for _ in range(100)}
    assert len(secrets) == 100
    assert all(len(secret) >= 43 for secret in secrets)  # 256 bits in base64
    with closing(sqlite3.connect(tmp_path / "jobs.db")) as conn:
        stored = "".join(str(value) for row in conn.execute("SELECT * FROM links") for value in row)
    assert not any(secret in stored for secret in secrets)


def test_expired_links_are_deleted_when_links_are_made(links, clock, tmp_path):
    links.create("alice", "10.0.0.5", UPLOAD)
    clock.now += LIFETIME
    links.create("alice", "10.0.0.5", UPLOAD)
    with closing(sqlite3.connect(tmp_path / "jobs.db")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 1


def test_links_survive_a_restart(links, clock, tmp_path):
    secret, _ = links.create("alice", "10.0.0.5", UPLOAD)
    again = LinkService(tmp_path / "jobs.db", LIFETIME, clock)
    assert again.check(secret, "10.0.0.5", upload=True).user == "alice"
