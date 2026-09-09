from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Callable, Protocol

from .qobuz_client import QobuzClient, QobuzError, discover_web_credentials
from .state import AppConfig, DEFAULT_DOWNLOAD_DIR, SyncState

LOGGER = logging.getLogger(__name__)
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._() \-\[\]]+")
_SENSITIVE_PARAM_RE = re.compile(
    r"(user_auth_token|user_id|password|app_id|app_secret|seed|request_sig)=([^&\"'\\s]+)",
    re.IGNORECASE,
)


def _safe_message(exc: Exception) -> str:
    """Stringify an exception with query-string credentials redacted.

    ``requests`` errors embed the full request URL (which carries the Qobuz
    ``user_auth_token``) in their message text; those messages flow into the UI,
    the database, and the logs, so strip sensitive parameters before surfacing.
    """
    return _SENSITIVE_PARAM_RE.sub(r"\1=[REDACTED]", str(exc))


class PurchaseClient(Protocol):
    def login(self, email: str, password_md5: str, *, user_id: str = "", user_auth_token: str = ""): ...
    def list_owned_items(self, *, include_albums: bool = True, include_tracks: bool = True) -> list[dict[str, str]]: ...
    def download_owned_item(self, item: dict[str, str], download_dir: str | Path, quality: int, *, include_extras: bool = True, progress_callback: Callable[[int, int, str], None] | None = None, track_progress_callback: Callable[[str, str, str, str, int, int, str], None] | None = None) -> Path: ...
    def download_owned_item_extras(self, item: dict[str, str], downloaded_path: str | Path) -> list[Path]: ...


class SyncService:
    def __init__(self, state: SyncState, client: PurchaseClient | None = None) -> None:
        self.state = state
        self.client = client

    def sync_once(self) -> dict[str, object]:
        config = self.state.load_config()
        if not config.is_configured:
            result = {"success": False, "found": 0, "downloaded": 0, "message": "Qobuz credentials are not configured"}
            self.state.record_sync(**result)  # type: ignore[arg-type]
            return result

        try:
            self.state.set_progress(phase="login", message="Signing in to Qobuz", current=0, total=0)
            client = self.client or self._build_client()
            client.login(
                config.qobuz_email,
                config.qobuz_password_md5,
                user_id=config.qobuz_user_id,
                user_auth_token=config.qobuz_user_auth_token,
            )
            self.state.set_progress(phase="discover", message="Reading purchased library", current=0, total=0)
            purchases = client.list_owned_items(include_albums=config.include_albums, include_tracks=config.include_tracks)
            plan = self.state.plan_new_downloads(purchases)
            if plan:
                self.state.clear_track_progress()
            self.state.set_progress(phase="plan", message=f"Found {len(purchases)} purchases; {len(plan)} new downloads", current=0, total=len(plan))
            if config.embed_art:
                backfill_total = len(
                    [
                        item
                        for item in purchases
                        if item not in plan
                        and (downloaded_path := self.state.downloaded_path(item["kind"], str(item["id"])))
                        and Path(downloaded_path).is_file()
                    ]
                )
                backfill_current = 0
                for item in purchases:
                    if item in plan:
                        continue
                    downloaded_path = self.state.downloaded_path(item["kind"], str(item["id"]))
                    if downloaded_path and Path(downloaded_path).is_file():
                        backfill_current += 1
                        self.state.set_progress(
                            phase="extras",
                            message=f"Backfilling artwork/extras for {item.get('title', item['id'])}",
                            current=backfill_current,
                            total=backfill_total,
                        )
                        client.download_owned_item_extras(item, downloaded_path)
            downloaded = 0
            dry_run = os.environ.get("QOBUZ_SYNC_DRY_RUN", "0") == "1"
            total_to_download = len(plan)
            for item_index, item in enumerate(plan, start=1):
                self.state.set_progress(
                    phase="download",
                    message=f"Downloading {item.get('title', item['id'])}",
                    current=item_index - 1,
                    total=total_to_download,
                )

                def album_progress(track_index: int, track_total: int, title: str, *, item_index: int = item_index, total_to_download: int = total_to_download) -> None:
                    self.state.set_progress(
                        phase="album",
                        message=f"Album track {track_index}/{track_total}: {title}",
                        current=item_index,
                        total=total_to_download,
                    )

                if dry_run:
                    output_path = self._write_purchase_marker(config, item)
                else:
                    def track_progress(kind: str, purchase_id: str, title: str, path: str, downloaded_bytes: int, total_bytes: int, status: str) -> None:
                        self.state.set_track_progress(kind, purchase_id, title=title, path=path, downloaded_bytes=downloaded_bytes, total_bytes=total_bytes, status=status)

                    output_path = client.download_owned_item(
                        item,
                        config.download_dir,
                        config.quality,
                        include_extras=config.embed_art,
                        progress_callback=album_progress,
                        track_progress_callback=track_progress,
                    )
                self.state.mark_downloaded(item["kind"], str(item["id"]), title=item.get("title", ""), path=str(output_path))
                downloaded += 1
                self.state.set_progress(phase="download", message=f"Downloaded {item.get('title', item['id'])}", current=item_index, total=total_to_download)
            result = {"success": True, "found": len(purchases), "downloaded": downloaded, "message": "Sync completed"}
            self.state.set_progress(phase="complete", message="All Good!", current=total_to_download, total=total_to_download)
            self.state.clear_track_progress()
        except Exception as exc:
            LOGGER.exception("Qobuz sync failed")
            result = {"success": False, "found": 0, "downloaded": 0, "message": _safe_message(exc)}
            self.state.set_progress(phase="error", message=_safe_message(exc), current=0, total=0)
            self.state.clear_track_progress()

        self.state.record_sync(**result)  # type: ignore[arg-type]
        return result

    def resync_entire_library(self) -> dict[str, object]:
        """Delete existing downloaded files and download the full purchased library again."""
        config = self.state.load_config()
        if not config.is_configured:
            result = {"success": False, "found": 0, "downloaded": 0, "message": "Qobuz credentials are not configured"}
            self.state.record_sync(**result)  # type: ignore[arg-type]
            return result
        self.state.set_progress(phase="reset", message="Clearing existing downloaded library", current=0, total=0)
        self._clear_download_dir(Path(config.download_dir))
        self.state.clear_downloads()
        self.state.clear_track_progress()
        return self.sync_once()

    @staticmethod
    def _clear_download_dir(download_dir: Path) -> None:
        """Remove only the contents of Qobuz Sync's fixed download directory."""
        resolved = download_dir.resolve()
        allowed = Path(DEFAULT_DOWNLOAD_DIR).resolve()
        if resolved != allowed:
            raise QobuzError(f"Refusing to clear unexpected download directory: {download_dir}")
        resolved.mkdir(parents=True, exist_ok=True)
        for child in resolved.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()

    def _build_client(self) -> QobuzClient:
        app_id = os.environ.get("QOBUZ_APP_ID")
        secrets = tuple(filter(None, os.environ.get("QOBUZ_SECRETS", "").split(",")))
        if not app_id:
            credentials = discover_web_credentials()
            app_id = credentials.app_id
            secrets = credentials.secrets
        if not app_id:
            raise QobuzError("Qobuz app id could not be discovered")
        return QobuzClient(app_id, secrets=secrets)

    @staticmethod
    def _write_purchase_marker(config: AppConfig, item: dict[str, str]) -> Path:
        download_dir = Path(config.download_dir)
        safe_title = _SAFE_NAME_RE.sub("_", item.get("title") or f"{item['kind']}-{item['id']}").strip()[:120]
        folder = download_dir / "_qobuz-sync-pending"
        folder.mkdir(parents=True, exist_ok=True)
        marker = folder / f"{item['kind']}-{item['id']}-{safe_title}.txt"
        marker.write_text(
            "Qobuz Sync discovered this purchased item.\n"
            "Audio download enablement requires live Qobuz account integration testing.\n"
            f"Kind: {item['kind']}\nID: {item['id']}\nTitle: {item.get('title', '')}\n",
            encoding="utf-8",
        )
        return marker
