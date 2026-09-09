from __future__ import annotations

import base64
import hashlib
import logging
import re
import time
from dataclasses import dataclass
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
            LOGGER.debug("Skipping undecodable Qobuz secret: %s", exc)
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
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) QobuzSync/0.1",
                "X-App-Id": self.app_id,
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
        self.session.headers.update({"X-User-Auth-Token": token})
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
        offset = 0
        limit = 500
        while True:
            payload = self.get_purchases(purchase_type, limit=limit, offset=offset)
            block = payload.get(purchase_type, payload)
            page_items = block.get("items", []) if isinstance(block, dict) else []
            items.extend(page_items)
            total = int(block.get("total", len(items))) if isinstance(block, dict) else len(items)
            if len(items) >= total or not page_items:
                break
            offset += limit
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
        return self._get("album/get", album_id=album_id)

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


    def download_owned_item(
        self,
        item: dict[str, str],
        download_dir: str | Path,
        quality: int,
        *,
        include_extras: bool = True,
        progress_callback: Callable[[int, int, str], None] | None = None,
        track_progress_callback: Callable[[str, str, str, str, int, int, str], None] | None = None,
    ) -> Path:
        """Download an owned album or track into a safe local folder.

        This is intentionally conservative: it only downloads items that came from the
        authenticated user's purchase list. Album downloads iterate the album's track
        metadata and download each track into one album folder.
        """
        kind = item["kind"]
        item_id = str(item["id"])
        root = Path(download_dir)
        if kind == "track":
            track = self.get_track(item_id)
            album = track.get("album", {}) if isinstance(track.get("album"), dict) else {}
            title = self._track_title_only(track)
            artist_dir = root / safe_filename(self._track_artist_name(track, album))
            album_dir = artist_dir / safe_filename(self._album_title_only(album) if album else "Singles")
            destination = album_dir / f"{safe_filename(title)} [{item_id}].flac"
            if progress_callback:
                progress_callback(1, 1, title)
            if track_progress_callback:
                track_progress_callback(kind, item_id, title, str(destination), 0, 0, "downloading")
            track_byte_progress = None
            if track_progress_callback:
                track_byte_progress = lambda downloaded, total: track_progress_callback(
                    kind, item_id, title, str(destination), downloaded, total, "downloading"
                )
            if track_byte_progress:
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
            artist_dir = root / safe_filename(self._album_artist_name(album))
            album_dir = artist_dir / safe_filename(album_title)
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
                track_id = str(track.get("id"))
                title = self._track_title_only(track)
                if progress_callback:
                    progress_callback(index, total_tracks, title)
                destination = album_dir / f"{index:02d}. {safe_filename(title)}.flac"
                if track_progress_callback:
                    track_progress_callback("track", track_id, title, str(destination), 0, 0, "downloading")
                track_byte_progress = None
                if track_progress_callback:
                    track_byte_progress = lambda downloaded, total, track_id=track_id, title=title, destination=destination: track_progress_callback(
                        "track", track_id, title, str(destination), downloaded, total, "downloading"
                    )
                if track_byte_progress:
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
        item_id = str(item["id"])
        path = Path(downloaded_path)
        if kind == "track":
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
                for flac_path in sorted(path.glob(f"{index:02d}. *.flac")):
                    self.embed_track_metadata(track, flac_path, album=album, cover_path=cover_path, track_number=index, track_total=total_tracks)
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
            written.append(self.download_url_file(str(cover_url), album_dir / f"cover{suffix}"))
        for index, goodie in enumerate(album.get("goodies") or [], start=1):
            if not isinstance(goodie, dict):
                continue
            url = goodie.get("url") or goodie.get("file") or goodie.get("download_url")
            if not url:
                continue
            title = safe_filename(str(goodie.get("description") or goodie.get("name") or goodie.get("title") or f"extra-{index}"))
            suffix = Path(str(url).split("?", 1)[0]).suffix or ".bin"
            written.append(self.download_url_file(str(url), album_dir / f"{index:02d}. {title}{suffix}"))
        return written

    def download_artist_poster(self, source: Any, artist_dir: Path) -> Path | None:
        """Save the artist poster/image in the artist folder when Qobuz metadata exposes one."""
        if not isinstance(source, dict):
            return None
        artist = self._artist_dict(source)
        artist_payload = artist
        artist_id = artist.get("id") or artist.get("artist_id")
        if artist_id:
            try:
                fetched_artist = self.get_artist(str(artist_id))
                if isinstance(fetched_artist, dict):
                    artist_payload = {**artist, **fetched_artist}
            except Exception as exc:  # pragma: no cover - poster is best-effort and must not break sync
                LOGGER.debug("Could not fetch Qobuz artist poster metadata for %s: %s", artist_id, exc)
        poster_url = self._image_url(artist_payload)
        if not poster_url:
            return None
        suffix = Path(str(poster_url).split("?", 1)[0]).suffix or ".jpg"
        return self.download_url_file(str(poster_url), artist_dir / f"artist-poster{suffix}")


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
        """Embed basic Vorbis comments and cover art into a downloaded FLAC file."""
        try:
            from mutagen.flac import FLAC, Picture
        except Exception as exc:  # pragma: no cover - dependency/import environment issue
            LOGGER.warning("Skipping FLAC metadata embedding because mutagen is unavailable: %s", exc)
            return

        try:
            audio = FLAC(path)
            album_dict = album if isinstance(album, dict) else {}
            performer = track.get("performer", {}).get("name") if isinstance(track.get("performer"), dict) else None
            album_artist = album_dict.get("artist", {}).get("name") if isinstance(album_dict.get("artist"), dict) else None
            artist = performer or album_artist
            title = track.get("title") or track.get("name")
            album_title = album_dict.get("title") or album_dict.get("name")
            release_date = album_dict.get("release_date_original") or album_dict.get("released_at") or album_dict.get("release_date_download")
            if title:
                audio["TITLE"] = str(title)
            if artist:
                audio["ARTIST"] = str(artist)
            if album_title:
                audio["ALBUM"] = str(album_title)
            if release_date:
                audio["DATE"] = str(release_date)[:10]
            number = track_number or track.get("track_number") or track.get("number")
            if number:
                audio["TRACKNUMBER"] = str(number)
            if track_total:
                audio["TRACKTOTAL"] = str(track_total)
            if cover_path and cover_path.exists():
                picture = Picture()
                picture.type = 3
                picture.mime = "image/jpeg" if cover_path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
                picture.desc = "Cover"
                picture.data = cover_path.read_bytes()
                audio.clear_pictures()
                audio.add_picture(picture)
            audio.save()
        except Exception as exc:  # pragma: no cover - corrupted/unsupported files should not break sync
            LOGGER.warning("Could not embed metadata into %s: %s", path, exc)

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
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.session.get(url, stream=True, timeout=self.timeout) as response:  # type: ignore[attr-defined]
                response.raise_for_status()
                total = int(response.headers.get("content-length") or 0)
                downloaded = 0
                with destination.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            handle.write(chunk)
                            downloaded += len(chunk)
                            if progress_callback:
                                progress_callback(downloaded, total)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return destination

    def download_url_file(self, url: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.session.get(url, stream=True, timeout=self.timeout) as response:  # type: ignore[attr-defined]
                response.raise_for_status()
                with destination.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            handle.write(chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return destination

    def _get(self, endpoint: str, **params: Any) -> dict[str, Any]:
        response = self.session.get(f"{self.base_url}/{endpoint}", params=params, timeout=self.timeout)
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
    return (cleaned or 'untitled')[:160]
