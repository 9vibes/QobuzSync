from __future__ import annotations

import base64
import hashlib
import logging
import math
import os
import re
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import requests

LOGGER = logging.getLogger(__name__)
QOBUZ_API_BASE = "https://www.qobuz.com/api.json/0.2"
QOBUZ_WEB_BASE = "https://play.qobuz.com"

_BUNDLE_URL_RE = re.compile(r'<script src="(/resources/\d+\.\d+\.\d+-[a-z]\d{3}/bundle\.js)"></script>')
_APP_ID_RE = re.compile(r'production:\{api:\{appId:"(?P<app_id>\d{9})",appSecret:"\w{32}"')
_SEED_TIMEZONE_RE = re.compile(r'[a-z]\.initialSeed\("(?P<seed>[\w=]+)",window\.utimezone\.(?P<timezone>[a-z]+)\)')
_INFO_EXTRAS_TEMPLATE = r'name:"\w+/(?P<timezone>{timezones})",info:"(?P<info>[\w=]+)",extras:"(?P<extras>[\w=]+)"'

PurchaseKind = Literal["album", "track"]
PurchaseApiType = Literal["albums", "tracks"]


class QobuzError(RuntimeError):
    """Base error for Qobuz API failures."""


class IneligibleAccountError(QobuzError):
    """Raised when the account cannot stream/download music."""


class AuthenticationError(QobuzError):
    """Raised when Qobuz rejects credentials."""


@dataclass(frozen=True)
class LoginResult:
    membership: str
    user_auth_token: str


@dataclass(frozen=True)
class QobuzWebCredentials:
    app_id: str
    secrets: tuple[str, ...]


def discover_web_credentials(session: requests.Session | None = None, timeout: int = 30) -> QobuzWebCredentials:
    """Discover Qobuz web app id and request-signing secrets from the public web bundle.

    Qobuz does not publish a stable public app-registration flow for this use case. This mirrors the
    technique used by qobuz-dl: read the current web bundle and extract the app id/secrets the web app
    itself uses. This is intentionally isolated so it can be replaced if Qobuz offers official OAuth/API
    credentials later.
    """
    http = session or requests.Session()
    http.headers.update({"User-Agent": "Mozilla/5.0"})
    login_html = http.get(f"{QOBUZ_WEB_BASE}/login", timeout=timeout)
    login_html.raise_for_status()
    bundle_match = _BUNDLE_URL_RE.search(login_html.text)
    if not bundle_match:
        raise QobuzError("Could not locate Qobuz web bundle URL")

    bundle = http.get(QOBUZ_WEB_BASE + bundle_match.group(1), timeout=timeout)
    bundle.raise_for_status()
    bundle_text = bundle.text

    app_id_match = _APP_ID_RE.search(bundle_text)
    if not app_id_match:
        raise QobuzError("Could not locate Qobuz app id in web bundle")

    secrets: dict[str, list[str]] = {}
    for match in _SEED_TIMEZONE_RE.finditer(bundle_text):
        seed, timezone = match.group("seed", "timezone")
        secrets[timezone] = [seed]

    if len(secrets) >= 2:
        ordered_keys = list(secrets)
        # Preserve qobuz-dl behavior: the second timezone has to be tested first for many bundles.
        second_key = ordered_keys[1]
        secrets = {second_key: secrets[second_key], **{k: v for k, v in secrets.items() if k != second_key}}

    if secrets:
        timezones = "|".join(tz.capitalize() for tz in secrets)
        info_extras_re = re.compile(_INFO_EXTRAS_TEMPLATE.format(timezones=timezones))
        for match in info_extras_re.finditer(bundle_text):
            timezone, info, extras = match.group("timezone", "info", "extras")
            secrets[timezone.lower()] += [info, extras]

    decoded_secrets: list[str] = []
    for parts in secrets.values():
        try:
            decoded = base64.standard_b64decode("".join(parts)[:-44]).decode("utf-8")
        except Exception as exc:  # pragma: no cover - web bundle shape varies
            LOGGER.debug("Skipping undecodable Qobuz secret: %s", type(exc).__name__)
            continue
        if decoded:
            decoded_secrets.append(decoded)

    return QobuzWebCredentials(app_id=app_id_match.group("app_id"), secrets=tuple(decoded_secrets))


class QobuzClient:
    """Small Qobuz API client focused on user purchases and owned-track downloads."""

    def __init__(
        self,
        app_id: str,
        *,
        secrets: tuple[str, ...] | list[str] = (),
        session: Any | None = None,
        base_url: str = QOBUZ_API_BASE,
        timeout: int = 30,
    ) -> None:
        self.app_id = str(app_id)
        self.secrets = tuple(secrets)
        self.session = session or requests.Session()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.user_auth_token: str | None = None
        self.active_secret: str | None = None
        # Media requests share this session, so credentials must be request-local.
        self.session.headers.pop("X-User-Auth-Token", None)
        self.session.headers.pop("X-App-Id", None)
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) QobuzSync/0.1",
                "Content-Type": "application/json;charset=UTF-8",
            }
        )

    def login(
        self,
        *,
        user_id: str = "",
        user_auth_token: str = "",
    ) -> LoginResult:
        if not (user_id and user_auth_token):
            raise AuthenticationError("Qobuz user ID and auth token are required")
        payload = self._get("user/login", user_id=user_id, user_auth_token=user_auth_token, app_id=self.app_id)
        token = payload.get("user_auth_token")
        if not token:
            raise AuthenticationError("Qobuz login did not return a user auth token")
        self.user_auth_token = token
        user = payload.get("user", {}) if isinstance(payload.get("user"), dict) else {}
        credentials = user.get("credential", {}).get("parameters") if isinstance(user.get("credential"), dict) else None
        subscription = user.get("subscription") if isinstance(user.get("subscription"), dict) else None
        membership = (
            (credentials or {}).get("short_label")
            or (subscription or {}).get("offer")
            or (subscription or {}).get("label")
            or "Purchased music account"
        )
        return LoginResult(membership=str(membership), user_auth_token=token)

    def get_purchase_ids(self) -> dict[str, Any]:
        self._require_login()
        return self._get("purchase/getUserPurchasesIds", user_auth_token=self.user_auth_token)

    def get_purchases(self, purchase_type: PurchaseApiType, *, limit: int = 500, offset: int = 0) -> dict[str, Any]:
        self._require_login()
        if purchase_type not in {"albums", "tracks"}:
            raise ValueError("purchase_type must be 'albums' or 'tracks'")
        return self._get(
            "purchase/getUserPurchases",
            user_auth_token=self.user_auth_token,
            type=purchase_type,
            limit=limit,
            offset=offset,
        )

    def iter_purchases(self, purchase_type: PurchaseApiType) -> list[dict[str, Any]]:
        """Fetch every purchased album or track page and normalize to item dictionaries."""
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        total: int | None = None
        while True:
            offset = len(items)
            payload = self.get_purchases(purchase_type, limit=500, offset=offset)
            block = payload.get(purchase_type, payload)
            if not isinstance(block, dict) or not isinstance(block.get("items"), list):
                raise QobuzError("Invalid purchase page")
            if not re.fullmatch(r"[0-9]+", str(block.get("total", ""))):
                raise QobuzError("Invalid or missing purchase total")
            page_total = int(block["total"])
            if total is not None and page_total != total:
                raise QobuzError("Purchase total changed during pagination")
            total = page_total
            if "offset" in block and str(block["offset"]) != str(offset):
                raise QobuzError("Unexpected purchase page offset")
            page_items = block["items"]
            if not page_items and offset < total:
                raise QobuzError("Incomplete purchase listing")
            for item in page_items:
                if not isinstance(item, dict):
                    raise QobuzError("Invalid purchase metadata")
                item_id = self._safe_id(item.get("id"))
                if item_id in seen:
                    raise QobuzError("Duplicate item in purchase listing")
                seen.add(item_id)
                items.append(item)
            if len(items) > total:
                raise QobuzError("Inconsistent purchase total")
            if len(items) == total:
                return items

    def list_owned_items(self, *, include_albums: bool = True, include_tracks: bool = True) -> list[dict[str, str]]:
        owned: list[dict[str, str]] = []
        if include_albums:
            for album in self.iter_purchases("albums"):
                owned.append({"kind": "album", "id": str(album.get("id")), "title": self._album_title(album)})
        if include_tracks:
            for track in self.iter_purchases("tracks"):
                owned.append({"kind": "track", "id": str(track.get("id")), "title": self._track_title(track)})
        return [item for item in owned if item["id"] and item["id"] != "None"]

    def get_album(self, album_id: str) -> dict[str, Any]:
        album = self._get("album/get", album_id=album_id, limit=500, offset=0)
        block = album.get("tracks")
        if not isinstance(block, dict) or not isinstance(block.get("items"), list):
            raise QobuzError(f"Album {album_id} did not include track metadata")
        if "offset" in block and str(block["offset"]) != "0":
            raise QobuzError(f"Album {album_id} returned the wrong track page")
        tracks: list[dict[str, Any]] = []
        seen: set[str] = set()
        try:
            total = int(block.get("total", album.get("tracks_count", len(block["items"]))))
            if "tracks_count" in album and int(album["tracks_count"]) != total:
                raise ValueError("inconsistent totals")
        except (ValueError, TypeError) as exc:
            raise QobuzError(f"Album {album_id} has invalid track totals") from exc
        while True:
            page = block.get("items")
            if not isinstance(page, list) or not page:
                raise QobuzError(f"Album {album_id} has an incomplete track listing")
            for track in page:
                if not isinstance(track, dict):
                    raise QobuzError(f"Album {album_id} has invalid track metadata")
                track_id = self._safe_id(track.get("id"))
                if track_id in seen:
                    raise QobuzError(f"Album {album_id} repeats track {track_id}")
                seen.add(track_id)
                tracks.append(track)
            if len(tracks) > total:
                raise QobuzError(f"Album {album_id} has inconsistent track totals")
            if len(tracks) == total:
                break
            payload = self._get("album/get", album_id=album_id, limit=500, offset=len(tracks))
            block = payload.get("tracks")
            if not isinstance(block, dict):
                raise QobuzError(f"Album {album_id} has an incomplete track listing")
            if str(block.get("total", total)) != str(total):
                raise QobuzError(f"Album {album_id} track total changed during pagination")
            if "offset" in block and str(block["offset"]) != str(len(tracks)):
                raise QobuzError(f"Album {album_id} returned the wrong track page")
        album["tracks"] = {**album["tracks"], "items": tracks, "total": total}
        return album

    def get_track(self, track_id: str) -> dict[str, Any]:
        return self._get("track/get", track_id=track_id)

    def get_artist(self, artist_id: str) -> dict[str, Any]:
        return self._get("artist/get", artist_id=artist_id)

    def get_track_file_url(self, track_id: str, quality: int) -> str:
        candidate_secrets = [self.active_secret] if self.active_secret else []
        candidate_secrets.extend(secret for secret in self.secrets if secret and secret not in candidate_secrets)
        if not candidate_secrets:
            raise QobuzError("No Qobuz request-signing secret is configured")
        last_error: Exception | None = None
        for secret in candidate_secrets:
            unix = time.time()
            # Qobuz's current web/reference clients request a stream URL here;
            # purchased tracks are still downloaded by this app after receiving
            # that signed URL. The older "download" intent now returns 400s for
            # valid purchases/tokens.
            signature_base = f"trackgetFileUrlformat_id{quality}intentstreamtrack_id{track_id}{unix}{secret}"
            signature = hashlib.md5(signature_base.encode("utf-8"), usedforsecurity=False).hexdigest()
            try:
                payload = self._get(
                    "track/getFileUrl",
                    request_ts=unix,
                    request_sig=signature,
                    track_id=track_id,
                    format_id=quality,
                    intent="stream",
                )
            except Exception as exc:
                last_error = exc
                continue
            url = payload.get("url")
            if url:
                self.active_secret = secret
                return str(url)
        if last_error:
            raise QobuzError(f"Qobuz did not return a file URL for track {track_id}: {last_error}") from last_error
        raise QobuzError(f"Qobuz did not return a file URL for track {track_id}")


    def find_existing_purchase(
        self,
        item: dict[str, str],
        download_dir: str | Path,
        quality: int,
        *,
        known_path: str | Path | None = None,
    ) -> Path | None:
        """Read-only discovery in the QobuzSync layout or an explicit legacy path.

        Return a track file or a complete album directory, never a partial purchase.
        Relative known_path values are relative to download_dir, not the working directory.
        Unmarked albums require the exact audio filename set across FLAC and MP3,
        plus raw title/album/artist tags.
        Mutagen stream information is checked, not a full decode or lossless tier.
        Metadata API errors propagate; inaccessible or untrusted files do not match.
        """
        kind = item["kind"]
        item_id = self._safe_id(item["id"])
        extension = ".mp3" if quality == 5 else ".flac"
        if kind == "track":
            track = self.get_track(item_id)
            album = track.get("album", {}) if isinstance(track.get("album"), dict) else {}
            album_id = album.get("id")
            artist = self._track_artist_name(track, album)
            title = self._album_title_only(album) if album else "Singles"
            filenames = [f"{safe_filename(self._track_title_only(track))} [{item_id}]{extension}"]
            tracks = [track]
        elif kind == "album":
            album = self.get_album(item_id)
            album_id = item_id
            artist = self._album_artist_name(album)
            title = self._album_title_only(album)
            tracks = album["tracks"]["items"]
            filenames = [f"{index:02d}. {safe_filename(self._track_title_only(track))}{extension}"
                         for index, track in enumerate(tracks, start=1)]
        else:
            raise ValueError("item kind must be 'album' or 'track'")
        album_id = self._safe_id(album_id) if album_id is not None else None
        try:
            root = Path(download_dir).resolve()
        except (OSError, RuntimeError, ValueError):
            return None
        directory = root / safe_filename(artist) / safe_filename(title)
        directories = [directory]
        if album_id is not None:
            alternate = directory.with_name(f"{safe_filename(title)} [{album_id}]")
            directories.append(alternate)
            try:
                if self._existing_album_marker(root, alternate) == album_id:
                    directories.reverse()  # Match _album_directory's established alternate preference.
            except (OSError, RuntimeError, ValueError):
                pass
        candidates = [path / filenames[0] if kind == "track" else path for path in directories]
        if known_path is not None:
            known = Path(known_path)
            candidates.insert(0, known if known.is_absolute() else root / known)
        for candidate in dict.fromkeys(candidates):
            try:
                self._contained_path(root, candidate)
                folder = candidate.parent if kind == "track" else candidate
                if not folder.is_dir():
                    continue
                marker = self._existing_album_marker(root, folder)
                if marker is not None and marker != album_id:
                    continue
                marked = marker is not None
                if kind == "track":
                    identified = candidate.stem.endswith(f"[{item_id}]") or (marked and candidate.name == filenames[0])
                    if self._valid_existing_audio(root, candidate, quality, track, album, require_metadata=not identified):
                        return candidate
                else:
                    if not marked and {path.name for path in folder.iterdir() if path.suffix.lower() in {".flac", ".mp3"}} != set(filenames):
                        continue
                    if tracks and all(
                        self._valid_existing_audio(root, folder / filename, quality, track, album, require_metadata=not marked)
                        for filename, track in zip(filenames, tracks)
                    ):
                        return folder
            except (OSError, RuntimeError, ValueError):
                continue
        return None

    def register_existing_purchase(self, item: dict[str, str], download_dir: str | Path, path: str | Path) -> None:
        """Mark a verified album for future repair; standalone tracks need no marker.

        The caller must first validate path with find_existing_purchase and must not
        register during a dry run. This only writes an exclusive ownership marker,
        never creates directories or changes existing metadata/audio.
        """
        if item["kind"] == "track":
            return
        if item["kind"] != "album":
            raise ValueError("item kind must be 'album' or 'track'")
        album_id = self._safe_id(item["id"])
        root = Path(download_dir).resolve()
        directory = Path(path)
        directory = self._contained_path(root, directory if directory.is_absolute() else root / directory)
        if not directory.is_dir():
            raise QobuzError(f"Cannot register missing album directory: {directory}")
        marker = self._contained_path(root, directory / ".qobuz-album-id")
        try:
            with marker.open("x", encoding="ascii") as handle:
                handle.write(album_id)
        except FileExistsError:
            if self._existing_album_marker(root, directory) != album_id:
                raise QobuzError(f"Album directory is already owned by another album: {album_id}")

    def _existing_album_marker(self, root: Path, directory: Path) -> str | None:
        marker = self._contained_path(root, directory / ".qobuz-album-id")
        try:
            descriptor = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise QobuzError(f"Cannot read album ownership marker: {marker}") from exc
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise QobuzError(f"Album ownership marker is not a regular file: {marker}")
        with os.fdopen(descriptor, encoding="ascii") as handle:
            return self._safe_id(handle.read().strip())

    def _valid_existing_audio(
        self, root: Path, path: Path, quality: int, track: dict[str, Any], album: dict[str, Any],
        *, require_metadata: bool = False,
    ) -> bool:
        import mutagen
        from mutagen.flac import FLAC
        from mutagen.mp3 import MP3

        try:
            self._contained_path(root, path)
            if path.suffix != (".mp3" if quality == 5 else ".flac") or not path.is_file() or path.stat().st_size == 0:
                return False
            audio = mutagen.File(path, easy=True)
            if not isinstance(audio, MP3 if quality == 5 else FLAC):
                return False
            info = audio.info
            if not math.isfinite(info.length) or info.length <= 0 or info.sample_rate <= 0 or info.channels <= 0 or info.bitrate <= 0:
                return False
            if isinstance(audio, MP3) and (info.layer != 3 or info.sketchy):
                return False
            try:
                duration = float(track.get("duration"))
            except (TypeError, ValueError):
                duration = 0
            # Qobuz durations are rounded seconds; allow encoder padding and small discrepancies.
            if math.isfinite(duration) and duration > 0 and abs(info.length - duration) > max(2.0, duration * 0.02):
                return False
            if require_metadata:
                performer = track.get("performer", {})
                album_artist = album.get("artist", {})
                artist = (performer.get("name") if isinstance(performer, dict) else None) or (
                    album_artist.get("name") if isinstance(album_artist, dict) else None
                )
                expected = {"title": track.get("title") or track.get("name"),
                            "album": album.get("title") or album.get("name"), "artist": artist}
                if any(not value or audio.get(key) != [str(value)] for key, value in expected.items()):
                    return False
            return True
        except (OSError, RuntimeError, ValueError, mutagen.MutagenError):
            return False

    def download_owned_item(
        self,
        item: dict[str, str],
        download_dir: str | Path,
        quality: int,
        *,
        include_extras: bool = True,
        skip_existing: bool = False,
        known_path: str | Path | None = None,
        progress_callback: Callable[[int, int, str], None] | None = None,
        track_progress_callback: Callable[[str, str, str, str, int, int, str], None] | None = None,
    ) -> Path:
        """Download an owned album or track into a safe local folder.

        This is intentionally conservative: it only downloads items that came from the
        authenticated user's purchase list. Album downloads iterate the album's track
        metadata and download each track into one album folder.
        With skip_existing, valid destination audio is reused in the selected owned
        folder; extras, tagging, and terminal downloaded callbacks still run.
        known_path selects a matching marked album directory or an identified track
        file without claiming its folder. Relative paths are beneath download_dir.
        """
        kind = item["kind"]
        item_id = self._safe_id(item["id"])
        root = Path(download_dir).resolve()
        extension = ".mp3" if quality == 5 else ".flac"
        known_directory = None
        if known_path is not None:
            known = Path(known_path)
            known = self._contained_path(root, known if known.is_absolute() else root / known)
            known_directory = known.parent if kind == "track" else known
        if kind == "track":
            track = self.get_track(item_id)
            album = track.get("album", {}) if isinstance(track.get("album"), dict) else {}
            title = self._track_title_only(track)
            album_id = self._safe_id(album["id"]) if album.get("id") is not None else None
            artist_dir = self._contained_path(root, root / safe_filename(self._track_artist_name(track, album)))
            destination = None
            if known_path is not None and known.suffix in {".flac", ".mp3"}:
                try:
                    marker = self._existing_album_marker(root, known.parent)
                    candidate = self._contained_path(root, known.with_suffix(extension))
                    identified = known.stem.endswith(f"[{item_id}]")
                    if (marker is None or marker == album_id) and (not known.exists() or known.is_file()):
                        source_matches = identified or self._valid_existing_audio(
                            root, known, 5 if known.suffix == ".mp3" else 6, track, album, require_metadata=True,
                        )
                        # A quality change must not overwrite an unrelated same-stem file.
                        target_matches = identified or candidate == known or not candidate.exists() or self._valid_existing_audio(
                            root, candidate, quality, track, album, require_metadata=True,
                        )
                        if source_matches and target_matches and (not candidate.exists() or candidate.is_file()):
                            destination = candidate
                except (OSError, RuntimeError, ValueError):
                    pass
            if destination is None:
                album_dir = self._album_directory(root, artist_dir, self._album_title_only(album) if album else "Singles", album_id,
                                                  known_directory=known_directory)
                destination = self._contained_path(root, album_dir / f"{safe_filename(title)} [{item_id}]{extension}")
                marker = self._existing_album_marker(root, album_dir)
                if marker is not None and marker != album_id:
                    raise QobuzError(f"Album directory is already owned by another album: {album_id}")
            if progress_callback:
                progress_callback(1, 1, title)
            if track_progress_callback:
                track_progress_callback(kind, item_id, title, str(destination), 0, 0, "downloading")
            track_byte_progress = None
            if track_progress_callback:
                track_byte_progress = lambda downloaded, total: track_progress_callback(
                    kind, item_id, title, str(destination), downloaded, total, "downloading"
                )
            if skip_existing and self._valid_existing_audio(root, destination, quality, track, album):
                path = destination
            elif track_byte_progress:
                path = self.download_track_file(item_id, destination, quality, progress_callback=track_byte_progress)
            else:
                path = self.download_track_file(item_id, destination, quality)
            if track_progress_callback:
                total_size = path.stat().st_size if path.exists() else 0
                track_progress_callback(kind, item_id, title, str(path), total_size, total_size, "downloaded")
            cover_paths = self.download_album_extras(album, path.parent) if include_extras else []
            if include_extras:
                self.download_artist_poster(track, artist_dir)
            self.embed_track_metadata(track, path, album=album, cover_path=self._first_cover(cover_paths))
            return path
        if kind == "album":
            album = self.get_album(item_id)
            album_title = self._album_title_only(album)
            artist_dir = self._contained_path(root, root / safe_filename(self._album_artist_name(album)))
            album_dir = self._album_directory(root, artist_dir, album_title, item_id, known_directory=known_directory)
            tracks_block = album.get("tracks", {}) if isinstance(album.get("tracks"), dict) else {}
            tracks = tracks_block.get("items", []) if isinstance(tracks_block, dict) else []
            if not tracks:
                raise QobuzError(f"Album {item_id} did not include track metadata")
            cover_paths = self.download_album_extras(album, album_dir) if include_extras else []
            if include_extras:
                self.download_artist_poster(album, artist_dir)
            cover_path = self._first_cover(cover_paths)
            last_path = album_dir
            total_tracks = len(tracks)
            for index, track in enumerate(tracks, start=1):
                track_id = self._safe_id(track.get("id"))
                title = self._track_title_only(track)
                if progress_callback:
                    progress_callback(index, total_tracks, title)
                # A single album-wide index stays unique across discs and preserves legacy paths.
                destination = self._contained_path(root, album_dir / f"{index:02d}. {safe_filename(title)}{extension}")
                if track_progress_callback:
                    track_progress_callback("track", track_id, title, str(destination), 0, 0, "downloading")
                track_byte_progress = None
                if track_progress_callback:
                    track_byte_progress = lambda downloaded, total, track_id=track_id, title=title, destination=destination: track_progress_callback(
                        "track", track_id, title, str(destination), downloaded, total, "downloading"
                    )
                if skip_existing and self._valid_existing_audio(root, destination, quality, track, album):
                    last_path = destination
                elif track_byte_progress:
                    last_path = self.download_track_file(track_id, destination, quality, progress_callback=track_byte_progress)
                else:
                    last_path = self.download_track_file(track_id, destination, quality)
                if track_progress_callback:
                    total_size = Path(last_path).stat().st_size if Path(last_path).exists() else 0
                    track_progress_callback("track", track_id, title, str(last_path), total_size, total_size, "downloaded")
                self.embed_track_metadata(track, last_path, album=album, cover_path=cover_path, track_number=index, track_total=total_tracks)
            return Path(last_path).parent
        raise ValueError("item kind must be 'album' or 'track'")

    def download_owned_item_extras(self, item: dict[str, str], downloaded_path: str | Path) -> list[Path]:
        """Download missing cover/goodie files for an already-recorded purchase."""
        kind = item["kind"]
        item_id = self._safe_id(item["id"])
        path = Path(downloaded_path)
        if kind == "track":
            self._contained_path(path.parent, path)
            track = self.get_track(item_id)
            written = self.download_album_extras(track.get("album", {}), path.parent)
            if path.exists():
                self.embed_track_metadata(track, path, album=track.get("album", {}), cover_path=self._first_cover(written))
            return written
        if kind == "album":
            album = self.get_album(item_id)
            written = self.download_album_extras(album, path)
            cover_path = self._first_cover(written)
            tracks_block = album.get("tracks", {}) if isinstance(album.get("tracks"), dict) else {}
            tracks = tracks_block.get("items", []) if isinstance(tracks_block, dict) else []
            total_tracks = len(tracks)
            for index, track in enumerate(tracks, start=1):
                for extension in (".flac", ".mp3"):
                    audio_path = self._contained_path(path, path / f"{index:02d}. {safe_filename(self._track_title_only(track))}{extension}")
                    if audio_path.is_file():
                        self.embed_track_metadata(track, audio_path, album=album, cover_path=cover_path, track_number=index, track_total=total_tracks)
            return written
        raise ValueError("item kind must be 'album' or 'track'")

    def download_album_extras(self, album: Any, album_dir: Path) -> list[Path]:
        """Download cover art and Qobuz album goodies into an album/track folder."""
        if not isinstance(album, dict):
            return []
        written: list[Path] = []
        image_obj = album.get("image")
        image: dict[str, Any] = image_obj if isinstance(image_obj, dict) else {}
        cover_url = image.get("large") or image.get("small") or image.get("thumbnail")
        if cover_url:
            suffix = Path(str(cover_url).split("?", 1)[0]).suffix or ".jpg"
            cover = self._download_extra(str(cover_url), album_dir, f"cover{safe_filename(suffix)}")
            if cover:
                written.append(cover)
        for index, goodie in enumerate(album.get("goodies") or [], start=1):
            if not isinstance(goodie, dict):
                continue
            url = goodie.get("url") or goodie.get("file") or goodie.get("download_url")
            if not url:
                continue
            title = safe_filename(str(goodie.get("description") or goodie.get("name") or goodie.get("title") or f"extra-{index}"))
            suffix = Path(str(url).split("?", 1)[0]).suffix or ".bin"
            extra = self._download_extra(str(url), album_dir, f"{index:02d}. {title}{safe_filename(suffix)[:20]}")
            if extra:
                written.append(extra)
        return written

    def _download_extra(self, url: str, directory: Path, filename: str) -> Path | None:
        destination = self._contained_path(directory, directory / filename)
        try:
            if destination.is_file() and destination.stat().st_size > 0:
                return destination
            return self.download_url_file(url, destination)
        except Exception as exc:
            LOGGER.warning("Could not download optional extra %s: %s", destination, type(exc).__name__)
            return None

    def download_artist_poster(self, source: Any, artist_dir: Path) -> Path | None:
        """Save the artist poster/image in the artist folder when Qobuz metadata exposes one."""
        if not isinstance(source, dict):
            return None
        for path in sorted(artist_dir.glob("artist-poster.*")):
            self._contained_path(artist_dir, path)
            if path.is_file() and path.stat().st_size > 0:
                return path
        artist = self._artist_dict(source)
        artist_payload = artist
        artist_id = artist.get("id") or artist.get("artist_id")
        if artist_id:
            try:
                fetched_artist = self.get_artist(str(artist_id))
                if isinstance(fetched_artist, dict):
                    artist_payload = {**artist, **fetched_artist}
            except Exception as exc:  # pragma: no cover - poster is best-effort and must not break sync
                LOGGER.debug("Could not fetch Qobuz artist poster metadata for %s: %s", artist_id, type(exc).__name__)
        poster_url = self._image_url(artist_payload)
        if not poster_url:
            return None
        suffix = Path(str(poster_url).split("?", 1)[0]).suffix or ".jpg"
        return self._download_extra(str(poster_url), artist_dir, f"artist-poster{safe_filename(suffix)}")


    def embed_track_metadata(
        self,
        track: dict[str, Any],
        path: Path,
        *,
        album: Any | None = None,
        cover_path: Path | None = None,
        track_number: int | None = None,
        track_total: int | None = None,
    ) -> None:
        """Embed FLAC/ID3 tags, avoiding writes when metadata and art already match."""
        try:
            from mutagen.flac import FLAC, Picture
            from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK
        except Exception as exc:  # pragma: no cover - dependency/import environment issue
            LOGGER.warning("Skipping metadata embedding because mutagen is unavailable: %s", type(exc).__name__)
            return

        try:
            path = Path(path)
            album_dict = album if isinstance(album, dict) else {}
            performer = track.get("performer", {}).get("name") if isinstance(track.get("performer"), dict) else None
            album_artist = album_dict.get("artist", {}).get("name") if isinstance(album_dict.get("artist"), dict) else None
            artist = performer or album_artist
            title = track.get("title") or track.get("name")
            album_title = album_dict.get("title") or album_dict.get("name")
            release_date = album_dict.get("release_date_original") or album_dict.get("released_at") or album_dict.get("release_date_download")
            if isinstance(release_date, (int, float)):
                release_date = datetime.fromtimestamp(release_date, timezone.utc).date().isoformat()
            number = track_number or track.get("track_number") or track.get("number")
            disc = track.get("media_number") or track.get("disc_number")
            cover_data = cover_path.read_bytes() if cover_path and cover_path.is_file() else None
            mime = "image/jpeg" if cover_path and cover_path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
            changed = False
            if path.suffix.lower() == ".mp3":
                try:
                    audio = ID3(path)
                except ID3NoHeaderError:
                    audio = ID3()
                values = [
                    (TIT2, title), (TPE1, artist), (TPE2, album_artist), (TALB, album_title),
                    (TDRC, str(release_date)[:10] if release_date else None),
                    (TRCK, f"{number}/{track_total}" if number and track_total else number),
                    (TPOS, disc),
                ]
                for frame, value in values:
                    if value and str(audio.get(frame.__name__, "")) != str(value):
                        audio.add(frame(encoding=3, text=[str(value)]))
                        changed = True
                if cover_data and not any(p.type == 3 and p.data == cover_data and p.mime == mime for p in audio.getall("APIC")):
                    audio.delall("APIC")
                    audio.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover_data))
                    changed = True
                if changed:
                    self._save_metadata(audio, path)
                return
            if path.suffix.lower() != ".flac":
                return
            audio = FLAC(path)
            values = {
                "TITLE": title, "ARTIST": artist, "ALBUMARTIST": album_artist, "ALBUM": album_title,
                "DATE": str(release_date)[:10] if release_date else None,
                "TRACKNUMBER": number, "TRACKTOTAL": track_total, "DISCNUMBER": disc,
            }
            for key, value in values.items():
                if value and audio.get(key) != [str(value)]:
                    audio[key] = str(value)
                    changed = True
            if cover_data and not any(p.type == 3 and p.data == cover_data and p.mime == mime for p in audio.pictures):
                picture = Picture()
                picture.type = 3
                picture.mime = mime
                picture.desc = "Cover"
                picture.data = cover_data
                audio.clear_pictures()
                audio.add_picture(picture)
                changed = True
            if changed:
                self._save_metadata(audio, path)
        except Exception as exc:  # pragma: no cover - corrupted/unsupported files should not break sync
            LOGGER.warning("Could not embed metadata into %s: %s", path, type(exc).__name__)

    @staticmethod
    def _save_metadata(audio: Any, path: Path) -> None:
        """Mutagen can modify a file before raising; only ever save into a sibling copy."""
        temporary: Path | None = None
        try:
            with path.open("rb") as source, tempfile.NamedTemporaryFile(mode="w+b", dir=path.parent, prefix=".qobuz-tags-", suffix=".part", delete=False) as handle:
                temporary = Path(handle.name)
                shutil.copyfileobj(source, handle)
                handle.flush()
                handle.seek(0)
                audio.save(handle)
                handle.flush()
                os.fchmod(handle.fileno(), os.fstat(source.fileno()).st_mode & 0o777)
                os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _first_cover(paths: list[Path]) -> Path | None:
        for path in paths:
            if path.name.startswith("cover"):
                return path
        return None

    @classmethod
    def _artist_dict(cls, source: dict[str, Any]) -> dict[str, Any]:
        for key in ("artist", "performer"):
            value = source.get(key)
            if isinstance(value, dict):
                return value
        album = source.get("album")
        if isinstance(album, dict):
            artist = album.get("artist")
            if isinstance(artist, dict):
                return artist
        return {}

    @staticmethod
    def _image_url(source: dict[str, Any]) -> str:
        image_obj = source.get("image") or source.get("picture") or source.get("photo") or source.get("avatar")
        if isinstance(image_obj, dict):
            for key in ("mega", "large", "extralarge", "xxlarge", "original", "small", "thumbnail"):
                if image_obj.get(key):
                    return str(image_obj[key])
        if isinstance(image_obj, str):
            return image_obj
        for key in ("picture", "photo", "avatar", "image"):
            value = source.get(key)
            if isinstance(value, str):
                return value
        return ""

    def download_track_file(self, track_id: str, destination: Path, quality: int, *, progress_callback: Callable[[int, int], None] | None = None) -> Path:
        """Download one owned track URL to a destination path.

        This method intentionally does not infer metadata or filenames. The sync service is responsible
        for choosing a safe path after reading track/album metadata.
        """
        url = self.get_track_file_url(track_id, quality)
        return self._download_file(url, destination, progress_callback=progress_callback, expected_format="mp3" if quality == 5 else "flac")

    def download_url_file(self, url: str, destination: Path) -> Path:
        return self._download_file(url, destination)

    def _download_file(self, url: str, destination: Path, *, progress_callback: Callable[[int, int], None] | None = None, expected_format: Literal["flac", "mp3"] | None = None) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            # None removes session Qobuz headers before media requests and their redirects.
            headers = {"X-User-Auth-Token": None, "X-App-Id": None, "Authorization": None, "Accept-Encoding": "identity"}
            with self.session.get(url, stream=True, timeout=self.timeout, headers=headers) as response:
                response.raise_for_status()
                if getattr(response, "status_code", 200) == 206 or "content-range" in response.headers:
                    raise QobuzError("Unexpected partial media response")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise QobuzError("Unexpected content encoding for media download")
                length = response.headers.get("content-length")
                try:
                    total = int(length) if length is not None else 0
                    if total < 0:
                        raise ValueError("negative length")
                except (TypeError, ValueError) as exc:
                    raise QobuzError("Invalid media Content-Length") from exc
                downloaded = 0
                with tempfile.NamedTemporaryFile(mode="wb", dir=destination.parent, prefix=".qobuz-", suffix=".part", delete=False) as handle:
                    temporary = Path(handle.name)
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            handle.write(chunk)
                            downloaded += len(chunk)
                            if progress_callback:
                                progress_callback(downloaded, total)
                    if downloaded == 0 or (length is not None and downloaded != total):
                        raise QobuzError(f"Incomplete media download: received {downloaded} bytes, expected {length or 'nonzero'}")
                    handle.flush()
                    # Keep existing archive permissions; new media must also be readable by library services.
                    os.fchmod(handle.fileno(), destination.stat().st_mode & 0o777 if destination.exists() else 0o644)
                    os.fsync(handle.fileno())
            if expected_format:
                # Basic format identification only, not a full audio decode or integrity check.
                with temporary.open("rb") as handle:
                    signature = handle.read(4)
                if expected_format == "flac":
                    valid = signature == b"fLaC"
                else:
                    valid = len(signature) == 4 and (
                        signature.startswith(b"ID3") or (
                            signature[0] == 0xFF and signature[1] & 0xE0 == 0xE0
                            and signature[1] & 0x06 == 0x02 and signature[1] & 0x18 != 0x08
                            and signature[2] & 0xF0 != 0xF0 and signature[2] & 0x0C != 0x0C
                        )
                    )
                if not valid:
                    raise QobuzError(f"Downloaded audio does not have the expected {expected_format.upper()} signature")
            # Only publish after the stream and response have both closed successfully.
            temporary.replace(destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return destination

    def _get(self, endpoint: str, **params: Any) -> dict[str, Any]:
        headers = {"X-App-Id": self.app_id, "X-User-Auth-Token": self.user_auth_token}
        response = self.session.get(f"{self.base_url}/{endpoint}", params=params, timeout=self.timeout, headers=headers, allow_redirects=False)
        if 300 <= getattr(response, "status_code", 200) < 400:
            response.close()
            raise QobuzError(f"Refusing redirect from Qobuz API endpoint {endpoint}")
        if endpoint == "user/login" and getattr(response, "status_code", 200) == 401:
            raise AuthenticationError("Invalid Qobuz credentials")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise QobuzError(f"Unexpected Qobuz response for {endpoint}")
        return payload

    def _require_login(self) -> None:
        if not self.user_auth_token:
            raise AuthenticationError("Qobuz client is not logged in")

    @staticmethod
    def _safe_id(value: Any) -> str:
        value = str(value) if value is not None else ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value) or value == "None":
            raise QobuzError("Invalid Qobuz item ID")
        return value

    @staticmethod
    def _contained_path(root: Path, path: Path) -> Path:
        if not path.resolve().is_relative_to(root.resolve()):
            raise QobuzError(f"Download path escapes output directory: {path}")
        return path

    def _album_directory(
        self, root: Path, artist_dir: Path, title: str, album_id: Any,
        *, known_directory: str | Path | None = None,
    ) -> Path:
        if album_id is None:
            return self._contained_path(root, artist_dir / safe_filename(title))
        album_id = self._safe_id(album_id)
        known = None
        if known_directory is not None:
            known = Path(known_directory)
            known = self._contained_path(root, known if known.is_absolute() else root / known).resolve()
            if known.is_dir() and self._existing_album_marker(root, known) == album_id:
                return known
        directory = self._contained_path(root, artist_dir / safe_filename(title))
        # Reuse known archives, but never claim nonempty folders with unknown ownership.
        alternate = self._contained_path(root, artist_dir / f"{safe_filename(title)} [{album_id}]")
        if alternate.resolve() != known and self._existing_album_marker(root, alternate) == album_id:
            return alternate
        for candidate in (directory, alternate):
            if candidate.resolve() == known and candidate.exists():
                continue  # A supplied unmarked/mismatched directory is never ours to claim.
            self._contained_path(root, candidate)
            marker = self._contained_path(root, candidate / ".qobuz-album-id")
            candidate.mkdir(parents=True, exist_ok=True)
            if not marker.exists() and any(candidate.iterdir()):
                continue
            try:
                with marker.open("x", encoding="ascii") as handle:
                    handle.write(album_id)
                return candidate
            except FileExistsError:
                if self._existing_album_marker(root, candidate) == album_id:
                    return candidate
        raise QobuzError(f"Album directory is already owned by another album: {album_id}")

    @staticmethod
    def _album_title(album: dict[str, Any]) -> str:
        artist = album.get("artist", {}).get("name") if isinstance(album.get("artist"), dict) else None
        title = album.get("title") or album.get("name") or "Unknown album"
        return f"{artist} - {title}" if artist else str(title)

    @staticmethod
    def _album_title_only(album: dict[str, Any]) -> str:
        return str(album.get("title") or album.get("name") or "Unknown album")

    @staticmethod
    def _album_artist_name(album: dict[str, Any]) -> str:
        artist = album.get("artist", {}).get("name") if isinstance(album.get("artist"), dict) else None
        return str(artist or "Unknown Artist")

    @classmethod
    def _track_artist_name(cls, track: dict[str, Any], album: dict[str, Any] | None = None) -> str:
        performer = track.get("performer", {}).get("name") if isinstance(track.get("performer"), dict) else None
        album_artist = (album or {}).get("artist", {}).get("name") if isinstance((album or {}).get("artist"), dict) else None
        return str(performer or album_artist or "Unknown Artist")

    @staticmethod
    def _track_title_only(track: dict[str, Any]) -> str:
        return str(track.get("title") or track.get("name") or "Unknown track")

    @staticmethod
    def _track_title(track: dict[str, Any]) -> str:
        performer = track.get("performer", {}).get("name") if isinstance(track.get("performer"), dict) else None
        title = track.get("title") or "Unknown track"
        return f"{performer} - {title}" if performer else str(title)


def safe_filename(value: str) -> str:
    cleaned = re.sub(r'[^A-Za-z0-9._() \-\[\]]+', '_', value).strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)
    cleaned = cleaned[:160].rstrip()
    return cleaned if cleaned.strip(".") else 'untitled'
