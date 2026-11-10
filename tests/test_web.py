import re

from fastapi.testclient import TestClient
from helpers import TOKENS, login, write_json

from texdrop.app import create_app


def test_first_visit_shows_the_login_page(app):
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert 'id="login-form"' in response.text
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"


def test_login_sets_a_30_day_http_only_cookie(app):
    browser = TestClient(app)
    response = browser.post("/api/session", json={"token": TOKENS["bob"]})
    assert response.json() == {"user": "bob"}
    cookie = response.headers["set-cookie"]
    assert re.match(r"texdrop_session=[A-Za-z0-9_-]{43};", cookie)  # a session of 256 random bits, not the token
    for attribute in ("HttpOnly", "Max-Age=2592000", "Path=/", "SameSite=strict"):
        assert attribute in cookie
    assert "Secure" not in cookie  # plain HTTP; set when served over HTTPS

    page = browser.get("/")
    assert 'id="dropzone"' in page.text
    assert '<a href="/" class="pill brand">' in page.text  # the logo leads home
    assert browser.get("/api/session").json() == {"user": "bob"}


def test_info_page_needs_a_login(app):
    browser = TestClient(app)
    assert 'id="login-form"' in browser.get("/info").text
    browser.post("/api/session", json={"token": TOKENS["alice"]})
    page = browser.get("/info")
    assert 'id="storage"' in page.text
    assert 'id="mcp"' in page.text  # whether the MCP server is on, and its address
    assert page.headers["Cache-Control"] == "no-store"
    assert "script-src 'self'" in page.headers["Content-Security-Policy"]
    assert '<a href="/docs" class="pill"' in page.text  # the API documentation is linked from here
    assert '<a href="/info" class="pill">' in browser.get("/").text  # and the page from the main page


def test_pages_have_a_favicon(app):
    icon = TestClient(app).get("/static/favicon.svg")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")
    browser = TestClient(app)
    assert 'rel="icon" href="/static/favicon.svg"' in browser.get("/").text  # the login page
    browser.post("/api/session", json={"token": TOKENS["alice"]})
    for path in ("/", "/info"):
        assert 'rel="icon" href="/static/favicon.svg"' in browser.get(path).text


def test_pages_choose_their_theme_before_painting(app):
    script = '<script src="/static/theme.js"></script>'  # in <head> and not deferred, so there is no flash
    browser = TestClient(app)
    assert script in browser.get("/").text  # the login page
    assert browser.get("/static/theme.js").status_code == 200
    browser.post("/api/session", json={"token": TOKENS["alice"]})
    for path in ("/", "/info"):
        page = browser.get(path).text
        assert page.index(script) < page.index("</head>")
        assert all(f'data-theme-choice="{choice}"' in page for choice in ("light", "dark", "auto"))
        # The switch follows the Server Info link; the user's name and Log out stay at the far right.
        assert page.index('href="/info"') < page.index('class="theme-switch"') < page.index('class="spacer"')
        assert page.index('class="spacer"') < page.index('id="user"') < page.index('id="logout"')


def test_the_job_list_has_a_place_for_errors_over_the_page(app):
    page = TestClient(app, cookies={"texdrop_session": login(app)}).get("/").text
    assert '<pre id="error-tooltip" class="error-tooltip" role="tooltip" hidden></pre>' in page


def test_cookie_is_secure_over_https(app):
    response = TestClient(app, base_url="https://testserver").post("/api/session", json={"token": TOKENS["bob"]})
    assert "Secure" in response.headers["set-cookie"]


def test_wrong_token_is_rejected(app):
    browser = TestClient(app)
    response = browser.post("/api/session", json={"token": "not-a-valid-token-at-all"})
    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert 'id="login-form"' in browser.get("/").text


def test_invalid_login_bodies(app):
    browser = TestClient(app)
    assert browser.post("/api/session", json={}).status_code == 422
    assert browser.post("/api/session", json={"token": "x" * 1000}).status_code == 422


def test_stale_cookie_shows_the_login_page(app):
    browser = TestClient(app, cookies={"texdrop_session": "ended-session-0123456789"})
    assert 'id="login-form"' in browser.get("/").text
    # The cookie holds a session: a token there is no session.
    assert 'id="login-form"' in TestClient(app, cookies={"texdrop_session": TOKENS["alice"]}).get("/").text


def test_logout_clears_the_cookie(app):
    browser = TestClient(app)
    browser.post("/api/session", json={"token": TOKENS["alice"]})
    response = browser.delete("/api/session")
    assert response.status_code == 204
    assert 'texdrop_session=""' in response.headers["set-cookie"]
    assert 'id="login-form"' in browser.get("/").text
    assert browser.get("/api/session").status_code == 401


def test_logout_ends_the_session_also_for_a_copy_of_the_cookie(app, client):
    browser = TestClient(app)
    copied = {"Cookie": f"texdrop_session={login(app)}"}
    other = {"Cookie": f"texdrop_session={login(app)}"}  # the same user, in another browser
    assert 'id="dropzone"' in browser.get("/", headers=copied).text
    assert browser.delete("/api/session", headers=copied).status_code == 204
    assert 'id="login-form"' in browser.get("/", headers=copied).text
    response = browser.get("/api/jobs", headers=copied)
    assert (response.status_code, response.json()) == (401, {"detail": "the session ended or expired: log in again"})
    assert 'id="dropzone"' in browser.get("/", headers=other).text
    assert client.get("/api/session").json() == {"user": "alice"}  # the token itself still works


def test_a_new_token_ends_the_users_sessions(app, settings):
    alice, bob = login(app, "alice"), login(app, "bob")
    write_json(settings.users_file, {"users": {"alice": "alice-new-token-0123456789", "bob": TOKENS["bob"]}})
    assert TestClient(app, cookies={"texdrop_session": alice}).get("/api/session").status_code == 401
    assert TestClient(app, cookies={"texdrop_session": bob}).get("/api/session").json() == {"user": "bob"}


def test_removing_a_user_ends_their_sessions(app, settings):
    browser = TestClient(app, cookies={"texdrop_session": login(app)})
    write_json(settings.users_file, {"users": {"bob": TOKENS["bob"]}})
    assert 'id="login-form"' in browser.get("/").text
    write_json(settings.users_file, {"users": TOKENS})  # back with the same token: the session was not ended
    assert 'id="dropzone"' in browser.get("/").text


def test_sessions_survive_a_restart(app, settings, fake_compiler):
    alice = login(app)
    restarted = TestClient(create_app(settings, fake_compiler), cookies={"texdrop_session": alice})
    assert restarted.get("/api/session").json() == {"user": "alice"}


def test_static_assets_are_served(app):
    client = TestClient(app)
    for asset in ("app.js", "login.js", "style.css"):
        assert client.get(f"/static/{asset}").status_code == 200
    assert client.get("/static/../app.py").status_code == 404
