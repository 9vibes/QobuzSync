from pathlib import Path
import sqlite3

from qobuz_sync.state import AppConfig, DEFAULT_DOWNLOAD_DIR, SyncState


def test_config_round_trips_to_sqlite(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")
    config = AppConfig(
        qobuz_email="me@example.com",
        qobuz_password="secret",
        qobuz_password_md5="abc123",
        qobuz_user_id="123",
        qobuz_user_auth_token="uat-123",
        download_dir=DEFAULT_DOWNLOAD_DIR,
        quality=6,
        interval_minutes=60,
        embed_art=True,
    )

    state.save_config(config)

    loaded = state.load_config()
    assert loaded == AppConfig(
        qobuz_email="me@example.com",
        qobuz_password="",
        qobuz_password_md5="",
        qobuz_user_id="123",
        qobuz_user_auth_token="uat-123",
        download_dir=DEFAULT_DOWNLOAD_DIR,
        quality=6,
        interval_minutes=60,
        embed_art=True,
    )


def test_download_dir_is_fixed_even_if_old_config_has_custom_value(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")
    state.save_config(AppConfig(download_dir="/legacy/custom/path"))

    assert state.load_config().download_dir == DEFAULT_DOWNLOAD_DIR


def test_legacy_plaintext_password_is_scrubbed_on_startup(tmp_path: Path):
    db_path = tmp_path / "state.db"
    state = SyncState(db_path)
    state.save_config(AppConfig(qobuz_email="me@example.com", qobuz_password_md5="abc123", qobuz_user_id="123", qobuz_user_auth_token="uat-123"))
    with sqlite3.connect(db_path) as con:
        con.execute("update config set value = ? where key = ?", ("legacy-plaintext-password", "qobuz_password"))

    reloaded = SyncState(db_path).load_config()

    assert reloaded.qobuz_password == ""
    assert reloaded.qobuz_password_md5 == ""
    assert reloaded.qobuz_user_auth_token == "uat-123"
    with sqlite3.connect(db_path) as con:
        stored = con.execute("select value from config where key = ?", ("qobuz_password",)).fetchone()[0]
    assert stored == ""


def test_configured_with_qobuz_auth_token_without_password():
    assert AppConfig(qobuz_user_id="123", qobuz_user_auth_token="uat-123").is_configured is True


def test_email_password_hash_does_not_configure_qobuz_login():
    assert AppConfig(qobuz_email="me@example.com", qobuz_password_md5="abc123").is_configured is False


def test_album_art_and_extras_default_on(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")

    assert AppConfig().embed_art is True
    assert state.load_config().embed_art is True


def test_clear_downloads_removes_all_download_records(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")
    state.mark_downloaded("track", "1", title="One", path="/downloads/one.flac")
    state.mark_downloaded("album", "2", title="Two", path="/downloads/two")

    state.clear_downloads()

    assert state.count_downloads() == 0
    assert state.list_downloads() == []


def test_seen_purchase_ids_are_not_planned_again(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")
    state.mark_downloaded("album", "111", title="Album One")

    plan = state.plan_new_downloads([
        {"kind": "album", "id": "111", "title": "Album One"},
        {"kind": "track", "id": "222", "title": "Song Two"},
    ])

    assert plan == [{"kind": "track", "id": "222", "title": "Song Two"}]


def test_downloaded_path_returns_recorded_path(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")
    state.mark_downloaded("track", "222", title="Song Two", path="/downloads/Song Two/Song Two.flac")

    assert state.downloaded_path("track", "222") == "/downloads/Song Two/Song Two.flac"


def test_sync_runs_are_recorded_with_counts(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")

    state.record_sync(success=True, found=3, downloaded=2, message="ok")

    latest = state.latest_sync()
    assert latest is not None
    assert latest["success"] is True
    assert latest["found"] == 3
    assert latest["downloaded"] == 2
    assert latest["message"] == "ok"


def test_track_progress_round_trips_and_clears(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")

    state.set_track_progress("track", "123", title="Song", path="/downloads/song.flac", downloaded_bytes=25, total_bytes=100, status="downloading")

    rows = state.list_track_progress()
    assert rows == [{
        "kind": "track",
        "purchase_id": "123",
        "title": "Song",
        "path": "/downloads/song.flac",
        "downloaded_bytes": 25,
        "total_bytes": 100,
        "status": "downloading",
    }]

    state.clear_track_progress()

    assert state.list_track_progress() == []
