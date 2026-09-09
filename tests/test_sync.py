from pathlib import Path
import pytest

from qobuz_sync.state import AppConfig, DEFAULT_DOWNLOAD_DIR, SyncState
from qobuz_sync.sync import SyncService
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
        if progress_callback:
            progress_callback(1, 1, item["title"])
        if track_progress_callback:
            track_progress_callback(item["kind"], str(item["id"]), item["title"], str(self.output_dir / f"{item['kind']}-{item['id']}.flac"), 10, 10, "downloaded")
        path = self.output_dir / f"{item['kind']}-{item['id']}.flac"
        path.write_text("fake audio", encoding="utf-8")
        return path

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


def test_resync_entire_library_clears_files_and_download_records(tmp_path: Path, monkeypatch):
    download_dir = tmp_path / "downloads"
    stale_folder = download_dir / "Old Artist"
    stale_folder.mkdir(parents=True)
    stale_file = stale_folder / "old.flac"
    stale_file.write_text("old", encoding="utf-8")
    monkeypatch.setattr("qobuz_sync.state.DEFAULT_DOWNLOAD_DIR", str(download_dir))
    monkeypatch.setattr("qobuz_sync.sync.DEFAULT_DOWNLOAD_DIR", str(download_dir))

    state = SyncState(tmp_path / "state.db")
    state.save_config(AppConfig(qobuz_user_id="123", qobuz_user_auth_token="uat-123"))
    state.mark_downloaded("track", "222", title="Song Two", path=str(stale_file))
    client = FakePurchaseClient(download_dir)

    result = SyncService(state, client=client).resync_entire_library()

    assert result["success"] is True
    assert result["downloaded"] == 2
    assert not stale_file.exists()
    assert state.count_downloads() == 2
    assert len(client.download_calls) == 2


def test_resync_refuses_to_clear_non_default_download_directory(tmp_path: Path, monkeypatch):
    download_dir = tmp_path / "downloads"
    unexpected_dir = tmp_path / "unexpected"
    stale_file = unexpected_dir / "old.flac"
    stale_file.parent.mkdir(parents=True)
    stale_file.write_text("old", encoding="utf-8")
    monkeypatch.setattr("qobuz_sync.state.DEFAULT_DOWNLOAD_DIR", str(download_dir))
    monkeypatch.setattr("qobuz_sync.sync.DEFAULT_DOWNLOAD_DIR", str(download_dir))

    state = SyncState(tmp_path / "state.db")
    state.save_config(AppConfig(qobuz_user_id="123", qobuz_user_auth_token="uat-123"))
    state.mark_downloaded("track", "222", title="Song Two", path=str(stale_file))
    client = FakePurchaseClient(download_dir)

    with pytest.raises(QobuzError, match="Refusing to clear unexpected download directory"):
        SyncService(state, client=client)._clear_download_dir(unexpected_dir)

    assert stale_file.exists()
