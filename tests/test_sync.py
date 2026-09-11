from pathlib import Path
import pytest

from qobuz_sync.state import AppConfig, DEFAULT_DOWNLOAD_DIR, SyncState
from qobuz_sync.sync import SyncService, _safe_message
from qobuz_sync.qobuz_client import QobuzError


class FakePurchaseClient:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.login_calls = []
        self.download_calls = []
        self.extra_calls = []

    def login(self, *, user_id: str = "", user_auth_token: str = ""):
        self.login_calls.append((user_id, user_auth_token))

    def list_owned_items(self, *, include_albums: bool = True, include_tracks: bool = True):
        return [
            {"kind": "album", "id": "111", "title": "Album One"},
            {"kind": "track", "id": "222", "title": "Song Two"},
        ]

    def download_owned_item(self, item, download_dir, quality, *, include_extras=True, progress_callback=None, track_progress_callback=None):
        self.download_calls.append((item, download_dir, quality, include_extras))
        output = self.output_dir / f"{item['kind']}-{item['id']}"
        if item["kind"] == "album":
            output.mkdir(exist_ok=True)
            path = output / "01.flac"
        else:
            output = path = output.with_suffix(".flac")
        if progress_callback:
            progress_callback(1, 1, item["title"])
        if track_progress_callback:
            track_progress_callback(item["kind"], str(item["id"]), item["title"], str(path), 10, 10, "downloaded")
        path.write_text("fake audio", encoding="utf-8")
        return output

    def download_owned_item_extras(self, item, downloaded_path):
        self.extra_calls.append((item, downloaded_path))
        return []


def test_sync_once_downloads_only_new_purchases(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")
    state.save_config(AppConfig(qobuz_user_id="123", qobuz_user_auth_token="uat-123", download_dir=str(tmp_path / "music")))
    client = FakePurchaseClient(tmp_path)
    service = SyncService(state, client=client)

    first = service.sync_once()
    second = service.sync_once()

    assert first["success"] is True
    assert first["found"] == 2
    assert first["downloaded"] == 2
    assert second["success"] is True
    assert second["found"] == 2
    assert second["downloaded"] == 0
    assert len(client.download_calls) == 2
    assert client.login_calls[0] == ("123", "uat-123")
    assert all(call[1] == DEFAULT_DOWNLOAD_DIR for call in client.download_calls)
    assert all(call[3] is True for call in client.download_calls)
    assert len(state.list_downloads()) == 2
    assert state.count_downloads() == 2
    progress = state.latest_progress()
    assert progress is not None
    assert progress["phase"] == "complete"


def test_sync_records_album_download_progress(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")
    state.save_config(AppConfig(qobuz_user_id="123", qobuz_user_auth_token="uat-123"))
    client = FakePurchaseClient(tmp_path)

    result = SyncService(state, client=client).sync_once()

    assert result["success"] is True
    progress = state.latest_progress()
    assert progress is not None
    assert progress["message"] == "All Good!"


def test_sync_passes_existing_qobuz_auth_token_to_client(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")
    state.save_config(AppConfig(qobuz_user_id="123", qobuz_user_auth_token="uat-123"))
    client = FakePurchaseClient(tmp_path)

    result = SyncService(state, client=client).sync_once()

    assert result["success"] is True
    assert client.login_calls[0] == ("123", "uat-123")


def test_sync_backfills_album_art_and_extras_for_existing_downloads(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")
    state.save_config(AppConfig(qobuz_user_id="123", qobuz_user_auth_token="uat-123", embed_art=True))
    existing_audio = tmp_path / "Song Two.flac"
    existing_audio.write_text("fake audio", encoding="utf-8")
    state.mark_downloaded("track", "222", title="Song Two", path=str(existing_audio))
    client = FakePurchaseClient(tmp_path)

    result = SyncService(state, client=client).sync_once()

    assert result["success"] is True
    assert (client.extra_calls[0][0]["id"], client.extra_calls[0][1]) == ("222", str(existing_audio))


def test_sync_redownloads_recorded_item_when_audio_file_is_missing(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")
    state.save_config(AppConfig(qobuz_user_id="123", qobuz_user_auth_token="uat-123", embed_art=True))
    missing_audio = tmp_path / "deleted" / "Song Two.flac"
    state.mark_downloaded("track", "222", title="Song Two", path=str(missing_audio))
    client = FakePurchaseClient(tmp_path)

    result = SyncService(state, client=client).sync_once()

    assert result["success"] is True
    assert result["downloaded"] == 2
    assert not client.extra_calls
    assert [call[0]["id"] for call in client.download_calls] == ["111", "222"]
    assert state.downloaded_path("track", "222") == str(tmp_path / "track-222.flac")


def test_sync_clears_track_progress_after_successful_completion(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")
    state.save_config(AppConfig(qobuz_user_id="123", qobuz_user_auth_token="uat-123"))
    client = FakePurchaseClient(tmp_path)

    result = SyncService(state, client=client).sync_once()

    assert result["success"] is True
    assert state.latest_progress()["phase"] == "complete"
    assert state.list_track_progress() == []


def test_resync_entire_library_preserves_files_and_unlisted_records(tmp_path: Path, monkeypatch):
    download_dir = tmp_path / "downloads"
    stale_folder = download_dir / "Old Artist"
    stale_folder.mkdir(parents=True)
    stale_file = stale_folder / "old.flac"
    stale_file.write_text("old", encoding="utf-8")
    monkeypatch.setattr("qobuz_sync.state.DEFAULT_DOWNLOAD_DIR", str(download_dir))

    state = SyncState(tmp_path / "state.db")
    state.save_config(AppConfig(qobuz_user_id="123", qobuz_user_auth_token="uat-123"))
    state.mark_downloaded("track", "222", title="Song Two", path=str(stale_file))
    state.mark_downloaded("track", "unlisted", path=str(stale_file))
    client = FakePurchaseClient(download_dir)

    result = SyncService(state, client=client).resync_entire_library()

    assert result["success"] is True
    assert result["downloaded"] == 2
    assert stale_file.read_text() == "old"
    assert state.count_downloads() == 3
    assert state.downloaded_path("track", "unlisted") == str(stale_file)
    assert len(client.download_calls) == 2


@pytest.mark.parametrize("failure", ["unconfigured", "login", "list_owned_items"])
def test_resync_preserves_library_and_records_before_network_success(tmp_path: Path, monkeypatch, failure):
    state = SyncState(tmp_path / "state.db")
    if failure != "unconfigured":
        state.save_config(AppConfig(qobuz_user_id="123", qobuz_user_auth_token="uat-123"))
    audio = tmp_path / "old.flac"
    audio.write_bytes(b"old audio")
    state.mark_downloaded("track", "222", path=str(audio))
    before = state.list_downloads()
    client = FakePurchaseClient(tmp_path)

    def fail(**kwargs):
        raise QobuzError("Network failure")

    if failure != "unconfigured":
        monkeypatch.setattr(client, failure, fail)

    result = SyncService(state, client=client).resync_entire_library()

    assert result["success"] is False
    assert result["found"] == result["downloaded"] == 0
    assert audio.read_bytes() == b"old audio"
    assert state.list_downloads() == before
    assert client.download_calls == []


def test_sync_redownloads_album_after_one_manifest_file_is_deleted(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")
    state.save_config(AppConfig(qobuz_user_id="123", qobuz_user_auth_token="uat-123"))
    client = FakePurchaseClient(tmp_path)
    service = SyncService(state, client=client)
    service.sync_once()
    album = tmp_path / "album-111"
    (album / "02.flac").write_bytes(b"second track")
    state.mark_downloaded("album", "111", path=str(album))

    assert service.sync_once()["downloaded"] == 0
    assert any(call[1] == str(album) for call in client.extra_calls)
    (album / "02.flac").unlink()

    assert service.sync_once()["downloaded"] == 1
    assert client.download_calls[-1][0]["kind"] == "album"


def test_legacy_album_redownloads_once_and_preserves_old_directory(tmp_path: Path, monkeypatch):
    state = SyncState(tmp_path / "state.db")
    state.save_config(AppConfig(qobuz_user_id="123", qobuz_user_auth_token="uat-123"))
    legacy_album = tmp_path / "Legacy Album"
    state.mark_downloaded("album", "111", path=str(legacy_album))
    legacy_album.mkdir()
    legacy_audio = legacy_album / "01.flac"
    legacy_audio.write_bytes(b"legacy audio")
    client = FakePurchaseClient(tmp_path)
    purchases = [client.list_owned_items()[0]]
    monkeypatch.setattr(client, "list_owned_items", lambda **kwargs: purchases)
    service = SyncService(state, client=client)

    assert service.sync_once()["downloaded"] == 1
    assert state.downloaded_path("album", "111") == str(tmp_path / "album-111")
    assert SyncService(SyncState(state.db_path), client=client).sync_once()["downloaded"] == 0
    assert len(client.download_calls) == 1
    assert legacy_audio.read_bytes() == b"legacy audio"


@pytest.mark.parametrize("resync", [False, True])
def test_missing_credentials_clear_stale_active_progress(tmp_path: Path, resync):
    state = SyncState(tmp_path / "state.db")
    state.set_progress(phase="download", message="Downloading", current=1, total=3)
    state.set_track_progress("track", "222", downloaded_bytes=10, total_bytes=100, status="downloading")
    client = FakePurchaseClient(tmp_path)
    service = SyncService(state, client=client)

    result = service.resync_entire_library() if resync else service.sync_once()

    assert result["success"] is False
    assert result["found"] == result["downloaded"] == 0
    progress = state.latest_progress()
    assert progress["phase"] == "error"
    assert progress["message"] == result["message"]
    assert progress["current"] == progress["total"] == 0
    assert state.list_track_progress() == []
    assert state.latest_sync()["success"] is False
    assert not client.login_calls
    assert not client.download_calls


@pytest.mark.parametrize("failed_id", ["111", "222"])
def test_purchase_failure_does_not_starve_others_and_counts_successes(tmp_path: Path, monkeypatch, caplog, failed_id):
    state = SyncState(tmp_path / "state.db")
    state.save_config(AppConfig(qobuz_user_id="123", qobuz_user_auth_token="uat-123"))
    client = FakePurchaseClient(tmp_path)
    download = client.download_owned_item
    attempted = []

    def fail_one(item, *args, **kwargs):
        attempted.append(item["id"])
        if item["id"] == failed_id:
            raise QobuzError("request failed?user_auth_token=secret-starts-with-s&request_sig=signature")
        return download(item, *args, **kwargs)

    monkeypatch.setattr(client, "download_owned_item", fail_one)
    result = SyncService(state, client=client).sync_once()

    assert attempted == ["111", "222"]
    assert result["success"] is False
    assert result["found"] == 2
    assert result["downloaded"] == 1
    assert state.count_downloads() == 1
    assert state.latest_sync()["downloaded"] == 1
    assert state.latest_sync()["found"] == 2
    assert state.latest_progress()["phase"] == "error"
    assert "secret-starts-with-s" not in str(result) + caplog.text + str(state.latest_sync()) + str(state.latest_progress())
    assert "signature" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_resync_keeps_failed_purchase_record_and_manifest(tmp_path: Path, monkeypatch):
    state = SyncState(tmp_path / "state.db")
    state.save_config(AppConfig(qobuz_user_id="123", qobuz_user_auth_token="uat-123"))
    client = FakePurchaseClient(tmp_path)
    service = SyncService(state, client=client)
    service.sync_once()
    before = next(row for row in state.list_downloads() if row["kind"] == "album")
    download = client.download_owned_item

    def fail_album(item, *args, **kwargs):
        if item["kind"] == "album":
            raise QobuzError("Album download failed")
        return download(item, *args, **kwargs)

    monkeypatch.setattr(client, "download_owned_item", fail_album)
    result = service.resync_entire_library()

    assert result["success"] is False
    assert result["downloaded"] == 1
    assert next(row for row in state.list_downloads() if row["kind"] == "album") == before
    assert (tmp_path / "album-111" / "01.flac").read_text() == "fake audio"
    assert state.plan_new_downloads(client.list_owned_items()) == []
    with state._connect() as con:
        assert con.execute("select count(*) from download_files").fetchone()[0] == 1


@pytest.mark.parametrize("resync", [False, True])
def test_dry_run_does_not_write_audio_extras_or_download_records(tmp_path: Path, monkeypatch, resync):
    state = SyncState(tmp_path / "state.db")
    state.save_config(AppConfig(qobuz_user_id="123", qobuz_user_auth_token="uat-123"))
    audio = tmp_path / "existing.flac"
    audio.write_bytes(b"existing audio")
    state.mark_downloaded("track", "222", path=str(audio))
    before = state.list_downloads()
    client = FakePurchaseClient(tmp_path)
    service = SyncService(state, client=client)
    monkeypatch.setenv("QOBUZ_SYNC_DRY_RUN", "1")

    result = service.resync_entire_library() if resync else service.sync_once()

    assert result["success"] is True
    assert result["found"] == 2
    assert result["downloaded"] == 0
    assert "Dry run" in result["message"]
    assert not client.download_calls
    assert not client.extra_calls
    assert state.list_downloads() == before
    assert not state.is_downloaded("album", "111")
    assert audio.read_bytes() == b"existing audio"
    monkeypatch.delenv("QOBUZ_SYNC_DRY_RUN")
    assert service.sync_once()["downloaded"] == 1


def test_optional_extras_failure_does_not_block_downloads_or_other_backfills(tmp_path: Path, monkeypatch, caplog):
    state = SyncState(tmp_path / "state.db")
    state.save_config(AppConfig(qobuz_user_id="123", qobuz_user_auth_token="uat-123"))
    client = FakePurchaseClient(tmp_path)
    service = SyncService(state, client=client)
    service.sync_once()
    purchases = client.list_owned_items() + [{"kind": "track", "id": "333", "title": "Third"}]
    monkeypatch.setattr(client, "list_owned_items", lambda **kwargs: purchases)

    def extras(item, path):
        client.extra_calls.append((item, path))
        if item["kind"] == "album":
            raise QobuzError("extras failed?app_secret=supersecret")
        return []

    monkeypatch.setattr(client, "download_owned_item_extras", extras)
    result = service.sync_once()

    assert result["success"] is True
    assert result["downloaded"] == 1
    assert result["found"] == 3
    assert [call[0]["id"] for call in client.extra_calls] == ["111", "222"]
    assert "supersecret" not in caplog.text
    assert "[REDACTED]" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_track_progress_throttles_by_elapsed_time_and_always_writes_final(tmp_path: Path, monkeypatch):
    state = SyncState(tmp_path / "state.db")
    state.save_config(AppConfig(qobuz_user_id="123", qobuz_user_auth_token="uat-123"))
    client = FakePurchaseClient(tmp_path)
    monkeypatch.setattr(client, "list_owned_items", lambda **kwargs: [{"kind": "album", "id": "111"}])
    clock = [0.0]
    monkeypatch.setattr("qobuz_sync.sync.time.monotonic", lambda: clock[0])
    writes = []
    monkeypatch.setattr(state, "set_track_progress", lambda kind, purchase_id, **kwargs: writes.append((purchase_id, kwargs)))

    def download(item, *args, track_progress_callback, **kwargs):
        for elapsed, count in [(0, 0), (0.1, 10), (0.49, 49), (0.5, 50), (0.6, 60)]:
            clock[0] = elapsed
            track_progress_callback("track", "1", "First", "first.flac", count, 100, "downloading")
        track_progress_callback("track", "1", "First", "first.flac", 100, 100, "downloaded")
        track_progress_callback("track", "2", "Second", "second.flac", 0, 100, "downloading")
        track_progress_callback("track", "2", "Second", "second.flac", 0, 100, "error")
        return tmp_path

    monkeypatch.setattr(client, "download_owned_item", download)

    assert SyncService(state, client=client).sync_once()["success"] is True
    assert [(purchase_id, row["downloaded_bytes"], row["status"]) for purchase_id, row in writes] == [
        ("1", 0, "downloading"), ("1", 50, "downloading"), ("1", 100, "downloaded"),
        ("2", 0, "downloading"), ("2", 0, "error"),
    ]


def test_safe_message_redacts_complete_values_and_preserves_whitespace():
    message = "user_auth_token=starts-with-s&user_id=123 password=pass\\word\nAPP_SECRET=secret\trequest_sig=signature&plain=visible"

    assert _safe_message(QobuzError(message)) == (
        "user_auth_token=[REDACTED]&user_id=[REDACTED] password=[REDACTED]\n"
        "APP_SECRET=[REDACTED]\trequest_sig=[REDACTED]&plain=visible"
    )


def test_login_error_is_sanitized_in_logs_state_and_result(tmp_path: Path, monkeypatch, caplog):
    state = SyncState(tmp_path / "state.db")
    state.save_config(AppConfig(qobuz_user_id="123", qobuz_user_auth_token="uat-123"))
    client = FakePurchaseClient(tmp_path)

    def fail(**kwargs):
        raise QobuzError("Login failed?user_auth_token=secret&app_id=sensitive")

    monkeypatch.setattr(client, "login", fail)
    result = SyncService(state, client=client).sync_once()

    assert result["success"] is False
    surfaced = str(result) + str(state.latest_sync()) + str(state.latest_progress()) + caplog.text
    assert "secret" not in surfaced
    assert "sensitive" not in surfaced
    assert "[REDACTED]" in surfaced
    assert all(record.exc_info is None for record in caplog.records)
