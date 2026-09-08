import hashlib

from qobuz_sync.qobuz_client import QobuzClient, md5_password


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.calls = []
        self.responses = []

    def queue(self, payload, status_code=200):
        self.responses.append(FakeResponse(payload, status_code))

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params or {}, "timeout": timeout, "headers": dict(self.headers)})
        if not self.responses:
            raise AssertionError("no fake response queued")
        return self.responses.pop(0)


def test_md5_password_hashes_plaintext_for_qobuz_login():
    assert md5_password("correct horse") == hashlib.md5(b"correct horse").hexdigest()


def test_login_stores_user_auth_token_and_membership_label():
    session = FakeSession()
    session.queue({
        "user_auth_token": "uat-123",
        "user": {"credential": {"parameters": {"short_label": "Sublime"}}},
    })
    client = QobuzClient("app-1", session=session)

    result = client.login("me@example.com", "already-md5")

    assert result.membership == "Sublime"
    assert client.user_auth_token == "uat-123"
    assert session.headers["X-App-Id"] == "app-1"
    assert session.headers["X-User-Auth-Token"] == "uat-123"
    assert session.calls[0]["url"].endswith("/user/login")
    assert session.calls[0]["params"] == {
        "email": "me@example.com",
        "password": "already-md5",
        "app_id": "app-1",
    }


def test_login_can_use_existing_user_auth_token():
    session = FakeSession()
    session.queue({
        "user_auth_token": "uat-refreshed",
        "user": {"credential": {"parameters": {"short_label": "Studio"}}},
    })
    client = QobuzClient("app-1", session=session)

    result = client.login(user_id="123", user_auth_token="uat-existing")

    assert result.membership == "Studio"
    assert client.user_auth_token == "uat-refreshed"
    assert session.headers["X-User-Auth-Token"] == "uat-refreshed"
    assert session.calls[0]["params"] == {
        "user_id": "123",
        "user_auth_token": "uat-existing",
        "app_id": "app-1",
    }


def test_login_accepts_purchased_music_accounts_without_subscription_credentials():
    session = FakeSession()
    session.queue({
        "user_auth_token": "uat-123",
        "user": {"credential": {"parameters": {}}},
    })
    client = QobuzClient("app-1", session=session)

    result = client.login("me@example.com", "already-md5")

    assert result.membership == "Purchased music account"
    assert client.user_auth_token == "uat-123"


def test_get_purchase_ids_calls_private_qobuz_purchase_ids_endpoint():
    session = FakeSession()
    session.queue({"albums": [111], "tracks": [222]})
    client = QobuzClient("app-1", session=session)
    client.user_auth_token = "uat-123"
    session.headers["X-User-Auth-Token"] = "uat-123"

    result = client.get_purchase_ids()

    assert result == {"albums": [111], "tracks": [222]}
    assert session.calls[0]["url"].endswith("/purchase/getUserPurchasesIds")
    assert session.calls[0]["params"] == {"user_auth_token": "uat-123"}


def test_get_purchases_requests_type_limit_and_offset():
    session = FakeSession()
    session.queue({"albums": {"items": [{"id": "abc"}], "total": 1}})
    client = QobuzClient("app-1", session=session)
    client.user_auth_token = "uat-123"

    result = client.get_purchases("albums", limit=50, offset=100)

    assert result["albums"]["items"] == [{"id": "abc"}]
    assert session.calls[0]["url"].endswith("/purchase/getUserPurchases")
    assert session.calls[0]["params"] == {
        "user_auth_token": "uat-123",
        "type": "albums",
        "limit": 50,
        "offset": 100,
    }


def test_get_track_file_url_uses_stream_intent_and_tries_each_secret():
    session = FakeSession()
    session.queue({"status": "error", "message": "bad secret"}, status_code=400)
    session.queue({"url": "https://cdn.example/track.flac"})
    client = QobuzClient("app-1", secrets=("bad-secret", "good-secret"), session=session)
    client.user_auth_token = "uat-123"

    result = client.get_track_file_url("track-1", 6)

    assert result == "https://cdn.example/track.flac"
    assert client.active_secret == "good-secret"
    assert len(session.calls) == 2
    assert session.calls[0]["params"]["intent"] == "stream"
    assert session.calls[1]["params"]["intent"] == "stream"
    assert session.calls[1]["params"]["format_id"] == 6


def test_track_download_filename_includes_track_id_to_avoid_title_collisions(tmp_path):
    session = FakeSession()
    session.queue({"title": "Same Song", "performer": {"name": "Same Artist"}})
    client = QobuzClient("app-1", secrets=("secret",), session=session)
    captured = {}

    def fake_download_track_file(track_id, destination, quality):
        captured["track_id"] = track_id
        captured["destination"] = destination
        captured["quality"] = quality
        return destination

    client.download_track_file = fake_download_track_file

    result = client.download_owned_item({"kind": "track", "id": "track-123", "title": "Same Artist - Same Song"}, tmp_path, 6)

    assert result == tmp_path / "Same Artist" / "Singles" / "Same Song [track-123].flac"
    assert captured["track_id"] == "track-123"


def test_track_download_saves_album_cover_by_default(tmp_path):
    session = FakeSession()
    session.queue({
        "title": "Same Song",
        "performer": {"name": "Same Artist"},
        "album": {"title": "Album One", "artist": {"name": "Album Artist"}, "image": {"large": "https://static.example/cover_600.jpg"}},
    })
    client = QobuzClient("app-1", secrets=("secret",), session=session)
    written = []

    def fake_download_track_file(track_id, destination, quality):
        return destination

    def fake_download_url_file(url, destination):
        written.append((url, destination))
        return destination

    client.download_track_file = fake_download_track_file
    client.download_url_file = fake_download_url_file
    embedded = []
    client.embed_track_metadata = lambda track, path, **kwargs: embedded.append((track, path, kwargs))

    client.download_owned_item({"kind": "track", "id": "track-123", "title": "Same Artist - Same Song"}, tmp_path, 6)

    assert written == [("https://static.example/cover_600.jpg", tmp_path / "Same Artist" / "Album One" / "cover.jpg")]
    assert embedded[0][1] == tmp_path / "Same Artist" / "Album One" / "Same Song [track-123].flac"
    assert embedded[0][2]["cover_path"] == tmp_path / "Same Artist" / "Album One" / "cover.jpg"


def test_album_download_saves_cover_and_goodies_by_default(tmp_path):
    session = FakeSession()
    session.queue({
        "title": "Album One",
        "artist": {"name": "Artist"},
        "image": {"large": "https://static.example/album.jpg"},
        "goodies": [{"description": "Digital booklet", "url": "https://static.example/booklet.pdf"}],
        "tracks": {"items": [{"id": "1", "title": "Intro"}]},
    })
    client = QobuzClient("app-1", secrets=("secret",), session=session)
    written = []

    destinations = []

    def fake_download_track_file(track_id, destination, quality):
        destinations.append(destination)
        return destination

    client.download_track_file = fake_download_track_file
    progress = []
    embedded = []
    client.embed_track_metadata = lambda track, path, **kwargs: embedded.append((track, path, kwargs))

    def fake_download_url_file(url, destination):
        written.append((url, destination))
        return destination

    client.download_url_file = fake_download_url_file

    client.download_owned_item(
        {"kind": "album", "id": "album-1", "title": "Artist - Album One"},
        tmp_path,
        6,
        progress_callback=lambda current, total, title: progress.append((current, total, title)),
    )

    assert written == [
        ("https://static.example/album.jpg", tmp_path / "Artist" / "Album One" / "cover.jpg"),
        ("https://static.example/booklet.pdf", tmp_path / "Artist" / "Album One" / "01. Digital booklet.pdf"),
    ]
    assert destinations == [tmp_path / "Artist" / "Album One" / "01. Intro.flac"]
    assert progress == [(1, 1, "Intro")]
    assert embedded[0][2]["track_number"] == 1
    assert embedded[0][2]["track_total"] == 1


def test_track_extra_backfill_skips_metadata_when_legacy_audio_path_is_missing(tmp_path):
    session = FakeSession()
    session.queue({
        "title": "Same Song",
        "performer": {"name": "Same Artist"},
        "album": {"image": {"large": "https://static.example/cover_600.jpg"}},
    })
    client = QobuzClient("app-1", secrets=("secret",), session=session)
    written = []
    embedded = []

    def fake_download_url_file(url, destination):
        written.append((url, destination))
        return destination

    client.download_url_file = fake_download_url_file
    client.embed_track_metadata = lambda track, path, **kwargs: embedded.append((track, path, kwargs))

    missing_path = tmp_path / "Same Artist - Same Song" / "old-filename.flac"
    client.download_owned_item_extras({"kind": "track", "id": "track-123", "title": "Same Artist - Same Song"}, missing_path)

    assert written == [("https://static.example/cover_600.jpg", tmp_path / "Same Artist - Same Song" / "cover.jpg")]
    assert embedded == []


def test_album_download_saves_artist_poster_in_artist_folder(tmp_path):
    session = FakeSession()
    session.queue({
        "title": "Album One",
        "artist": {"id": 42, "name": "Artist"},
        "image": {"large": "https://static.example/album.jpg"},
        "tracks": {"items": [{"id": "1", "title": "Intro"}]},
    })
    session.queue({"image": {"large": "https://static.example/artist.jpg"}})
    client = QobuzClient("app-1", secrets=("secret",), session=session)
    written = []

    client.download_track_file = lambda track_id, destination, quality: destination
    client.embed_track_metadata = lambda *args, **kwargs: None

    def fake_download_url_file(url, destination):
        written.append((url, destination))
        return destination

    client.download_url_file = fake_download_url_file

    client.download_owned_item({"kind": "album", "id": "album-1", "title": "Artist - Album One"}, tmp_path, 6)

    assert ("https://static.example/artist.jpg", tmp_path / "Artist" / "artist-poster.jpg") in written


def test_download_track_file_reports_byte_progress(tmp_path):
    class FakeDownloadResponse:
        headers = {"content-length": "9"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            yield b"abc"
            yield b"defghi"

    class DownloadSession(FakeSession):
        def get(self, url, params=None, timeout=None, **kwargs):
            if url == "https://cdn.example/track.flac":
                return FakeDownloadResponse()
            return super().get(url, params=params, timeout=timeout, **kwargs)

    session = DownloadSession()
    session.queue({"url": "https://cdn.example/track.flac"})
    client = QobuzClient("app-1", secrets=("secret",), session=session)
    progress = []

    result = client.download_track_file("track-1", tmp_path / "song.flac", 6, progress_callback=lambda done, total: progress.append((done, total)))

    assert result == tmp_path / "song.flac"
    assert result.read_bytes() == b"abcdefghi"
    assert progress == [(3, 9), (9, 9)]
