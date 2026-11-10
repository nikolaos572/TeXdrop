"""Changes a browser sends from a page of another site are refused: their Origin header must be
PUBLIC_BASE_URL's origin or, without it, name the host the request was sent to (the test client
sends its requests to http://testserver, and no Origin unless a test adds one, like curl)."""

import dataclasses
import logging

import pytest
from fastapi.testclient import TestClient
from helpers import MINIMAL_TEX, TOKENS, browser_of, submit

from texdrop.app import create_app

ALICE = {"Authorization": f"Bearer {TOKENS['alice']}"}
OTHER_SITES = [
    "https://evil.example",
    "http://blog.testserver",  # a sibling subdomain: the same site for SameSite cookies
    "http://testserver.evil.example",
    "null",  # sandboxed pages, some redirects
    "testserver",  # not an origin at all
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


def login_from(client: TestClient, origin: str):
    return client.post("/api/session", json={"token": TOKENS["alice"]}, headers={"Origin": origin})


@pytest.mark.parametrize("origin", OTHER_SITES)
def test_a_login_from_another_site_is_refused(app, origin):
    response = login_from(TestClient(app), origin)
    assert (response.status_code, response.json()) == (
        403, {"detail": f"the request came from a page at {origin}, not from this server (testserver)"},
    )  # fmt: skip
    assert "set-cookie" not in response.headers
    assert response.headers["X-Content-Type-Options"] == "nosniff"  # an answer of the server like any other


@pytest.mark.parametrize(("method", "path"), CHANGES)
def test_every_change_is_checked_before_anything_else(app, method, path):
    # Before the token (here a valid one) and before the body (here none at all).
    response = TestClient(app).request(method, path, headers=ALICE | {"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_a_deletion_from_another_site_deletes_nothing(client, make_archive):
    job = submit(client, make_archive({"main.tex": MINIMAL_TEX})).json()
    assert client.delete("/api/jobs", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.delete(f"/api/jobs/{job['id']}", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/jobs").json()["total"] == 1


def test_a_logout_from_another_site_keeps_the_session(app):
    browser = browser_of(app)
    assert browser.delete("/api/session", headers={"Origin": "https://evil.example"}).status_code == 403
    assert browser.get("/api/session").json() == {"user": "alice"}


@pytest.mark.parametrize("origin", ["http://testserver", "http://TestServer", "https://testserver", "http://testserver:8000"])
def test_without_public_base_url_the_host_decides(app, origin):
    # Behind a reverse proxy, the scheme and port the server sees can be the proxy's.
    assert login_from(TestClient(app), origin).status_code == 200


@pytest.mark.parametrize("origin", ["https://texdrop.example.com", "https://TeXdrop.example.com:443"])
def test_with_public_base_url_its_origin_passes(settings, fake_compiler, origin):
    app = create_app(dataclasses.replace(settings, public_base_url="https://texdrop.example.com"), fake_compiler)
    assert login_from(TestClient(app), origin).status_code == 200


@pytest.mark.parametrize("origin", ["http://texdrop.example.com", "https://texdrop.example.com:8443", "http://testserver", "https://blog.example.com"])
def test_with_public_base_url_every_other_origin_is_refused(settings, fake_compiler, origin):
    app = create_app(dataclasses.replace(settings, public_base_url="https://texdrop.example.com"), fake_compiler)
    response = login_from(TestClient(app), origin)
    assert (response.status_code, response.json()["detail"]) == (
        403,
        f"this server's address is https://texdrop.example.com (PUBLIC_BASE_URL), but the request came from a page at {origin}: "
        "open https://texdrop.example.com, or set PUBLIC_BASE_URL to the address you use",
    )


def test_requests_without_an_origin_pass(app, client, make_archive):
    # Not from a browser, so no other site can make them; they still need a token.
    assert TestClient(app).post("/api/session", json={"token": TOKENS["alice"]}).status_code == 200
    assert submit(client, make_archive({"main.tex": MINIMAL_TEX})).status_code == 201


def test_reads_are_not_checked(client):
    assert client.get("/api/jobs", headers={"Origin": "https://evil.example"}).status_code == 200


def test_the_log_says_why(app, caplog):
    caplog.set_level(logging.INFO)
    login_from(TestClient(app), "https://evil.example")
    lines = [record.getMessage() for record in caplog.records if record.name == "texdrop.logs"]
    assert len(lines) == 1
    assert lines[0].startswith("testclient - POST /api/session 403 ")
    assert lines[0].endswith(" ms: the request came from a page at https://evil.example, not from this server (testserver)")
