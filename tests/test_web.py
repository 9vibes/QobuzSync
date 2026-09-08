from fastapi.testclient import TestClient

from qobuz_sync.web import create_app


def test_status_page_loads_with_no_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path))
    app = create_app()
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "Qobuz Sync" in response.text
    assert 'class="umbrel-shell"' in response.text
    assert 'class="topbar umbrel-glass"' not in response.text
    assert ".topbar" not in response.text
    assert "Umbrel-native music archive" not in response.text
    assert '<span class="mini-mark">⌁</span>' not in response.text
    assert 'class="app-icon"' in response.text
    assert 'class="app-preview hero-card"' not in response.text
    assert "<small>Destination</small><strong>/Home/Downloads/QobuzSync</strong>" not in response.text
    assert "Your Qobuz library, beautifully archived." in response.text
    assert "Monitor purchased albums and tracks, sync them to your Umbrel Downloads/QobuzSync folder, and keep artwork, and metadata in one place for Navidrome or your preferred music server to digest." in response.text
    assert "one polished glass dashboard" not in response.text
    assert "overflow: hidden" in response.text
    assert "clip-path: inset(0 round 32px)" in response.text
    assert "clip-path: inset(0 round 24px)" in response.text
    assert "clip-path: inset(0 round 18px)" in response.text
    assert "clip-path: inset(0 round 16px)" in response.text
    assert "clip-path: inset(0 round 14px)" in response.text
    assert "clip-path: inset(0 round 999px)" in response.text
    assert "background-clip: padding-box" in response.text
    assert "contain: paint" in response.text
    assert 'id="sync-now-form"' in response.text
    assert "event.preventDefault()" in response.text
    assert "fetch(syncForm.action" in response.text
    assert "startLiveRefresh()" in response.text
    assert "const IDLE_REFRESH_MS = 8000" in response.text
    assert "const ACTIVE_REFRESH_MS = 2000" in response.text
    assert 'id="last-sync-time"' in response.text
    assert 'id="sync-status-badge"' in response.text
    assert 'id="sync-now-check"' in response.text
    assert "setTimeout(() => syncCheck.classList.remove('visible'), 2000)" in response.text
    assert 'id="resync-all-form"' in response.text
    assert "Re Sync Entire Library" in response.text
    assert "fetch(resyncForm.action" in response.text
    assert 'id="resync-all-check"' in response.text
    assert "grid-template-columns: 1fr;" in response.text
    assert "font-size: clamp(2rem, 5.4vw, 3.7rem)" in response.text
    assert ".status-panel { grid-column: 1 / -1; padding: .8rem;" in response.text
    assert "Not configured" in response.text
    assert "Recent downloads" in response.text
    assert "Recorded items" not in response.text
    assert "<th>Path</th>" not in response.text
    assert "downloads-grid" in response.text
    assert '<strong>Last sync:</strong> <span id="last-sync-time">Never synced</span>' in response.text
    assert 'class="sync-status-card"' in response.text
    assert 'aria-label="Master downloads progress"' in response.text
    assert 'id="master-downloads-progress-fill"' in response.text
    assert 'track-progress-list' not in response.text
    assert "<small>Phase</small><strong>Idle</strong>" in response.text
    assert "Album art &amp; extras" in response.text
    assert 'name="embed_art" type="checkbox" checked' in response.text
    assert 'id="tab-library" checked' in response.text
    assert 'id="tab-settings"' in response.text
    assert "Settings &amp; status" in response.text
    assert "Easiest login: use Qobuz in your browser" in response.text
    assert 'name="qobuz_localuser" type="password" autocomplete="off" value=""' in response.text
    assert "Open your browser’s Developer Tools / Inspect Element" in response.text
    assert "copy(localStorage.getItem('localuser'))" in response.text
    assert "Qobuz Web Player → <code>localuser</code>" in response.text
    assert "copy <code>token</code>" in response.text
    assert "<code>id</code>" in response.text
    assert "Downloads are always saved" not in response.text


def test_settings_can_be_saved_from_browser_form(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path))
    app = create_app()
    client = TestClient(app)

    response = client.post("/settings", data={
        "qobuz_email": "me@example.com",
        "qobuz_password": "secret",
        "qobuz_user_id": "123",
        "qobuz_user_auth_token": "uat-123",
        "quality": "6",
        "interval_minutes": "120",
        "embed_art": "on",
        "include_albums": "on",
        "include_tracks": "on",
    }, follow_redirects=False)

    assert response.status_code == 303
    home = client.get("/")
    assert "me@example.com" in home.text
    assert 'name="qobuz_password" type="password" autocomplete="current-password" value=""' in home.text
    assert "secret" not in home.text
    assert "Saved password hash is stored; enter a new password to replace it." in home.text
    assert "123" in home.text
    assert 'name="qobuz_user_auth_token" type="password" autocomplete="off" value=""' in home.text
    assert "uat-123" not in home.text
    assert "Saved token is stored; enter a new token to replace it." in home.text
    assert 'class="select-wrap"' in home.text
    assert "Configured" in home.text
    assert 'name="download_dir"' not in home.text
    assert "Download folder" not in home.text


def test_settings_can_parse_pasted_qobuz_localuser_session(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path))
    app = create_app()
    client = TestClient(app)

    response = client.post("/settings", data={
        "qobuz_localuser": '{"id": 123, "token": "uat-123", "email": "me@example.com"}',
        "quality": "6",
        "interval_minutes": "60",
        "include_albums": "on",
        "include_tracks": "on",
    }, follow_redirects=False)

    assert response.status_code == 303
    home = client.get("/")
    assert "me@example.com" in home.text
    assert 'value="123"' in home.text
    assert "uat-123" not in home.text
    assert "Saved token is stored; enter a new token to replace it." in home.text
    assert "Configured" in home.text


def test_downloaded_stat_shows_total_recorded_downloads_not_latest_delta(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path))
    app = create_app()
    client = TestClient(app)

    from qobuz_sync.web import data_dir
    from qobuz_sync.state import SyncState
    sync_state = SyncState(data_dir() / "qobuz-sync.db")
    sync_state.mark_downloaded("track", "1", title="One", path="/downloads/one.flac")
    sync_state.mark_downloaded("track", "2", title="Two", path="/downloads/two.flac")
    sync_state.record_sync(success=True, found=2, downloaded=0, message="Sync completed")

    home = client.get("/")

    assert "<small>Downloaded</small><strong>2</strong>" in home.text


def test_library_tab_shows_twenty_recent_downloads_in_scroll_area(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path))
    app = create_app()
    client = TestClient(app)

    from qobuz_sync.web import data_dir
    from qobuz_sync.state import SyncState
    sync_state = SyncState(data_dir() / "qobuz-sync.db")
    for index in range(22):
        sync_state.mark_downloaded("track", str(index), title=f"Track {index}", path=f"/downloads/{index}.flac")

    home = client.get("/")

    assert home.text.count('<span class="kind-badge">track</span>') == 20
    assert "Latest 20 Tracks, Scroll Down To See More" in home.text
    assert "max-height: 32.8rem" in home.text
    assert "overflow-y: auto" in home.text


def test_recent_downloads_render_artwork_metadata_and_no_path_column(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        "qobuz_sync.web.media_metadata",
        lambda path: {"title": "Glass Song", "artist": "Umbrel Artist", "album": "Dark Album", "duration": "3:42"},
    )
    music_dir = tmp_path / "music" / "song"
    music_dir.mkdir(parents=True)
    flac_path = music_dir / "song.flac"
    flac_path.write_bytes(b"fake-flac")
    cover = music_dir / "cover.jpg"
    cover.write_bytes(b"fake-jpeg")

    app = create_app()
    client = TestClient(app)
    from qobuz_sync.web import data_dir
    from qobuz_sync.state import SyncState
    sync_state = SyncState(data_dir() / "qobuz-sync.db")
    sync_state.mark_downloaded("track", "99", title="Old title", path=str(flac_path))

    home = client.get("/")

    assert 'class="download-card"' in home.text
    assert 'class="album-art" src="/art/track/99"' in home.text
    assert "Glass Song" in home.text
    assert "Umbrel Artist" in home.text
    assert "Dark Album" in home.text
    assert "3:42" in home.text
    assert "<th>Path</th>" not in home.text
    assert str(flac_path) not in home.text
    art = client.get("/art/track/99")
    assert art.status_code == 200
    assert art.content == b"fake-jpeg"


def test_recent_downloads_split_artist_from_stored_title_when_metadata_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("qobuz_sync.web.media_metadata", lambda path: {})
    app = create_app()
    client = TestClient(app)
    from qobuz_sync.web import data_dir
    from qobuz_sync.state import SyncState
    sync_state = SyncState(data_dir() / "qobuz-sync.db")
    sync_state.mark_downloaded("track", "103", title="Young the Giant - Mind Over Matter", path=str(tmp_path / "missing.flac"))

    home = client.get("/")

    assert "<h3>Mind Over Matter</h3>" in home.text
    assert "<p>Young the Giant</p>" in home.text
    assert "Unknown artist" not in home.text


def test_recent_downloads_fill_missing_length_from_qobuz_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("qobuz_sync.web.media_metadata", lambda path: {})

    class FakeQobuzClient:
        def get_track(self, track_id):
            return {
                "title": "Ocean Drive",
                "duration": 206,
                "performer": {"name": "Duke Dumont"},
                "album": {"title": "Ocean Drive", "artist": {"name": "Duke Dumont"}},
            }

    monkeypatch.setattr("qobuz_sync.web.logged_in_qobuz_client", lambda state: FakeQobuzClient())
    app = create_app()
    client = TestClient(app)
    from qobuz_sync.web import data_dir
    from qobuz_sync.state import SyncState
    sync_state = SyncState(data_dir() / "qobuz-sync.db")
    sync_state.mark_downloaded("track", "27632377", title="Duke Dumont - Ocean Drive", path=str(tmp_path / "missing.flac"))

    home = client.get("/")

    assert "<h3>Ocean Drive</h3>" in home.text
    assert "<p>Duke Dumont</p>" in home.text
    assert "3:26" in home.text
    assert "<span>—</span>" not in home.text


def test_art_route_falls_back_to_embedded_flac_art(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("qobuz_sync.web.embedded_art_for_path", lambda path: (b"embedded-cover", "image/png"))
    music_dir = tmp_path / "music" / "song"
    music_dir.mkdir(parents=True)
    flac_path = music_dir / "song.flac"
    flac_path.write_bytes(b"fake-flac")

    app = create_app()
    client = TestClient(app)
    from qobuz_sync.web import data_dir
    from qobuz_sync.state import SyncState
    sync_state = SyncState(data_dir() / "qobuz-sync.db")
    sync_state.mark_downloaded("track", "100", title="Embedded", path=str(flac_path))

    art = client.get("/art/track/100")

    assert art.status_code == 200
    assert art.headers["content-type"] == "image/png"
    assert art.content == b"embedded-cover"


def test_art_route_recovers_missing_cover_from_qobuz(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("qobuz_sync.web.embedded_art_for_path", lambda path: None)
    music_dir = tmp_path / "music" / "song"
    music_dir.mkdir(parents=True)
    flac_path = music_dir / "song.flac"
    flac_path.write_bytes(b"fake-flac")

    def fake_recover(state, kind, purchase_id, path):
        recovered = path.parent / "cover.jpg"
        recovered.write_bytes(b"qobuz-cover")
        return recovered

    monkeypatch.setattr("qobuz_sync.web.recover_cover_from_qobuz", fake_recover)
    app = create_app()
    client = TestClient(app)
    from qobuz_sync.web import data_dir
    from qobuz_sync.state import SyncState
    sync_state = SyncState(data_dir() / "qobuz-sync.db")
    sync_state.mark_downloaded("track", "101", title="Recovered", path=str(flac_path))

    art = client.get("/art/track/101")

    assert art.status_code == 200
    assert art.content == b"qobuz-cover"


def test_art_route_uses_svg_fallback_when_cover_cannot_be_recovered(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("qobuz_sync.web.embedded_art_for_path", lambda path: None)
    monkeypatch.setattr("qobuz_sync.web.recover_cover_from_qobuz", lambda state, kind, purchase_id, path: None)
    music_dir = tmp_path / "music" / "song"
    music_dir.mkdir(parents=True)
    flac_path = music_dir / "song.flac"
    flac_path.write_bytes(b"fake-flac")

    app = create_app()
    client = TestClient(app)
    from qobuz_sync.web import data_dir
    from qobuz_sync.state import SyncState
    sync_state = SyncState(data_dir() / "qobuz-sync.db")
    sync_state.mark_downloaded("track", "102", title="Fallback Song", path=str(flac_path))

    art = client.get("/art/track/102")

    assert art.status_code == 200
    assert art.headers["content-type"] == "image/svg+xml"
    assert b"Fallback Song" in art.content


def test_successful_sync_status_displays_all_good_under_last_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path))
    app = create_app()
    client = TestClient(app)

    from qobuz_sync.web import data_dir
    from qobuz_sync.state import SyncState
    sync_state = SyncState(data_dir() / "qobuz-sync.db")
    sync_state.record_sync(success=True, found=1, downloaded=1, message="Sync completed")

    home = client.get("/")

    assert "<strong>Last sync:</strong>" in home.text
    assert "All Good!" in home.text
    assert "Successful · Sync completed" not in home.text
    assert 'class="sync-badge success"' in home.text
    assert 'id="master-downloads-progress-fill"' in home.text

    widget = client.get("/widget").json()
    assert widget["text"] == "All Good!"
    assert widget["items"][1]["title"] == "Downloaded"

    progress = client.get("/api/progress").json()
    assert progress["downloaded_total"] == 0


def test_sync_status_card_integrates_progress_details(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path))
    app = create_app()
    client = TestClient(app)

    from qobuz_sync.web import data_dir
    from qobuz_sync.state import SyncState
    sync_state = SyncState(data_dir() / "qobuz-sync.db")
    sync_state.record_sync(success=True, found=12, downloaded=3, message="Sync completed")
    sync_state.set_progress(phase="album", message="Album track 2/10: Song", current=2, total=10)

    home = client.get("/")

    assert "Album track 2/10: Song" in home.text
    assert 'aria-label="Master downloads progress"' in home.text
    assert 'style="width: 20%"' in home.text
    assert "<small>Phase</small><strong>Album</strong>" in home.text
    assert "<small>Progress</small><strong>Album 2/10</strong>" in home.text


def test_track_progress_renders_inside_corresponding_library_track_card(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path / "data"))
    app = create_app()
    client = TestClient(app)

    from qobuz_sync.web import data_dir
    from qobuz_sync.state import SyncState
    sync_state = SyncState(data_dir() / "qobuz-sync.db")
    sync_state.mark_downloaded("track", "123", title="Moving Song", path="/downloads/moving.flac")
    sync_state.set_track_progress("track", "123", title="Moving Song", path="/downloads/moving.flac", downloaded_bytes=25, total_bytes=100, status="downloading")

    home = client.get("/")

    library_panel = home.text.split('<section id="library-panel"', 1)[1].split('<div id="settings-panel"', 1)[0]
    settings_panel = home.text.split('<div id="settings-panel"', 1)[1]
    assert "Moving Song" in library_panel
    assert 'class="track-card-progress"' in library_panel
    assert 'style="width: 25%"' in library_panel
    assert 'track-progress-list' not in settings_panel


def test_active_track_progress_without_download_record_renders_as_track_card(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path / "data"))
    app = create_app()
    client = TestClient(app)

    from qobuz_sync.web import data_dir
    from qobuz_sync.state import SyncState
    sync_state = SyncState(data_dir() / "qobuz-sync.db")
    sync_state.set_track_progress("track", "999", title="Active Song", path="/downloads/active.flac", downloaded_bytes=50, total_bytes=100, status="downloading")

    home = client.get("/")

    library_panel = home.text.split('<section id="library-panel"', 1)[1].split('<div id="settings-panel"', 1)[0]
    assert "Active Song" in library_panel
    assert "Writing to disk" in library_panel
    assert 'src="/progress-art/999"' in library_panel
    assert 'style="width: 50%"' in library_panel


def test_progress_only_cards_render_before_older_recent_downloads_with_art_route(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path / "data"))
    app = create_app()
    client = TestClient(app)

    from qobuz_sync.web import data_dir
    from qobuz_sync.state import SyncState
    sync_state = SyncState(data_dir() / "qobuz-sync.db")
    sync_state.mark_downloaded("track", "old", title="Older Song", path="/downloads/old.flac")
    sync_state.set_track_progress("track", "active", title="Active Song", path="/downloads/artist/album/active.flac", downloaded_bytes=50, total_bytes=100, status="downloading")

    payload = client.get("/api/progress").json()
    html = payload["downloads_html"]

    assert html.index("Active Song") < html.index("Older Song")
    assert 'src="/progress-art/active"' in html
    assert 'src=""' not in html


def test_completed_track_progress_does_not_render_writing_to_disk_cards(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path / "data"))
    app = create_app()
    client = TestClient(app)

    from qobuz_sync.web import data_dir
    from qobuz_sync.state import SyncState
    sync_state = SyncState(data_dir() / "qobuz-sync.db")
    sync_state.mark_downloaded("album", "album-1", title="Album One", path="/downloads/artist/album")
    sync_state.set_track_progress("track", "track-1", title="Finished Track", path="/downloads/artist/album/01.flac", downloaded_bytes=100, total_bytes=100, status="downloaded")
    sync_state.set_progress(phase="complete", message="All Good!", current=1, total=1)

    payload = client.get("/api/progress").json()

    assert payload["track_progress"] == []
    assert "Finished Track" not in payload["downloads_html"]
    assert "Writing to disk" not in payload["downloads_html"]
    assert "track-card-progress" not in payload["downloads_html"]


def test_progress_art_route_uses_cover_next_to_active_track(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path / "data"))
    app = create_app()
    client = TestClient(app)

    active_dir = tmp_path / "downloads" / "artist" / "album"
    active_dir.mkdir(parents=True)
    active_file = active_dir / "active.flac"
    active_file.write_bytes(b"fake-flac")
    cover = active_dir / "cover.jpg"
    cover.write_bytes(b"fake-jpeg")

    from qobuz_sync.web import data_dir
    from qobuz_sync.state import SyncState
    sync_state = SyncState(data_dir() / "qobuz-sync.db")
    sync_state.set_track_progress("track", "active", title="Active Song", path=str(active_file), downloaded_bytes=50, total_bytes=100, status="downloading")

    response = client.get("/progress-art/active")

    assert response.status_code == 200
    assert response.content == b"fake-jpeg"


def test_settings_preserve_saved_login_fields_when_blank(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path))
    app = create_app()
    client = TestClient(app)

    first = client.post("/settings", data={
        "qobuz_email": "me@example.com",
        "qobuz_password": "secret",
        "qobuz_user_id": "123",
        "qobuz_user_auth_token": "uat-123",
        "quality": "6",
        "interval_minutes": "60",
        "include_albums": "on",
        "include_tracks": "on",
    }, follow_redirects=False)
    second = client.post("/settings", data={
        "quality": "6",
        "interval_minutes": "60",
        "include_albums": "on",
        "include_tracks": "on",
    }, follow_redirects=False)

    assert first.status_code == 303
    assert second.status_code == 303
    home = client.get("/")
    assert "me@example.com" in home.text
    assert 'value="secret"' not in home.text
    assert "Saved password hash is stored; enter a new password to replace it." in home.text
    assert 'value="123"' in home.text
    assert "Saved token is stored; enter a new token to replace it." in home.text
    assert 'value="uat-123"' not in home.text


def test_album_and_track_filters_can_be_disabled_from_browser_form(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path))
    app = create_app()
    client = TestClient(app)

    response = client.post("/settings", data={
        "qobuz_email": "me@example.com",
        "qobuz_password": "secret",
        "quality": "6",
        "interval_minutes": "120",
    }, follow_redirects=False)

    assert response.status_code == 303
    home = client.get("/")
    assert 'name="include_albums" type="checkbox"' in home.text
    assert 'name="include_tracks" type="checkbox"' in home.text
    assert "status-pill" in home.text
    assert "Umbrel · Qobuz archive" in home.text
    assert "Umbrel-native music archive" not in home.text


def test_sync_now_can_start_without_full_page_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path))
    started = []

    class FakeThread:
        def __init__(self, target, daemon=False):
            self.target = target
            self.daemon = daemon

        def start(self):
            started.append((self.target, self.daemon))

    monkeypatch.setattr("qobuz_sync.web.threading.Thread", FakeThread)
    app = create_app()
    client = TestClient(app)

    response = client.post("/sync-now", headers={"Accept": "application/json"})

    assert response.status_code == 200
    assert response.json() == {"started": True, "message": "Sync started"}
    assert len(started) == 1
    assert started[0][1] is True


def test_resync_all_starts_in_background_for_live_library_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path))
    calls = []
    started = []

    class FakeSyncService:
        def __init__(self, state):
            calls.append(state)

        def resync_entire_library(self):
            calls.append("resync")

    class FakeThread:
        def __init__(self, target, daemon=False):
            self.target = target
            self.daemon = daemon

        def start(self):
            started.append((self.target, self.daemon))

    monkeypatch.setattr("qobuz_sync.web.SyncService", FakeSyncService)
    monkeypatch.setattr("qobuz_sync.web.threading.Thread", FakeThread)
    app = create_app()
    client = TestClient(app)

    response = client.post("/resync-all", headers={"Accept": "application/json"})

    assert response.status_code == 200
    assert response.json() == {"started": True, "message": "Full library re-sync started"}
    assert len(started) == 1
    assert started[0][1] is True
    assert calls == []

    started[0][0]()

    assert calls[1] == "resync"


def test_progress_api_returns_recent_downloads_for_live_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("qobuz_sync.web.media_metadata", lambda path: {"title": "Fresh Song", "artist": "Fresh Artist", "album": "Fresh Album", "duration": "3:00"})
    app = create_app()
    client = TestClient(app)

    from qobuz_sync.web import data_dir
    from qobuz_sync.state import SyncState
    sync_state = SyncState(data_dir() / "qobuz-sync.db")
    sync_state.mark_downloaded("track", "200", title="Old title", path=str(tmp_path / "song.flac"))
    sync_state.set_progress(phase="download", message="Downloading Fresh Song", current=1, total=2)

    response = client.get("/api/progress")

    assert response.status_code == 200
    payload = response.json()
    assert payload["downloaded_total"] == 1
    assert payload["downloads"][0]["display_title"] == "Fresh Song"
    assert payload["downloads"][0]["artist"] == "Fresh Artist"
    assert "Fresh Song" in payload["downloads_html"]


def test_progress_api_returns_track_progress_for_per_track_bars(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path / "data"))
    app = create_app()
    client = TestClient(app)

    from qobuz_sync.web import data_dir
    from qobuz_sync.state import SyncState
    sync_state = SyncState(data_dir() / "qobuz-sync.db")
    sync_state.set_track_progress("track", "123", title="Song", path="/downloads/song.flac", downloaded_bytes=25, total_bytes=100, status="downloading")

    payload = client.get("/api/progress").json()

    assert payload["track_progress"][0]["title"] == "Song"
    assert payload["track_progress"][0]["percent"] == 25
    assert "Song" in payload["downloads_html"]
    assert "width: 25%" in payload["downloads_html"]


def test_resync_all_rejects_overlapping_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("QOBUZ_SYNC_DATA_DIR", str(tmp_path))
    release = []

    class FakeLock:
        def __init__(self):
            self.locked = False

        def acquire(self, blocking=True):
            if self.locked:
                return False
            self.locked = True
            return True

        def release(self):
            release.append(True)
            self.locked = False

    lock = FakeLock()

    class FakeThread:
        def __init__(self, target, daemon=False):
            self.target = target
            self.daemon = daemon

        def start(self):
            pass

    monkeypatch.setattr("qobuz_sync.web.SYNC_JOB_LOCK", lock)
    monkeypatch.setattr("qobuz_sync.web.threading.Thread", FakeThread)
    app = create_app()
    client = TestClient(app)

    first = client.post("/resync-all", headers={"Accept": "application/json"})
    second = client.post("/resync-all", headers={"Accept": "application/json"})

    assert first.status_code == 200
    assert first.json()["started"] is True
    assert second.status_code == 409
    assert second.json()["started"] is False
