import json
import re
import threading
from types import SimpleNamespace
from unittest.mock import Mock, call
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient

from qobuz_sync import web
from qobuz_sync.state import AppConfig, SyncState


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("QOBUZ_SYNC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("QOBUZ_SYNC_BACKGROUND", "0")
    monkeypatch.setattr(web, "SYNC_JOB_LOCK", threading.Lock())
    return SyncState(tmp_path / "qobuz-sync.db")


def test_startup_reconciles_interrupted_sync(state):
    state.set_progress(phase="download", message="Downloading", current=1, total=3)
    state.set_track_progress("track", "1", title="Interrupted", status="downloading")

    with TestClient(web.create_app()) as client:
        payload = client.get("/api/progress").json()

    assert payload["busy"] is False
    assert payload["progress"]["phase"] == "error"
    assert "interrupted" in payload["progress"]["message"]
    assert payload["latest_sync"]["success"] == 0
    assert state.list_track_progress() == []


def test_startup_does_not_reset_an_existing_job(state):
    state.set_progress(phase="download", message="Still running")
    with web.SYNC_JOB_LOCK:
        with TestClient(web.create_app()) as client:
            payload = client.get("/api/progress").json()

    assert payload["busy"] is True
    assert payload["progress"]["phase"] == "download"


def test_entrypoint_uses_container_port_by_default(monkeypatch):
    from qobuz_sync import __main__

    monkeypatch.delenv("WEB_HOST", raising=False)
    monkeypatch.delenv("WEB_PORT", raising=False)
    run = Mock()
    monkeypatch.setattr(__main__.uvicorn, "run", run)
    __main__.main()
    run.assert_called_once_with("qobuz_sync.web:app", host="0.0.0.0", port=23809)


@pytest.mark.parametrize("route", ["/", "/api/progress", "/art/track/1", "/progress-art/2"])
def test_configured_dashboard_is_local_only(state, tmp_path, monkeypatch, route):
    state.save_config(AppConfig(qobuz_user_id="123", qobuz_user_auth_token="saved-token"))
    music = tmp_path / "music"
    music.mkdir()
    missing = str(music / "missing.mp3")
    state.mark_downloaded("track", "1", title="Local Artist - Local Song", path=missing)
    state.set_track_progress("track", "2", title="Active Song", path=missing, status="downloading")
    forbidden = Mock(side_effect=AssertionError("Dashboard must not access Qobuz or HTTP"))
    monkeypatch.setattr(web.SyncService, "_build_client", forbidden)
    monkeypatch.setattr("qobuz_sync.qobuz_client.QobuzClient.__init__", forbidden)
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    client = TestClient(web.create_app())

    response = client.get(route)

    assert response.status_code == 200
    forbidden.assert_not_called()
    assert list(music.iterdir()) == []
    assert "saved-token" not in response.text
    assert missing not in response.text
    if route == "/":
        assert "Configured" in response.text
        assert "<h3>Local Song</h3>" in response.text
        assert "<p>Local Artist</p>" in response.text
    elif route == "/api/progress":
        payload = response.json()
        assert payload["downloads"][0]["display_title"] == "Local Song"
        assert payload["downloads"][0]["duration"] == "\u2014"
        assert "path" not in payload["downloads"][0]
        assert "path" not in payload["track_progress"][0]
    else:
        assert response.headers["content-type"] == "image/svg+xml"
        assert ("Local Artist - Local Song" if route.startswith("/art/") else "Active Song") in response.text


@pytest.mark.parametrize(("base_url", "origin", "accepted"), [
    ("http://testserver", "http://TESTSERVER:80", True),
    ("https://testserver", "https://testserver:443", True),
    ("http://testserver:80", "http://testserver/", True),
    ("https://testserver:8443", "https://testserver:8443", True),
    ("http://testserver", "https://testserver", False),
    ("https://testserver", "http://testserver:443", False),
    ("http://testserver", "http://testserver:8080", False),
    ("https://testserver:8443", "https://testserver", False),
    ("http://testserver", "http://evil.test", False),
    ("http://testserver", "null", False),
    ("http://testserver", "not an origin", False),
    ("http://testserver", "//testserver", False),
    ("http://testserver", "http://testserver:bad", False),
    ("http://testserver", "http://testserver:65536", False),
    ("http://testserver", "http://[broken", False),
    ("http://testserver", "http://user:password@testserver", False),
    ("http://testserver", "http://testserver/path", False),
    ("http://testserver", "http://testserver?query=1", False),
    ("http://testserver", "http://testserver#fragment", False),
])
def test_settings_require_normalized_same_origin(state, base_url, origin, accepted):
    client = TestClient(web.create_app(), base_url=base_url)
    before = state.load_config()

    response = client.post("/settings", data={"quality": "27"}, headers={"Origin": origin}, follow_redirects=False)

    assert response.status_code == (303 if accepted else 403)
    assert state.load_config().quality == (27 if accepted else before.quality)


def form_token(response):
    return re.search(r'name="csrf_token" value="([a-f0-9]{64})"', response.text).group(1)


@pytest.mark.parametrize(("credentials", "status"), [
    ({}, 303),
    ({"qobuz_user_id": "123", "qobuz_user_auth_token": "test-token"}, 303),
    ({"qobuz_localuser": '{"id":123,"token":"test-token"}'}, 303),
    ({"qobuz_localuser": "not-a-session"}, 422),
])
@pytest.mark.parametrize("multipart", [False, True])
def test_safari_null_origin_settings_with_browser_bound_form_token(state, credentials, status, multipart):
    client = TestClient(web.create_app(), base_url="http://192.0.2.10:28080")
    home = client.get("/")
    token = form_token(home)
    assert home.text.count(f'name="csrf_token" value="{token}"') == 3
    assert token != client.cookies[web.CSRF_COOKIE_NAME]
    assert "httponly" in home.headers["set-cookie"].lower()
    assert home.headers["cache-control"] == "no-store"
    before = state.load_config()
    data = {"csrf_token": token, "quality": "27", "include_albums": "on", **credentials}
    body = {"files": {key: (None, value) for key, value in data.items()}} if multipart else {"data": data}

    response = client.post("/settings", headers={"Origin": "null"}, follow_redirects=False, **body)

    assert response.status_code == status
    if status == 303:
        config = state.load_config()
        assert config.quality == 27
        assert config.include_albums is True
        assert config.qobuz_user_id == ("123" if credentials else "")
        assert config.qobuz_user_auth_token == ("test-token" if credentials else "")
    else:
        assert state.load_config() == before


@pytest.mark.parametrize("invalid", ["missing-token", "tampered-token", "missing-cookie", "tampered-cookie", "other-browser", "cookie-as-token"])
def test_null_origin_rejects_invalid_csrf_proof(state, invalid):
    app = web.create_app()
    client = TestClient(app)
    token = form_token(client.get("/"))
    if invalid == "missing-token":
        token = ""
    elif invalid == "tampered-token":
        token += "x"
    elif invalid == "missing-cookie":
        client.cookies.clear()
    elif invalid == "tampered-cookie":
        client.cookies.clear()
        client.cookies.set(web.CSRF_COOKIE_NAME, "attacker-chosen-cookie")
    elif invalid == "other-browser":
        token = form_token(TestClient(app).get("/"))
    else:
        token = client.cookies[web.CSRF_COOKIE_NAME]
    before = state.load_config()
    response = client.post("/settings", data={"csrf_token": token, "quality": "27"}, headers={"Origin": "null"})
    assert response.status_code == 403
    assert "Reload" in response.json()["detail"]
    assert state.load_config() == before


def test_null_origin_stale_form_requires_reload_after_restart(state):
    original = TestClient(web.create_app())
    old_token = form_token(original.get("/"))
    restarted = TestClient(web.create_app())
    restarted.cookies.update(original.cookies)
    headers = {"Origin": "null"}
    response = restarted.post("/settings", data={"csrf_token": old_token}, headers=headers)
    assert response.status_code == 403
    new_token = form_token(restarted.get("/"))
    assert new_token != old_token
    assert restarted.post("/settings", data={"csrf_token": new_token}, headers=headers, follow_redirects=False).status_code == 303


@pytest.mark.parametrize("content_type", ["multipart/form-data", "multipart/form-data; boundary=foo"])
def test_null_origin_malformed_form_is_rejected(state, content_type):
    client = TestClient(web.create_app())
    client.get("/")
    response = client.post("/settings", content=b"not multipart", headers={
        "Origin": "null", "Content-Type": content_type,
    })
    assert response.status_code == 403


@pytest.mark.parametrize("chunked", [False, True])
def test_null_origin_form_body_is_bounded_before_parsing(state, chunked):
    client = TestClient(web.create_app())
    client.get("/")
    before = state.load_config()
    body = b"qobuz_localuser=" + b"x" * (64 * 1024)
    content = (body[i:i + 8192] for i in range(0, len(body), 8192)) if chunked else body
    response = client.post("/settings", content=content, headers={
        "Origin": "null", "Content-Type": "application/x-www-form-urlencoded",
    })
    assert response.status_code == 413
    assert state.load_config() == before


@pytest.mark.parametrize("headers", [
    {"Origin": "http://evil.test"},
    {"Origin": "null", "Sec-Fetch-Site": "cross-site"},
    {"Origin": "null", "Sec-Fetch-Site": "same-site"},
])
def test_csrf_token_does_not_override_explicit_cross_site_request(state, headers):
    client = TestClient(web.create_app())
    token = form_token(client.get("/"))
    response = client.post("/settings", data={"csrf_token": token, "quality": "27"}, headers=headers)
    assert response.status_code == 403


@pytest.mark.parametrize("path", ["/sync-now", "/resync-all"])
def test_null_origin_sync_uses_csrf_header(state, monkeypatch, path):
    thread = Mock()
    monkeypatch.setattr(web.threading, "Thread", thread)
    client = TestClient(web.create_app())
    home = client.get("/")
    assert "'X-CSRF-Token': form.elements.csrf_token.value" in home.text
    response = client.post(path, headers={"Origin": "null", "X-CSRF-Token": form_token(home), "Accept": "application/json"})
    assert response.status_code == 200
    assert response.json()["started"] is True
    thread.return_value.start.assert_called_once()


def test_null_origin_login_retry_and_settings_authentication(state, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_AUTH_TOKEN", "test-access-token")
    client = TestClient(web.create_app())
    login = client.get("/login")
    token = form_token(login)
    assert login.headers["cache-control"] == "no-store"
    headers = {"Origin": "null"}
    assert client.post("/settings", data={"csrf_token": token}, headers=headers).status_code == 401
    wrong = client.post("/login", data={"csrf_token": token, "token": "wrong"}, headers=headers)
    assert wrong.status_code == 401
    assert form_token(wrong) == token
    logged_in = client.post("/login", data={"csrf_token": token, "token": "test-access-token"}, headers=headers, follow_redirects=False)
    assert logged_in.status_code == 303
    assert client.post("/settings", data={"csrf_token": token}, headers=headers, follow_redirects=False).status_code == 303


def test_settings_accept_ipv6_host(state):
    client = TestClient(web.create_app())
    response = client.post("/settings", data={"quality": "27"}, headers={
        "Host": "[::1]:28080",
        "Origin": "http://[::1]:28080",
    }, follow_redirects=False)
    assert response.status_code == 303


def test_settings_accept_proxy_forwarded_same_origin(state):
    client = TestClient(web.create_app(), base_url="http://qobuz-sync:23809")

    response = client.post(
        "/settings",
        data={
            "qobuz_localuser": '{"user_auth_token": "uat-123", "user": {"id": 123, "email": "me@example.com"}}',
            "quality": "27",
        },
        headers={
            "Origin": "https://umbrel.local",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "umbrel.local",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    config = state.load_config()
    assert config.quality == 27
    assert config.qobuz_user_id == "123"
    assert config.qobuz_user_auth_token == "uat-123"


def test_settings_reject_proxy_forwarded_cross_origin(state):
    client = TestClient(web.create_app(), base_url="http://qobuz-sync:23809")
    before = state.load_config()

    response = client.post(
        "/settings",
        data={"quality": "27"},
        headers={
            "Origin": "https://evil.test",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "umbrel.local",
        },
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert state.load_config().quality == before.quality


@pytest.mark.parametrize("credentials", [
    {},
    {"qobuz_user_id": "123", "qobuz_user_auth_token": "test-token"},
    {"qobuz_localuser": '{"id":123,"token":"test-token"}'},
])
@pytest.mark.parametrize(("fetch_site", "accepted"), [
    ("same-origin", True),
    ("same-site", False),
    ("cross-site", False),
    ("none", False),
    ("", False),
])
def test_settings_behind_proxy_that_overwrites_https_scheme(state, credentials, fetch_site, accepted):
    client = TestClient(web.create_app(), base_url="http://umbrel.example")
    before = state.load_config()
    headers = {
        "Origin": "https://umbrel.example",
        "X-Forwarded-Host": "umbrel.example",
        "X-Forwarded-Proto": "http",
    }
    if fetch_site:
        headers["Sec-Fetch-Site"] = fetch_site

    response = client.post("/settings", data={"quality": "27", **credentials}, headers=headers, follow_redirects=False)

    assert response.status_code == (303 if accepted else 403)
    if accepted:
        config = state.load_config()
        assert config.quality == 27
        assert config.qobuz_user_id == ("123" if credentials else "")
        assert config.qobuz_user_auth_token == ("test-token" if credentials else "")
    else:
        assert state.load_config() == before


@pytest.mark.parametrize("origin", ["http://umbrel.local:28080", "http://evil.test:28080"])
def test_forwarded_host_without_forwarded_proto(state, origin):
    client = TestClient(web.create_app(), base_url="http://qobuz-sync:23809")
    response = client.post("/settings", data={"quality": "27"}, headers={
        "Origin": origin,
        "X-Forwarded-Host": "umbrel.local:28080",
    }, follow_redirects=False)
    assert response.status_code == (303 if origin == "http://umbrel.local:28080" else 403)


def test_same_origin_fetch_metadata_does_not_bypass_authentication(state, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_AUTH_TOKEN", "test-access-token")
    client = TestClient(web.create_app(), base_url="http://umbrel.example")
    before = state.load_config()
    response = client.post("/settings", data={"quality": "27"}, headers={
        "Origin": "https://umbrel.example",
        "Sec-Fetch-Site": "same-origin",
    }, follow_redirects=False)
    assert response.status_code == 401
    assert state.load_config() == before


@pytest.mark.parametrize("fetch_site", ["cross-site", "same-site"])
@pytest.mark.parametrize("origin", [None, "http://testserver"])
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_cross_site_mutations_are_rejected(state, method, origin, fetch_site):
    client = TestClient(web.create_app())
    headers = {"Sec-Fetch-Site": fetch_site}
    if origin:
        headers["Origin"] = origin
    response = client.request(method, "/settings", headers=headers)
    assert response.status_code == 403


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_unicode_login_uses_hashed_session_cookie(state, monkeypatch, scheme):
    token = "access-\u00e9-\u97f3\u697d"
    monkeypatch.setenv("QOBUZ_SYNC_AUTH_TOKEN", token)
    client = TestClient(web.create_app(), base_url=f"{scheme}://testserver")
    assert client.get("/health").status_code == 200
    assert client.get("/api/progress").status_code == 401
    assert client.get("/", headers={"Accept": "text/html"}, follow_redirects=False).headers["location"] == "/login"
    assert client.post("/login", data={"token": "wrong-\u97f3"}).status_code == 401

    response = client.post("/login", data={"token": f" {token} "}, follow_redirects=False)

    assert response.status_code == 303
    digest = client.cookies[web.AUTH_COOKIE_NAME]
    assert digest == web.session_token(token)
    assert len(digest) == 64
    assert int(digest, 16) >= 0
    assert digest != token
    assert digest != web.session_token(token + "changed")
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert ("secure" in cookie) is (scheme == "https")
    assert client.get("/api/progress").status_code == 200
    assert client.post("/logout", follow_redirects=False).status_code == 303
    assert web.AUTH_COOKIE_NAME not in client.cookies
    assert client.get("/api/progress").status_code == 401


@pytest.mark.parametrize(("header", "value", "status"), [
    (b"authorization", b"Bearer access-token", 200),
    (b"x-api-key", b"access-token", 200),
    (b"authorization", b"Bearer \xe9", 401),
    (b"x-api-key", b"\xe9", 401),
    (b"cookie", b"qobuz_sync_session=access-token", 401),
    (b"cookie", b"qobuz_sync_session=wrong", 401),
])
def test_api_auth_and_cookie_do_not_accept_raw_session_secret(state, monkeypatch, header, value, status):
    monkeypatch.setenv("QOBUZ_SYNC_AUTH_TOKEN", "access-token")
    client = TestClient(web.create_app())
    assert client.get("/api/progress", headers=[(header, value)]).status_code == status


@pytest.mark.parametrize(("field", "value", "accepted"), [
    ("quality", "5", True), ("quality", "6", True),
    ("quality", "7", True), ("quality", "27", True),
    ("quality", "0", False), ("quality", "8", False),
    ("quality", "garbage", False),
    ("interval_minutes", "5", True), ("interval_minutes", "10080", True),
    ("interval_minutes", "4", False), ("interval_minutes", "10081", False),
    ("interval_minutes", "-1", False), ("interval_minutes", "5.5", False),
])
def test_settings_validate_quality_and_interval_without_partial_save(state, field, value, accepted):
    before = state.load_config()
    client = TestClient(web.create_app())

    response = client.post("/settings", data={field: value, "qobuz_user_id": "123", "qobuz_user_auth_token": "new-token"}, follow_redirects=False)

    assert response.status_code == (303 if accepted else 422)
    if accepted:
        assert getattr(state.load_config(), field) == int(value)
    else:
        assert state.load_config() == before


@pytest.mark.parametrize("email", [None, "new@example.com"])
def test_pasted_session_atomically_replaces_stale_form_credentials(state, email):
    state.save_config(AppConfig(qobuz_user_id="old", qobuz_user_auth_token="old-token", qobuz_email="old@example.com"))
    pasted = {"id": 456, "token": "new-token"}
    if email:
        pasted["email"] = email
    client = TestClient(web.create_app())

    response = client.post("/settings", data={
        "qobuz_localuser": json.dumps(pasted),
        "qobuz_user_id": "old",
        "qobuz_user_auth_token": "stale-form-token",
    }, follow_redirects=False)

    assert response.status_code == 303
    config = state.load_config()
    assert (config.qobuz_user_id, config.qobuz_user_auth_token, config.qobuz_email) == ("456", "new-token", email or "")


@pytest.mark.parametrize("paste", [
    "not json", "[]", "null", "{}", '{"id": 123}', '{"token": "new"}',
    '{"id": true, "token": "new"}', '{"id": [], "token": "new"}',
    '{"id": 123, "token": 456}', '{"id": 123, "token": " "}',
])
def test_invalid_paste_rejects_even_valid_manual_credentials_without_saving(state, paste):
    before = AppConfig(qobuz_user_id="old", qobuz_user_auth_token="old-token")
    state.save_config(before)
    client = TestClient(web.create_app())

    response = client.post("/settings", data={
        "qobuz_localuser": paste, "qobuz_user_id": "new", "qobuz_user_auth_token": "new-token", "quality": "27",
    }, follow_redirects=False)

    assert response.status_code == 422
    assert state.load_config() == before


@pytest.mark.parametrize("token", ["", "  ", "new-token"])
def test_changed_user_id_requires_new_token(state, token):
    before = AppConfig(qobuz_user_id="old", qobuz_user_auth_token="old-token", qobuz_email="old@example.com")
    state.save_config(before)
    client = TestClient(web.create_app())
    response = client.post("/settings", data={"qobuz_user_id": "new", "qobuz_user_auth_token": token}, follow_redirects=False)
    if token.strip():
        assert response.status_code == 303
        config = state.load_config()
        assert (config.qobuz_user_id, config.qobuz_user_auth_token, config.qobuz_email) == ("new", token, "")
    else:
        assert response.status_code == 422
        assert state.load_config() == before


@pytest.mark.parametrize("route", ["/sync-now", "/resync-all"])
def test_thread_start_failure_releases_lock_and_allows_retry(state, monkeypatch, route):
    thread = Mock()
    thread.start.side_effect = [RuntimeError("private thread failure"), None]
    monkeypatch.setattr(web.threading, "Thread", Mock(return_value=thread))
    client = TestClient(web.create_app())

    response = client.post(route, headers={"Accept": "application/json"})

    assert response.status_code == 503
    assert response.json()["started"] is False
    assert "private thread failure" not in response.text
    assert not web.SYNC_JOB_LOCK.locked()
    assert client.post(route, headers={"Accept": "application/json"}).json()["started"] is True
    assert web.SYNC_JOB_LOCK.locked()


@pytest.mark.parametrize("route", ["/sync-now", "/resync-all"])
@pytest.mark.parametrize("fail", [False, True])
def test_manual_jobs_share_lock_report_busy_and_release_on_completion(state, monkeypatch, route, fail):
    thread = Mock()
    monkeypatch.setattr(web.threading, "Thread", thread)
    job = Mock(side_effect=RuntimeError("unexpected failure") if fail else None)
    monkeypatch.setattr(web, "run_sync_job", job)
    client = TestClient(web.create_app())
    state.set_progress(phase="complete", message="Previous run completed")
    assert client.get("/api/progress").json()["busy"] is False
    assert client.post(route, headers={"Accept": "application/json"}).json()["started"] is True
    assert client.get("/api/progress").json()["busy"] is True
    other = "/resync-all" if route == "/sync-now" else "/sync-now"
    assert client.post(other, headers={"Accept": "application/json"}).status_code == 409
    thread.assert_called_once()
    target = thread.call_args.kwargs["target"]

    if fail:
        with pytest.raises(RuntimeError, match="unexpected failure"):
            target()
    else:
        target()

    assert job.call_args.kwargs == {"resync": route == "/resync-all"}
    assert not web.SYNC_JOB_LOCK.locked()
    state.set_progress(phase="download", message="Stale active phase")
    assert client.get("/api/progress").json()["busy"] is False


@pytest.mark.parametrize("failure_at", ["constructor", "sync_once", "resync_entire_library"])
def test_run_sync_job_sanitizes_errors_and_persists_terminal_state(state, monkeypatch, caplog, failure_at):
    error = RuntimeError("failed https://example.test/?user_auth_token=secret-token&request_sig=secret-signature")
    service = Mock()
    factory = Mock(return_value=service)
    if failure_at == "constructor":
        factory.side_effect = error
    else:
        getattr(service, failure_at).side_effect = error
    monkeypatch.setattr(web, "SyncService", factory)
    state.set_progress(phase="download", message="Downloading", current=1, total=2)
    state.set_track_progress("track", "1", status="downloading")

    web.run_sync_job(state, resync=failure_at == "resync_entire_library")

    progress = state.latest_progress()
    assert progress["phase"] == "error"
    assert progress["current"] == progress["total"] == 0
    assert "retry" in progress["message"]
    assert state.list_track_progress() == []
    assert "[REDACTED]" in caplog.text
    for secret in ("secret-token", "secret-signature"):
        assert secret not in caplog.text
        assert secret not in json.dumps(progress)


@pytest.mark.parametrize(("configured", "busy"), [(True, False), (True, True), (False, False)])
@pytest.mark.parametrize(("minutes", "seconds"), [(1, 300), (60, 3600), (20000, 604800)])
def test_background_sync_uses_shared_lock_and_bounded_interval(state, monkeypatch, configured, busy, minutes, seconds):
    state.save_config(AppConfig(qobuz_user_id="123" if configured else "", qobuz_user_auth_token="token", interval_minutes=minutes))
    stop = Mock()
    stop.is_set.side_effect = [False, True]

    def run_job(actual_state):
        assert actual_state is state
        assert web.SYNC_JOB_LOCK.locked()

    job = Mock(side_effect=run_job)
    monkeypatch.setattr(web, "run_sync_job", job)
    if busy:
        web.SYNC_JOB_LOCK.acquire()

    web.run_background_sync(state, stop)

    assert job.call_count == int(configured and not busy)
    assert web.SYNC_JOB_LOCK.locked() is busy
    assert stop.wait.call_args_list == [call(10), call(seconds)]


@pytest.mark.parametrize("failure_at", ["config", "job"])
def test_background_loop_survives_errors_and_retries(state, monkeypatch, caplog, failure_at):
    config = AppConfig(qobuz_user_id="123", qobuz_user_auth_token="token", interval_minutes=1)
    error = RuntimeError("failed ?user_auth_token=secret-token")
    monkeypatch.setattr(state, "load_config", Mock(side_effect=[error, config] if failure_at == "config" else [config, config]))
    job = Mock(side_effect=[error, None] if failure_at == "job" else [None])
    monkeypatch.setattr(web, "run_sync_job", job)
    stop = Mock()
    stop.is_set.side_effect = [False, False, True]

    web.run_background_sync(state, stop)

    assert job.call_count == (2 if failure_at == "job" else 1)
    assert not web.SYNC_JOB_LOCK.locked()
    assert stop.wait.call_args_list == [call(10), call(300), call(300)]
    assert "[REDACTED]" in caplog.text
    assert "secret-token" not in caplog.text


@pytest.mark.parametrize("suffix", [".flac", ".mp3"])
def test_local_metadata_cache_reuses_stat_key_and_returns_independent_dicts(tmp_path, monkeypatch, suffix):
    audio = tmp_path / f"song{suffix}"
    audio.write_bytes(b"audio")
    parser = Mock(return_value=SimpleNamespace(tags={"title": ["Song"], "artist": ["Artist"], "album": ["Album"]}, info=SimpleNamespace(length=206)))
    monkeypatch.setattr("mutagen.File", parser)

    metadata = web.media_metadata(tmp_path)
    assert metadata == {"title": "Song", "artist": "Artist", "album": "Album", "duration": "3:26"}
    metadata["title"] = "Caller mutation"
    assert web.media_metadata(audio)["title"] == "Song"
    parser.assert_called_once_with(str(audio), easy=True)

    audio.write_bytes(b"updated audio with a different size")
    assert web.media_metadata(audio)["title"] == "Song"
    assert parser.call_count == 2


@pytest.mark.parametrize("album", ["Tagged Album", ""])
def test_album_card_uses_album_title_not_first_track_title_or_duration(monkeypatch, album):
    monkeypatch.setattr(web, "media_metadata", lambda path: {"title": "First Track", "artist": "Artist", "album": album, "duration": "3:26"})
    row = web.enrich_download({"kind": "album", "purchase_id": "1", "title": "Artist - Stored Album", "downloaded_at": "today"})
    assert row["display_title"] == row["album"] == (album or "Stored Album")
    assert row["duration"] == "\u2014"
    html = web.render_download_rows([row])
    assert f"<h3>{album or 'Stored Album'}</h3>" in html
    assert "First Track" not in html
    assert "3:26" not in html


@pytest.mark.parametrize(("filename", "mime"), [
    ("cover.jpg", "image/jpeg"), ("cover.jpeg", "image/jpeg"),
    ("cover.png", "image/png"), ("cover.webp", "image/webp"),
    ("FRONT.PNG", "image/png"), ("folder.jpg", "image/jpeg"),
    ("cover.svg", None), ("cover.html", None), ("cover.gif", None),
])
@pytest.mark.parametrize("route", ["/art/track/1", "/progress-art/1"])
def test_art_file_allowlist_and_missing_audio_parent_cover(state, tmp_path, route, filename, mime):
    music = tmp_path / "music"
    music.mkdir()
    cover = music / filename
    cover.write_bytes(b"local-cover")
    missing = str(music / "missing.mp3")
    state.mark_downloaded("track", "1", title="Local Song", path=missing)
    state.set_track_progress("track", "1", title="Local Song", path=missing)
    client = TestClient(web.create_app())

    response = client.get(route)

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert response.headers["content-type"] == (mime or "image/svg+xml")
    if mime:
        assert response.content == b"local-cover"
    else:
        assert "Local Song" in response.text
        assert response.content != b"local-cover"


@pytest.mark.parametrize("suffix", [".flac", ".mp3"])
@pytest.mark.parametrize("mime", ["image/jpeg", "image/png", "image/webp", "image/svg+xml", "text/html", "image/gif", ""])
def test_embedded_art_mime_allowlist(tmp_path, monkeypatch, suffix, mime):
    path = tmp_path / f"song{suffix}"
    path.write_bytes(b"audio")
    picture = SimpleNamespace(data=b"embedded-cover", mime=mime)
    tags = Mock()
    tags.getall.return_value = [picture]
    parser = Mock(return_value=SimpleNamespace(pictures=[picture] if suffix == ".flac" else [], tags=tags))
    monkeypatch.setattr("mutagen.File", parser)

    result = web.embedded_art_for_path(path)

    assert result == ((b"embedded-cover", mime) if mime in {"image/jpeg", "image/png", "image/webp"} else None)
    if suffix == ".mp3":
        tags.getall.assert_called_once_with("APIC")


def test_fallback_svg_escapes_untrusted_titles(state, tmp_path):
    title = '<script>alert("x")</script> & Song'
    state.mark_downloaded("track", "1", title=title, path=str(tmp_path / "missing.flac"))
    response = TestClient(web.create_app()).get("/art/track/1")
    assert response.status_code == 200
    root = ElementTree.fromstring(response.content)
    assert root.attrib["aria-label"] == f"Artwork placeholder for {title}"
    assert root.find(".//{http://www.w3.org/2000/svg}script") is None
    assert "<script>" not in response.text
