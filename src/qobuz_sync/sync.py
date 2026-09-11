from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Callable, Protocol

from .qobuz_client import QobuzClient, QobuzError, discover_web_credentials
from .state import SyncState

LOGGER = logging.getLogger(__name__)
_SENSITIVE_PARAM_RE = re.compile(
    r"(user_auth_token|user_id|password|app_id|app_secret|seed|request_sig)=([^&\"'\s]+)",
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
    def login(self, *, user_id: str = "", user_auth_token: str = ""): ...
    def list_owned_items(self, *, include_albums: bool = True, include_tracks: bool = True) -> list[dict[str, str]]: ...
    def find_existing_purchase(self, item: dict[str, str], download_dir: str | Path, quality: int, *, known_path: str | Path | None = None) -> Path | None: ...
    def register_existing_purchase(self, item: dict[str, str], download_dir: str | Path, path: str | Path) -> None: ...
    def download_owned_item(self, item: dict[str, str], download_dir: str | Path, quality: int, *, include_extras: bool = True, skip_existing: bool = False, known_path: str | Path | None = None, progress_callback: Callable[[int, int, str], None] | None = None, track_progress_callback: Callable[[str, str, str, str, int, int, str], None] | None = None) -> Path: ...
    def download_owned_item_extras(self, item: dict[str, str], downloaded_path: str | Path) -> list[Path]: ...


class SyncService:
    def __init__(self, state: SyncState, client: PurchaseClient | None = None) -> None:
        self.state = state
        self.client = client

    def sync_once(self, *, force_redownload: bool = False) -> dict[str, object]:
        config = self.state.load_config()
        if not config.is_configured:
            result = {"success": False, "found": 0, "downloaded": 0, "message": "Qobuz credentials are not configured"}
            self.state.set_progress(phase="error", message="Qobuz credentials are not configured")
            self.state.clear_track_progress()
            self.state.record_sync(**result)  # type: ignore[arg-type]
            return result

        found = downloaded = discovered = 0
        total_to_download = 0
        failures: list[str] = []
        dry_run = os.environ.get("QOBUZ_SYNC_DRY_RUN", "0") == "1"
        try:
            self.state.set_progress(phase="login", message="Signing in to Qobuz", current=0, total=0)
            client = self.client or self._build_client()
            client.login(
                user_id=config.qobuz_user_id,
                user_auth_token=config.qobuz_user_auth_token,
            )
            self.state.set_progress(phase="discover", message="Reading purchased library", current=0, total=0)
            purchases = client.list_owned_items(include_albums=config.include_albums, include_tracks=config.include_tracks)
            found = len(purchases)
            plan = purchases if force_redownload else self.state.plan_new_downloads(purchases)
            planned_keys = {(item["kind"], str(item["id"])) for item in plan}
            if plan:
                self.state.clear_track_progress()
            if not force_redownload:
                missing = []
                for index, item in enumerate(plan, start=1):
                    self.state.set_progress(phase="scan", message=f"Checking existing files for {item.get('title', item['id'])}", current=index, total=len(plan))
                    try:
                        existing_path = client.find_existing_purchase(
                            item, config.download_dir, config.quality,
                            known_path=self.state.downloaded_path(item["kind"], str(item["id"])),
                        )
                        if existing_path is None:
                            missing.append(item)
                            continue
                        if not dry_run:
                            client.register_existing_purchase(item, config.download_dir, existing_path)
                            self.state.mark_downloaded(item["kind"], str(item["id"]), title=item.get("title", ""), path=str(existing_path))
                        discovered += 1
                        planned_keys.discard((item["kind"], str(item["id"])))
                    except Exception as exc:
                        message = _safe_message(exc)
                        failures.append(message)
                        LOGGER.error("Qobuz purchase disk check failed: %s", message)
                plan = missing
            self.state.set_progress(phase="plan", message=f"Found {len(purchases)} purchases; {discovered} found on disk; {len(plan)} new downloads", current=0, total=len(plan))
            total_to_download = len(plan)
            for item_index, item in enumerate(plan, start=1):
                if dry_run:
                    continue
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

                progress_writes: dict[tuple[str, str], float] = {}

                def track_progress(kind: str, purchase_id: str, title: str, path: str, downloaded_bytes: int, total_bytes: int, status: str) -> None:
                    now = time.monotonic()
                    key = (kind, purchase_id)
                    # Never throttle terminal events, even if the last chunk just arrived.
                    if status != "downloading" or now - progress_writes.get(key, float("-inf")) >= 0.5:
                        self.state.set_track_progress(kind, purchase_id, title=title, path=path, downloaded_bytes=downloaded_bytes, total_bytes=total_bytes, status=status)
                        progress_writes[key] = now

                try:
                    output_path = client.download_owned_item(
                        item,
                        config.download_dir,
                        config.quality,
                        include_extras=config.embed_art,
                        skip_existing=not force_redownload,
                        known_path=self.state.downloaded_path(item["kind"], str(item["id"])),
                        progress_callback=album_progress,
                        track_progress_callback=track_progress,
                    )
                    self.state.mark_downloaded(item["kind"], str(item["id"]), title=item.get("title", ""), path=str(output_path))
                    downloaded += 1
                except Exception as exc:
                    message = _safe_message(exc)
                    failures.append(message)
                    LOGGER.error("Qobuz purchase download failed: %s", message)
                    self.state.set_progress(phase="download", message=message, current=item_index, total=total_to_download)
                    continue
                self.state.set_progress(phase="download", message=f"Downloaded {item.get('title', item['id'])}", current=item_index, total=total_to_download)

            if config.embed_art and not dry_run:
                paths = {(row["kind"], row["purchase_id"]): row["path"] for row in self.state.list_downloads(limit=-1)}
                backfill = [
                    (item, paths.get((item["kind"], str(item["id"])))) for item in purchases
                    if (item["kind"], str(item["id"])) not in planned_keys
                    and paths.get((item["kind"], str(item["id"])))
                ]
                for index, (item, path) in enumerate(backfill, start=1):
                    self.state.set_progress(
                        phase="extras", message=f"Backfilling artwork/extras for {item.get('title', item['id'])}",
                        current=index, total=len(backfill),
                    )
                    try:
                        client.download_owned_item_extras(item, path)
                    except Exception as exc:
                        LOGGER.warning("Qobuz artwork/extras backfill failed: %s", _safe_message(exc))

            message = "Sync completed"
            if dry_run:
                message = f"Dry run: {total_to_download} purchases would be downloaded"
            elif failures:
                message = f"{len(failures)} purchase(s) failed: {'; '.join(failures)}"
            if discovered:
                message += f"; {discovered} existing purchase(s) found on disk"
            result = {"success": not failures, "found": found, "downloaded": downloaded, "message": message}
            self.state.set_progress(phase="error" if failures else "complete", message=message if failures or dry_run or discovered else "All Good!", current=total_to_download, total=total_to_download)
            self.state.clear_track_progress()
        except Exception as exc:
            LOGGER.error("Qobuz sync failed: %s", _safe_message(exc))
            result = {"success": False, "found": found, "downloaded": downloaded, "message": _safe_message(exc)}
            self.state.set_progress(phase="error", message=_safe_message(exc), current=downloaded, total=total_to_download)
            self.state.clear_track_progress()

        self.state.record_sync(**result)  # type: ignore[arg-type]
        return result

    def resync_entire_library(self) -> dict[str, object]:
        """Redownload purchases, preserving existing files and records until replacement succeeds."""
        return self.sync_once(force_redownload=True)

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
