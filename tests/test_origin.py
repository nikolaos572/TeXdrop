"""With PUBLIC_BASE_URL set, changes a browser sends from a page at any other address are refused:
their Origin header must be exactly PUBLIC_BASE_URL's. Without it, the Origin is not checked (the
server cannot tell its own address behind a proxy), and the other protections stay. The test
client sends no Origin unless a test adds one, like curl."""

import dataclasses
import logging

import pytest
from fastapi.testclient import TestClient
from helpers import MINIMAL_TEX, TOKENS, browser_of, submit

from texdrop.app import create_app

PUBLIC = "https://texdrop.example.com"
ALICE = {"Authorization": f"Bearer {TOKENS['alice']}"}
OTHER_ORIGINS = [
    "https://evil.example",
    "https://blog.example.com",  # a sibling subdomain: the same site for SameSite cookies
    "http://texdrop.example.com",  # the same host over plain HTTP
    "https://texdrop.example.com:8443",  # the same host, another port
    "https://texdrop.example.com.evil.example",
    "http://testserver",  # the address the request was sent to, but not PUBLIC_BASE_URL
    "null",  # sandboxed pages, some redirects
]
CHANGES = [
    ("POST", "/api/session"),
    ("DELETE", "/api/session"),
    ("POST", "/api/jobs"),
    ("DELETE", "/api/jobs"),
    ("DELETE", "/api/jobs/1"),
    ("POST", "/api/inspect"),
    ("POST", "/mcp"),
    ("POST", "/api/links/no-such-link"),
]


@pytest.fixture
def public(settings, fake_compiler):
    """The app, with PUBLIC_BASE_URL set."""
    return create_app(dataclasses.replace(settings, public_base_url=PUBLIC), fake_compiler)


def login_from(client: TestClient, origin: str):
    return client.post("/api/session", json={"token": TOKENS["alice"]}, headers={"Origin": origin})


@pytest.mark.parametrize("origin", OTHER_ORIGINS)
def test_a_login_from_another_address_is_refused(public, origin):
    response = login_from(TestClient(public), origin)
    assert (response.status_code, response.json()["detail"]) == (
        403,
        f"this server's address is {PUBLIC} (PUBLIC_BASE_URL), but the request came from a page at {origin}: "
        f"open {PUBLIC}, or set PUBLIC_BASE_URL to the address you use",
    )
    assert "set-cookie" not in response.headers
    assert response.headers["X-Content-Type-Options"] == "nosniff"  # an answer of the server like any other


@pytest.mark.parametrize("origin", [PUBLIC, "https://TeXdrop.example.com:443"])
def test_its_own_address_passes_however_written(public, origin):
    assert login_from(TestClient(public), origin).status_code == 200


@pytest.mark.parametrize(("method", "path"), CHANGES)
def test_every_change_is_checked_before_anything_else(public, method, path):
    # Before the token (here a valid one) and before the body (here none at all).
    response = TestClient(public).request(method, path, headers=ALICE | {"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_a_deletion_from_another_site_deletes_nothing(public, make_archive):
    client = TestClient(public, headers=ALICE)
    job = submit(client, make_archive({"main.tex": MINIMAL_TEX})).json()
    assert client.delete("/api/jobs", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.delete(f"/api/jobs/{job['id']}", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/jobs").json()["total"] == 1


def test_a_logout_from_another_site_keeps_the_session(public):
    browser = browser_of(public)
    assert browser.delete("/api/session", headers={"Origin": "https://evil.example"}).status_code == 403
    assert browser.get("/api/session").json() == {"user": "alice"}


def test_requests_without_an_origin_pass(public, make_archive):
    # Not from a browser, so no other site can make them; they still need a token.
    assert TestClient(public).post("/api/session", json={"token": TOKENS["alice"]}).status_code == 200
    assert submit(TestClient(public, headers=ALICE), make_archive({"main.tex": MINIMAL_TEX})).status_code == 201


def test_reads_are_not_checked(public):
    assert TestClient(public, headers=ALICE).get("/api/jobs", headers={"Origin": "https://evil.example"}).status_code == 200


def test_the_log_says_why(public, caplog):
    caplog.set_level(logging.INFO)
    login_from(TestClient(public), "https://evil.example")
    lines = [record.getMessage() for record in caplog.records if record.name == "texdrop.logs"]
    assert len(lines) == 1
    assert lines[0].startswith("testclient - POST /api/session 403 ")
    assert lines[0].endswith(f" ms: this server's address is {PUBLIC} (PUBLIC_BASE_URL), but the request came from a page "
                             f"at https://evil.example: open {PUBLIC}, or set PUBLIC_BASE_URL to the address you use")  # fmt: skip


def test_without_public_base_url_the_origin_is_not_checked(app):
    # Behind a reverse proxy, the server cannot tell which address is its own...
    assert login_from(TestClient(app), "https://evil.example").status_code == 200
    # ...and the cookie still needs the header that other sites cannot send.
    refused = browser_of(app).delete("/api/jobs", headers={"Origin": "https://evil.example"})
    assert (refused.status_code, refused.json()) == (403, {"detail": "cookie-authenticated requests need a X-Requested-With header"})
