from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
from contextlib import contextmanager

import pytest

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


@pytest.mark.parametrize("status", ["downloaded", "downloading", "pending"])
def test_mark_downloaded_retires_only_finished_matching_track(tmp_path: Path, status):
    state = SyncState(tmp_path / "state.db")
    state.set_track_progress("track", "123", path="/downloads/old.flac", status=status)
    state.set_track_progress("album", "123", status="downloaded")
    state.set_track_progress("track", "456", path="/downloads/song.flac", status="downloaded")

    state.mark_downloaded("track", "123", path="/downloads/song.flac")

    expected = {("album", "123"), ("track", "456")}
    if status != "downloaded":
        expected.add(("track", "123"))
    assert {(row["kind"], row["purchase_id"]) for row in state.list_track_progress()} == expected
    assert state.is_downloaded("track", "123")
    assert state.count_downloads() == 1


def test_mark_downloaded_retires_only_finished_direct_album_children(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")
    album = tmp_path / "Album_100%"
    progress = [
        ("finished-1", album / "01.flac", "downloaded"),
        ("finished-2", album / "02.flac", "downloaded"),
        ("active", album / "03.flac", "downloading"),
        ("pending", album / "04.flac", "pending"),
        ("prefix", tmp_path / "Album_100% Deluxe" / "01.flac", "downloaded"),
        ("wildcards", tmp_path / "AlbumX100more" / "01.flac", "downloaded"),
        ("nested", album / "Disc 2" / "01.flac", "downloaded"),
        ("unrelated", tmp_path / "Other" / "01.flac", "downloaded"),
        ("no-path", "", "downloaded"),
    ]
    for purchase_id, path, status in progress:
        state.set_track_progress("track", purchase_id, path=str(path), status=status)
    before = state.list_track_progress()

    state.mark_downloaded("album", "123", path=str(album))

    assert state.list_track_progress() == [
        row for row in before if row["purchase_id"] not in {"finished-1", "finished-2"}
    ]
    assert state.is_downloaded("album", "123")
    assert state.count_downloads() == 1


@pytest.mark.parametrize("kind", ["track", "album"])
def test_progress_retirement_and_download_record_share_transaction(tmp_path: Path, kind):
    state = SyncState(tmp_path / "state.db")
    state.set_track_progress("track", "123", path="/downloads/album/song.flac", status="downloaded")
    with state._connect() as con:
        con.execute("""
            create trigger reject_progress_retirement after delete on track_progress
            begin
                select raise(abort, 'retirement failed');
            end
        """)

    path = "/downloads/album/song.flac" if kind == "track" else "/downloads/album"
    with pytest.raises(sqlite3.IntegrityError, match="retirement failed"):
        state.mark_downloaded(kind, "123", path=path)

    assert state.count_downloads() == 0
    assert [row["purchase_id"] for row in state.list_track_progress()] == ["123"]


@pytest.mark.parametrize("missing", ["one", "all", "empty"])
def test_album_manifest_detects_missing_audio_but_ignores_extras(tmp_path: Path, missing):
    state = SyncState(tmp_path / "state.db")
    album = tmp_path / "album"
    disc = album / "Disc 2"
    disc.mkdir(parents=True)
    first = album / "01.flac"
    second = disc / "02.MP3"
    first.write_bytes(b"audio one")
    second.write_bytes(b"audio two")
    cover = album / "cover.jpg"
    cover.write_bytes(b"cover")
    purchase = {"kind": "album", "id": "111"}
    state.mark_downloaded("album", "111", path=str(album))

    # The manifest survives reopening the database and optional extras removal.
    state = SyncState(state.db_path)
    cover.unlink()
    assert state.plan_new_downloads([purchase]) == []
    if missing == "empty":
        second.write_bytes(b"")
    else:
        second.unlink()
        if missing == "all":
            first.unlink()
    assert state.plan_new_downloads([purchase]) == [purchase]


@pytest.mark.parametrize("contents", [None, "cover.jpg", "song.flac"])
def test_legacy_album_directory_requires_redownload_even_with_audio(tmp_path: Path, contents):
    state = SyncState(tmp_path / "state.db")
    album = tmp_path / "album"
    # Recording before the directory exists emulates a legacy record with no manifest.
    state.mark_downloaded("album", "111", path=str(album))
    album.mkdir()
    if contents:
        (album / contents).write_bytes(b"contents")
    purchase = {"kind": "album", "id": "111"}

    assert state.plan_new_downloads([purchase]) == [purchase]
    assert state.plan_new_downloads([purchase]) == [purchase]
    (album / "complete.flac").write_bytes(b"downloaded audio")
    state.mark_downloaded("album", "111", path=str(album))
    assert SyncState(state.db_path).plan_new_downloads([purchase]) == []


def test_album_manifest_is_replaced_and_cleared_with_download(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")
    album = tmp_path / "album"
    album.mkdir()
    old = album / "old.flac"
    old.write_bytes(b"audio")
    state.mark_downloaded("album", "111", path=str(album))
    old.unlink()
    (album / "new.flac").write_bytes(b"new audio")
    state.mark_downloaded("album", "111", path=str(album))

    assert state.plan_new_downloads([{"kind": "album", "id": "111"}]) == []
    with state._connect() as con:
        assert [row[0] for row in con.execute("select relative_path from download_files")] == ["new.flac"]
    state.clear_downloads()
    with state._connect() as con:
        assert con.execute("select count(*) from download_files").fetchone()[0] == 0


@pytest.mark.parametrize("rollback", [False, True])
def test_connections_close_and_preserve_transaction_semantics(tmp_path: Path, monkeypatch, rollback):
    connect = sqlite3.connect
    connections = []

    def tracked_connect(*args, **kwargs):
        con = connect(*args, **kwargs)
        connections.append(con)
        return con

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    state = SyncState(tmp_path / "state.db")
    try:
        with state._connect() as con:
            con.execute("insert into config(key, value) values('test', 'value')")
            if rollback:
                raise RuntimeError("rollback")
    except RuntimeError:
        pass
    with state._connect() as con:
        assert bool(con.execute("select 1 from config where key='test'").fetchone()) is not rollback
    for con in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            con.execute("select 1")


def test_plan_uses_one_select_for_all_purchases(tmp_path: Path, monkeypatch):
    state = SyncState(tmp_path / "state.db")
    state.mark_downloaded("album", "111")
    purchases = [{"kind": "album", "id": str(index)} for index in range(200)]
    queries = []
    connect = state._connect

    @contextmanager
    def traced_connect():
        with connect() as con:
            con.set_trace_callback(queries.append)
            yield con

    monkeypatch.setattr(state, "_connect", traced_connect)

    assert len(state.plan_new_downloads(purchases)) == 199
    assert len(queries) == 1
    assert "left join download_files" in queries[0]


@pytest.mark.parametrize("existing", [False, True])
def test_database_and_sidecars_are_private_without_changing_shared_parent(tmp_path: Path, existing):
    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(0o755)
    unrelated = parent / "other.db-wal"
    unrelated.write_bytes(b"unrelated")
    unrelated.chmod(0o644)
    db_path = parent / "state.db"
    state = SyncState(db_path)
    state.save_config(AppConfig(qobuz_user_id="123", qobuz_user_auth_token="secret"))

    with state._connect() as con:
        con.execute("select * from config").fetchall()
        files = [db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]
        assert all(path.exists() for path in files)
        if existing:
            for path in files:
                path.chmod(0o666)
            state = SyncState(db_path)
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files)
        assert state.load_config().qobuz_user_auth_token == "secret"

    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(unrelated.stat().st_mode) == 0o644
    assert unrelated.read_bytes() == b"unrelated"


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_database_permission_changes_do_not_follow_symlinks(tmp_path: Path, suffix):
    unrelated = tmp_path / "unrelated"
    unrelated.write_bytes(b"unrelated")
    unrelated.chmod(0o644)
    db_path = tmp_path / "state.db"
    Path(f"{db_path}{suffix}").symlink_to(unrelated)

    with pytest.raises(OSError):
        SyncState(db_path)

    assert stat.S_IMODE(unrelated.stat().st_mode) == 0o644
    assert unrelated.read_bytes() == b"unrelated"


def test_permission_updates_preserve_active_sqlite_writer_locks(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")
    with state._connect() as con:
        con.execute("insert into config(key, value) values('test', 'uncommitted')")
        state.load_config()
        probe = subprocess.run(
            [
                sys.executable, "-c",
                "import sqlite3, sys\n"
                "con = sqlite3.connect(sys.argv[1], timeout=0)\n"
                "try:\n"
                "    con.execute('begin immediate')\n"
                "except sqlite3.OperationalError as exc:\n"
                "    sys.exit(0 if exc.sqlite_errorcode == sqlite3.SQLITE_BUSY else 2)\n"
                "else:\n"
                "    con.rollback()\n"
                "    sys.exit(1)\n",
                str(state.db_path),
            ],
            capture_output=True, text=True, timeout=10,
        )
        assert probe.returncode == 0, probe.stderr
