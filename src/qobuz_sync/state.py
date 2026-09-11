from __future__ import annotations

import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_DOWNLOAD_DIR = "/downloads"
CLEARED_SENSITIVE_VALUE = str()
_AUDIO_SUFFIXES = {".flac", ".mp3", ".m4a", ".wav", ".aif", ".aiff", ".ogg", ".opus"}


@dataclass(frozen=True)
class AppConfig:
    qobuz_email: str = ""
    qobuz_password: str = ""
    qobuz_password_md5: str = ""
    qobuz_user_id: str = ""
    qobuz_user_auth_token: str = ""
    download_dir: str = DEFAULT_DOWNLOAD_DIR
    quality: int = 6
    interval_minutes: int = 60
    embed_art: bool = True
    include_albums: bool = True
    include_tracks: bool = True

    @property
    def is_configured(self) -> bool:
        return bool(self.qobuz_user_id and self.qobuz_user_auth_token)


class SyncState:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def save_config(self, config: AppConfig) -> None:
        rows = {
            "qobuz_email": config.qobuz_email,
            "qobuz_password": CLEARED_SENSITIVE_VALUE,
            "qobuz_password_md5": CLEARED_SENSITIVE_VALUE,
            "qobuz_user_id": config.qobuz_user_id,
            "qobuz_user_auth_token": config.qobuz_user_auth_token,
            "download_dir": DEFAULT_DOWNLOAD_DIR,
            "quality": str(config.quality),
            "interval_minutes": str(config.interval_minutes),
            "embed_art": "1" if config.embed_art else "0",
            "include_albums": "1" if config.include_albums else "0",
            "include_tracks": "1" if config.include_tracks else "0",
        }
        with self._connect() as con:
            con.executemany(
                "insert into config(key, value) values(?, ?) on conflict(key) do update set value=excluded.value",
                rows.items(),
            )

    def load_config(self) -> AppConfig:
        with self._connect() as con:
            values = dict(con.execute("select key, value from config").fetchall())
        return AppConfig(
            qobuz_email=values.get("qobuz_email", ""),
            qobuz_password=CLEARED_SENSITIVE_VALUE,
            qobuz_password_md5=CLEARED_SENSITIVE_VALUE,
            qobuz_user_id=values.get("qobuz_user_id", ""),
            qobuz_user_auth_token=values.get("qobuz_user_auth_token", ""),
            # Download location is fixed. /downloads is mounted to Umbrel's
            # Downloads/QobuzSync folder by the app package, so ignore older saved UI values.
            download_dir=DEFAULT_DOWNLOAD_DIR,
            quality=int(values.get("quality", "6")),
            interval_minutes=int(values.get("interval_minutes", "60")),
            embed_art=values.get("embed_art", "1") == "1",
            include_albums=values.get("include_albums", "1") == "1",
            include_tracks=values.get("include_tracks", "1") == "1",
        )

    def mark_downloaded(self, kind: str, purchase_id: str, *, title: str = "", path: str = "") -> None:
        album_dir = Path(path)
        audio_files = [
            str(file.relative_to(album_dir))
            for file in album_dir.rglob("*")
            if file.suffix.lower() in _AUDIO_SUFFIXES and file.is_file()
        ] if kind == "album" and path and album_dir.is_dir() else []
        with self._connect() as con:
            con.execute(
                """
                insert into downloads(kind, purchase_id, title, path, downloaded_at)
                values(?, ?, ?, ?, datetime('now'))
                on conflict(kind, purchase_id) do update set
                  title=excluded.title,
                  path=excluded.path,
                  downloaded_at=excluded.downloaded_at
                """,
                (kind, purchase_id, title, path),
            )
            con.execute("delete from download_files where kind=? and purchase_id=?", (kind, purchase_id))
            con.executemany(
                "insert into download_files(kind, purchase_id, relative_path) values(?, ?, ?)",
                [(kind, purchase_id, file) for file in audio_files],
            )
            if kind == "track":
                con.execute(
                    "delete from track_progress where kind=? and purchase_id=? and status='downloaded'",
                    (kind, purchase_id),
                )
            elif kind == "album" and path:
                rows = con.execute(
                    "select purchase_id, path from track_progress where kind='track' and status='downloaded'"
                ).fetchall()
                con.executemany(
                    "delete from track_progress where kind='track' and purchase_id=? and status='downloaded'",
                    [(row["purchase_id"],) for row in rows if row["path"] and Path(row["path"]).parent == album_dir],
                )

    def is_downloaded(self, kind: str, purchase_id: str) -> bool:
        with self._connect() as con:
            row = con.execute(
                "select 1 from downloads where kind=? and purchase_id=?",
                (kind, purchase_id),
            ).fetchone()
        return row is not None

    def downloaded_path(self, kind: str, purchase_id: str) -> str | None:
        with self._connect() as con:
            row = con.execute(
                "select path from downloads where kind=? and purchase_id=?",
                (kind, purchase_id),
            ).fetchone()
        return str(row["path"]) if row and row["path"] else None

    def plan_new_downloads(self, purchases: list[dict[str, str]]) -> list[dict[str, str]]:
        purchase_keys = {(item["kind"], str(item["id"])) for item in purchases}
        with self._connect() as con:
            rows = con.execute(
                """
                select d.kind, d.purchase_id, d.path, f.relative_path
                from downloads d left join download_files f
                  on d.kind=f.kind and d.purchase_id=f.purchase_id
                """
            ).fetchall()
        records: dict[tuple[str, str], tuple[str, list[str]]] = {}
        for row in rows:
            _, files = records.setdefault((row["kind"], row["purchase_id"]), (row["path"], []))
            if row["relative_path"] is not None:
                files.append(row["relative_path"])
        downloaded = set()
        for key, (path, files) in records.items():
            if key not in purchase_keys:
                continue
            if not path:
                downloaded.add(key)
                continue
            root = Path(path)
            try:
                if files:
                    present = root.is_dir() and all(
                        (root / file).is_file() and (root / file).stat().st_size > 0 for file in files
                    )
                elif root.is_file():
                    present = root.stat().st_size > 0
                else:
                    # Legacy directories need a metadata-backed disk check or download to establish a manifest.
                    present = False
            except OSError:
                present = False
            if present:
                downloaded.add(key)
        return [item for item in purchases if (item["kind"], str(item["id"])) not in downloaded]

    def clear_downloads(self) -> None:
        with self._connect() as con:
            con.execute("delete from download_files")
            con.execute("delete from downloads")

    def set_track_progress(
        self,
        kind: str,
        purchase_id: str,
        *,
        title: str = "",
        path: str = "",
        downloaded_bytes: int = 0,
        total_bytes: int = 0,
        status: str = "pending",
    ) -> None:
        with self._connect() as con:
            con.execute(
                """
                insert into track_progress(kind, purchase_id, title, path, downloaded_bytes, total_bytes, status, updated_at)
                values(?, ?, ?, ?, ?, ?, ?, datetime('now'))
                on conflict(kind, purchase_id) do update set
                  title=excluded.title,
                  path=excluded.path,
                  downloaded_bytes=excluded.downloaded_bytes,
                  total_bytes=excluded.total_bytes,
                  status=excluded.status,
                  updated_at=excluded.updated_at
                """,
                (kind, purchase_id, title, path, max(0, int(downloaded_bytes)), max(0, int(total_bytes)), status),
            )

    def list_track_progress(self, limit: int | None = 50) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                """
                select kind, purchase_id, title, path, downloaded_bytes, total_bytes, status
                from track_progress order by updated_at desc, rowid desc limit ?
                """,
                (limit if limit is not None else -1,),
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_track_progress(self) -> None:
        with self._connect() as con:
            con.execute("delete from track_progress")

    def set_progress(self, *, phase: str, message: str, current: int = 0, total: int = 0) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as con:
            con.execute("delete from sync_progress")
            con.execute(
                """
                insert into sync_progress(phase, message, current, total, updated_at)
                values(?, ?, ?, ?, ?)
                """,
                (phase, message, current, total, now),
            )

    def latest_progress(self) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute(
                """
                select phase, message, current, total, updated_at
                from sync_progress order by id desc limit 1
                """
            ).fetchone()
        return dict(row) if row else None

    def list_downloads(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                """
                select kind, purchase_id, title, path, downloaded_at
                from downloads order by downloaded_at desc limit ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_downloads(self) -> int:
        with self._connect() as con:
            row = con.execute("select count(*) from downloads").fetchone()
        return int(row[0]) if row else 0

    def record_sync(self, *, success: bool, found: int, downloaded: int, message: str) -> None:
        with self._connect() as con:
            con.execute(
                """
                insert into sync_runs(started_at, finished_at, success, found, downloaded, message)
                values(datetime('now'), datetime('now'), ?, ?, ?, ?)
                """,
                (1 if success else 0, found, downloaded, message),
            )

    def latest_sync(self) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute(
                """
                select started_at, finished_at, success, found, downloaded, message
                from sync_runs order by id desc limit 1
                """
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["success"] = bool(result["success"])
        return result

    def _init_db(self) -> None:
        with self._connect() as con:
            con.executescript(
                """
                create table if not exists config(
                  key text primary key,
                  value text not null
                );
                create table if not exists downloads(
                  id integer primary key autoincrement,
                  kind text not null,
                  purchase_id text not null,
                  title text not null default '',
                  path text not null default '',
                  downloaded_at text not null,
                  unique(kind, purchase_id)
                );
                create table if not exists download_files(
                  kind text not null,
                  purchase_id text not null,
                  relative_path text not null,
                  primary key(kind, purchase_id, relative_path)
                );
                create table if not exists sync_runs(
                  id integer primary key autoincrement,
                  started_at text not null,
                  finished_at text not null,
                  success integer not null,
                  found integer not null,
                  downloaded integer not null,
                  message text not null default ''
                );
                create table if not exists sync_progress(
                  id integer primary key check (id = 1),
                  phase text not null default 'idle',
                  message text not null default '',
                  current integer not null default 0,
                  total integer not null default 0,
                  updated_at text not null
                );
                create table if not exists track_progress(
                  kind text not null,
                  purchase_id text not null,
                  title text not null default '',
                  path text not null default '',
                  downloaded_bytes integer not null default 0,
                  total_bytes integer not null default 0,
                  status text not null default 'pending',
                  updated_at text not null,
                  unique(kind, purchase_id)
                );
                """
            )
            con.execute("update config set value = ? where key = ?", (CLEARED_SENSITIVE_VALUE, "qobuz_password"))
            con.execute("update config set value = ? where key = ?", (CLEARED_SENSITIVE_VALUE, "qobuz_password_md5"))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # Create the DB privately; SQLite inherits its mode for new sidecars.
        # Restrict existing sidecars too, without touching a possibly shared parent.
        try:
            self.db_path.touch(mode=0o600, exist_ok=False)
        except FileExistsError:
            pass
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = Path(f"{self.db_path}{suffix}")
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise OSError(f"SQLite state path is not a regular file: {path}")
                path.chmod(0o600, follow_symlinks=False)
            except FileNotFoundError:
                if not suffix:
                    raise
        con = sqlite3.connect(self.db_path, timeout=30)
        try:
            con.row_factory = sqlite3.Row
            con.execute("pragma journal_mode=wal")
            con.execute("pragma busy_timeout=30000")
            with con:
                yield con
        finally:
            con.close()
