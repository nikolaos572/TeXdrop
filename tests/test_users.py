import json
import os

import pytest

from texdrop.users import ConfigError, UserRegistry, parse_users

GOOD_TOKEN = "0123456789abcdef"


def test_parse_valid_users():
    assert parse_users({"users": {"alice": GOOD_TOKEN, "bob_2": "x" * 64}}) == {GOOD_TOKEN: "alice", "x" * 64: "bob_2"}


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"users": []},
        {"users": {"Alice": GOOD_TOKEN}},  # upper case
        {"users": {"../etc": GOOD_TOKEN}},
        {"users": {"": GOOD_TOKEN}},
        {"users": {"a" * 33: GOOD_TOKEN}},
        {"users": {"alice": "short"}},
        {"users": {"alice": 12345678901234567890}},
        {"users": {"alice": GOOD_TOKEN + " with space"}},
        {"users": {"alice": GOOD_TOKEN + ";"}},
        {"users": {"alice": GOOD_TOKEN, "bob": GOOD_TOKEN}},  # shared token
    ],
)
def test_invalid_documents_are_rejected(document):
    with pytest.raises(ConfigError):
        parse_users(document)


def _write(path, users):
    path.write_text(json.dumps({"users": users}))


def test_authenticate(tmp_path):
    path = tmp_path / "config.json"
    _write(path, {"alice": GOOD_TOKEN})
    registry = UserRegistry(path)
    assert registry.authenticate(GOOD_TOKEN) == "alice"
    assert registry.authenticate(GOOD_TOKEN[:-1]) is None
    assert registry.authenticate("") is None
    assert registry.authenticate("ünïcode-token-value") is None


def test_missing_file_means_no_users(tmp_path):
    assert UserRegistry(tmp_path / "missing.json").authenticate(GOOD_TOKEN) is None


def test_invalid_file_at_startup_is_an_error(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError):
        UserRegistry(path)


def test_changes_are_picked_up_without_restart(tmp_path):
    path = tmp_path / "config.json"
    _write(path, {"alice": GOOD_TOKEN})
    registry = UserRegistry(path)

    _write(path, {"bob": "b" * 20})
    os.utime(path, ns=(1, 1))  # make sure the modification is noticed even on coarse clocks
    assert registry.authenticate(GOOD_TOKEN) is None
    assert registry.authenticate("b" * 20) == "bob"

    path.unlink()
    assert registry.authenticate("b" * 20) is None


def test_broken_edit_keeps_previous_users(tmp_path):
    path = tmp_path / "config.json"
    _write(path, {"alice": GOOD_TOKEN})
    registry = UserRegistry(path)
    path.write_text('{"users": {"alice": "short"}}')
    os.utime(path, ns=(1, 1))
    assert registry.authenticate(GOOD_TOKEN) == "alice"
