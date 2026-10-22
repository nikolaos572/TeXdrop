import pytest

from texdrop.storage import disk_usage, remove_tree


@pytest.fixture
def work(tmp_path):
    """A work directory whose owner took away its own rights to two folders, as a compilation can."""
    work = tmp_path / "work"
    (work / "open").mkdir(parents=True)
    (work / "open" / "a.bin").write_bytes(b"a" * 100_000)
    (work / "locked" / "inner").mkdir(parents=True)
    (work / "locked" / "inner" / "b.bin").write_bytes(b"b" * 1_000_000)
    (work / "locked" / "inner").chmod(0)
    (work / "locked").chmod(0o500)  # readable, but nothing in it can be deleted
    return work


def test_disk_usage_counts_files_in_folders_without_rights(work):
    assert 1_100_000 <= disk_usage(work) < 1_200_000


def test_disk_usage_does_not_follow_links(tmp_path):
    (tmp_path / "big.bin").write_bytes(b"x" * 1_000_000)
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "link").symlink_to(tmp_path / "big.bin")
    (tmp_path / "work" / "dir-link").symlink_to(tmp_path)
    assert disk_usage(tmp_path / "work") < 100_000


def test_remove_tree_removes_folders_without_rights(work):
    remove_tree(work)
    assert not work.exists()


def test_remove_tree_leaves_what_links_point_to(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    outside.chmod(0o500)
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "link").symlink_to(outside)
    remove_tree(tmp_path / "work")
    assert not (tmp_path / "work").exists()
    assert (outside / "keep.txt").read_text() == "keep"
    assert outside.stat().st_mode & 0o777 == 0o500  # its rights are untouched
    outside.chmod(0o700)


def test_remove_tree_of_a_missing_directory(tmp_path):
    remove_tree(tmp_path / "missing")
