"""Optional browser checks: install Playwright and Chromium separately to run.

On Alpine: PLAYWRIGHT_NODEJS_PATH=/usr/bin/node .venv/bin/python -m pytest tests/test_browser.py
All requests are intercepted; no server, credentials, or music files are used.
"""

from datetime import datetime, timezone
from pathlib import Path
import shutil
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

playwright = pytest.importorskip("playwright.sync_api", reason="Optional Playwright not installed")
expect = playwright.expect

from qobuz_sync.state import AppConfig
from qobuz_sync.web import render_download_rows, render_home


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as driver:
        executable = shutil.which("chromium") or shutil.which("chromium-browser")
        executable = executable or driver.chromium.executable_path
        if not Path(executable).is_file():
            pytest.skip("Optional Chromium not installed")
        browser = driver.chromium.launch(executable_path=executable, headless=True)
        yield browser
        browser.close()


@pytest.fixture
def dashboard(browser):
    downloads = [{
        "kind": "track", "purchase_id": "123", "display_title": "Glass Song",
        "artist": "Test Artist", "album": "Test Album", "duration": "3:42",
        "downloaded_at": "2026-09-11 12:00:00", "art_url": "/art/track/123",
    }]
    html = render_home(AppConfig(), None, downloads)
    api = SimpleNamespace(
        calls=0, status=200, hold=False, pending=[], posts=[], unexpected=[],
        downloads=downloads, art_calls=[],
        payload={
            "busy": False, "downloaded_total": 1,
            "latest_sync": {"success": True, "finished_at": "2026-09-11 12:00:00",
                            "message": "Sync completed", "found": 1, "downloaded": 1},
            "progress": {"phase": "complete", "current": 1, "total": 1},
            "downloads_html": render_download_rows(downloads),
        },
    )
    context = browser.new_context(viewport={"width": 1280, "height": 900}, service_workers="block")
    page = context.new_page()
    page.set_default_timeout(3000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def route_request(route):
        path = urlparse(route.request.url).path
        if route.request.url == "http://qobuz.test/" and route.request.method == "GET":
            route.fulfill(content_type="text/html", body=html)
        elif route.request.url == "http://qobuz.test/api/progress":
            api.calls += 1
            api.payload["progress"]["message"] = f"Poll {api.calls}"
            if api.hold:
                api.pending.append(route)
            else:
                route.fulfill(status=api.status, json=api.payload)
        elif path in {"/sync-now", "/resync-all"} and route.request.method == "POST":
            api.posts.append(route)
        elif path in {"/art/track/123", "/art/track/active", "/progress-art/active"}:
            api.art_calls.append(path)
            route.fulfill(content_type="image/svg+xml", body=(
                '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"/>'
            ))
        else:
            api.unexpected.append(route.request.url)
            route.abort()

    context.route("**/*", route_request)
    page.clock.install(time=datetime(2026, 9, 11, tzinfo=timezone.utc))
    page.clock.pause_at(datetime(2026, 9, 11, 0, 0, 1, tzinfo=timezone.utc))
    page.goto("http://qobuz.test/")
    page.evaluate("window.originalCard = document.querySelector('.download-card')")
    poll(page, api, 1)
    yield page, api
    context.close()
    assert not errors, f"Uncaught browser JavaScript errors: {errors}"
    assert not api.unexpected, f"Unexpected network requests: {api.unexpected}"


def poll(page, api, milliseconds=8000):
    expected_call = api.calls + 1
    page.clock.run_for(milliseconds)
    expect(page.locator("#sync-progress-message")).to_have_text(f"Poll {expected_call}")
    assert api.calls == expected_call


@pytest.mark.parametrize("width", [320, 375, 1280])
def test_layout_and_keyboard_sections(dashboard, width):
    page, _ = dashboard
    page.set_viewport_size({"width": width, "height": 900})
    expect(page.locator("#library-panel")).to_be_visible()
    expect(page.locator("#settings-panel")).to_be_hidden()
    # Reach the radio group with Tab, not a programmatic radio selection.
    page.locator("#resync-all-button").focus()
    page.keyboard.press("Tab")
    expect(page.locator("#tab-library")).to_be_focused()
    for key, selected, hidden in [
        ("ArrowRight", "settings", "library"),
        ("ArrowLeft", "library", "settings"),
    ]:
        page.keyboard.press(key)
        expect(page.locator(f"#tab-{selected}")).to_be_checked()
        expect(page.locator(f"#tab-{selected}")).to_be_focused()
        expect(page.locator(f"#{selected}-panel")).to_be_visible()
        expect(page.locator(f"#{hidden}-panel")).to_be_hidden()
        dimensions = page.evaluate("""() => ({
            viewport: document.documentElement.clientWidth,
            document: document.documentElement.scrollWidth,
            body: document.body.scrollWidth
        })""")
        assert dimensions["viewport"] == width
        assert max(dimensions["document"], dimensions["body"]) <= width, dimensions


def test_initial_refresh_success_and_unchanged_library(dashboard):
    page, api = dashboard
    page.locator('label[for="tab-settings"]').click()
    badge = page.locator("#sync-status-badge")
    expect(badge).to_be_visible()
    expect(badge).to_have_text("All Good!")
    expect(badge).to_have_class("sync-badge success")
    expect(page.get_by_role("progressbar", name="Master downloads progress")).to_have_attribute(
        "aria-valuenow", "100"
    )
    assert page.evaluate("window.originalCard === document.querySelector('.download-card')")
    for _ in range(2):
        poll(page, api)
        assert page.evaluate("window.originalCard === document.querySelector('.download-card')")
    expect(page.locator("#downloaded-stat strong")).to_have_text("1")


def test_downloading_cards_and_artwork_survive_progress_refreshes(dashboard):
    page, api = dashboard
    progress = {
        "kind": "track", "purchase_id": "active", "title": "Active Song",
        "status": "downloading", "downloaded_bytes": 10, "total_bytes": 100, "percent": 10,
    }
    api.payload["busy"] = True
    api.payload["downloads_html"] = render_download_rows(api.downloads, track_progress=[progress])
    poll(page, api)
    card = page.locator('[data-download-key="track:active"]')
    expect(card).to_be_visible()
    expect(card.locator("img")).to_have_js_property("complete", True)
    page.evaluate("""() => {
        window.activeCard = document.querySelector('[data-download-key="track:active"]');
        window.activeImage = window.activeCard.querySelector('img');
    }""")
    for percent in (30, 60, 100):
        progress.update(downloaded_bytes=percent, percent=percent)
        api.payload["downloads_html"] = render_download_rows(api.downloads, track_progress=[progress])
        poll(page, api, 2000)
        expect(card.get_by_role("progressbar")).to_have_attribute("aria-valuenow", str(percent))
        assert page.evaluate("window.activeCard === document.querySelector('[data-download-key=\"track:active\"]')")
        assert page.evaluate("window.activeImage === window.activeCard.querySelector('img')")
        assert page.evaluate("window.originalCard === document.querySelector('[data-download-key=\"track:123\"]')")
    assert api.art_calls.count("/progress-art/active") == 1

    progress["status"] = "downloaded"
    api.payload["downloads_html"] = render_download_rows(api.downloads, track_progress=[progress])
    poll(page, api, 2000)
    expect(card).to_contain_text("Finishing purchase")
    expect(card.get_by_role("progressbar")).to_have_count(0)
    assert page.evaluate("window.activeCard === document.querySelector('[data-download-key=\"track:active\"]')")

    completed = {**api.downloads[0], "purchase_id": "active", "display_title": "Active Song", "art_url": "/art/track/active"}
    api.payload["downloads_html"] = render_download_rows([completed, *api.downloads])
    poll(page, api, 2000)
    expect(card).to_have_count(1)
    expect(card).not_to_contain_text("Finishing purchase")
    assert page.evaluate("window.activeCard === document.querySelector('[data-download-key=\"track:active\"]')")
    assert page.evaluate("window.activeImage === window.activeCard.querySelector('img')")


def test_progress_refresh_preserves_library_scroll_position(dashboard):
    page, api = dashboard
    downloads = [{**api.downloads[0], "purchase_id": str(index)} for index in range(20)]
    progress = {"kind": "track", "purchase_id": "active", "title": "Active Song", "status": "downloading", "percent": 10}
    api.payload["busy"] = True
    api.payload["downloads_html"] = render_download_rows(downloads, track_progress=[progress])
    poll(page, api)
    page.evaluate("document.getElementById('downloads-grid').scrollTop = 200")
    before = page.locator("#downloads-grid").evaluate("grid => grid.scrollTop")
    assert before > 0
    progress["percent"] = 40
    api.payload["downloads_html"] = render_download_rows(downloads, track_progress=[progress])
    poll(page, api, 2000)
    assert page.locator("#downloads-grid").evaluate("grid => grid.scrollTop") == before


def test_artwork_version_change_updates_existing_image_node(dashboard):
    page, api = dashboard
    page.evaluate("window.originalImage = window.originalCard.querySelector('img')")
    completed = {**api.downloads[0], "art_url": "/art/track/123?v=new-cover"}
    api.payload["downloads_html"] = render_download_rows([completed])
    with page.expect_request("**/art/track/123?v=new-cover"):
        poll(page, api)
    expect(page.locator(".album-art")).to_have_attribute("src", completed["art_url"])
    assert page.evaluate("window.originalImage === document.querySelector('.album-art')")
    assert page.evaluate("window.originalCard === document.querySelector('.download-card')")
    count = len(api.art_calls)
    poll(page, api)
    assert len(api.art_calls) == count


def test_slow_progress_has_at_most_one_request_in_flight(dashboard):
    page, api = dashboard
    active, peaks = set(), []

    def started(request):
        if urlparse(request.url).path == "/api/progress":
            active.add(request)
            peaks.append(len(active))

    page.on("request", started)
    page.on("requestfinished", lambda request: active.discard(request))
    page.on("requestfailed", lambda request: active.discard(request))
    api.payload["busy"] = True
    poll(page, api)
    api.hold = True
    with page.expect_request("**/api/progress"):
        page.clock.run_for(2000)
    # Cross several active intervals, but stay below the 10-second abort timeout.
    page.clock.run_for(8000)
    # Visibility changes also request an immediate refresh, even while in flight.
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.clock.run_for(1)
    assert api.calls == 3
    assert len(api.pending) == 1
    assert len(active) == 1
    api.hold = False
    api.payload["busy"] = False
    api.pending.pop().fulfill(json=api.payload)
    expect(page.locator("#sync-progress-message")).to_have_text("Poll 3")
    poll(page, api)
    assert max(peaks) == 1


def test_progress_timeout_retries(dashboard):
    page, api = dashboard
    api.hold = True
    with page.expect_request("**/api/progress"):
        page.clock.run_for(8000)
    with page.expect_event("requestfailed", predicate=lambda request: request.url.endswith("/api/progress")):
        page.clock.run_for(10000)
    expect(page.locator("#action-feedback")).to_have_text("Live updates paused. Retrying shortly.")
    expect(page.locator("#action-feedback")).to_be_visible()
    api.pending.pop().abort()
    api.hold = False
    poll(page, api)
    expect(page.locator("#action-feedback")).to_have_text("")


@pytest.mark.parametrize("status,message", [
    (200, "Sync started"),
    (409, "A sync is already running"),
])
def test_sync_feedback_and_busy_buttons(dashboard, status, message):
    page, api = dashboard
    buttons = page.locator("#sync-now-button, #resync-all-button")
    with page.expect_request("**/sync-now"):
        page.locator("#sync-now-button").click()
    for button in buttons.all():
        expect(button).to_be_disabled()
    expect(page.locator("#action-feedback")).to_have_text("Starting sync...")
    api.payload["busy"] = True
    api.posts[0].fulfill(status=status, json={"started": status == 200, "message": message})
    expect(page.locator("#action-feedback")).to_have_text(message)
    expect(page.locator("#action-feedback")).to_be_visible()
    poll(page, api, 1)
    expect(page.locator("#sync-status-badge")).to_have_text("Syncing")
    for button in buttons.all():
        expect(button).to_be_disabled()
    api.payload["busy"] = False
    poll(page, api, 2000)
    for button in buttons.all():
        expect(button).to_be_enabled()
    expect(page.locator("#sync-status-badge")).to_have_text("All Good!")
    assert len(api.posts) == 1


def test_resync_requires_confirmation(dashboard):
    page, api = dashboard

    def dismiss(dialog):
        assert dialog.type == "confirm"
        assert "Redownload all selected purchases?" in dialog.message
        assert "Existing files are kept" in dialog.message
        dialog.dismiss()

    page.once("dialog", dismiss)
    page.locator("#resync-all-button").click()
    assert api.posts == []
    expect(page.locator("#resync-all-button")).to_be_enabled()
    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_request("**/resync-all"):
        page.locator("#resync-all-button").click()
    assert len(api.posts) == 1
    api.posts[0].fulfill(json={"started": True, "message": "Full library re-sync started"})
    expect(page.locator("#action-feedback")).to_have_text("Full library re-sync started")
    expect(page.locator("#downloads-grid .download-card")).to_have_count(1)


def test_expired_session_stops_polling(dashboard):
    page, api = dashboard
    api.status = 401
    page.clock.run_for(8000)
    expect(page.locator("#action-feedback")).to_have_text(
        "Session expired. Reload the page to sign in."
    )
    expect(page.locator("#action-feedback")).to_be_visible()
    assert api.calls == 2
    page.clock.run_for(60000)
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.clock.run_for(8000)
    assert api.calls == 2
    for selector in ("#sync-now-button", "#resync-all-button"):
        expect(page.locator(selector)).to_be_disabled()


def test_hidden_document_pauses_and_resumes_polling(dashboard):
    page, api = dashboard
    # Headless pages do not become hidden reliably by opening another tab.
    page.evaluate("""() => {
        Object.defineProperty(document, 'hidden', {configurable: true, value: true});
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    page.clock.run_for(32000)
    assert api.calls == 1
    page.evaluate("""() => {
        delete document.hidden;
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    poll(page, api, 1)
