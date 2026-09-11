import io
from pathlib import Path

import pytest
import requests

from qobuz_sync.qobuz_client import AuthenticationError, QobuzClient, QobuzError, safe_filename


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload

    def close(self):
        pass


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.calls = []
        self.responses = []

    def queue(self, payload, status_code=200):
        self.responses.append(FakeResponse(payload, status_code))

    def get(self, url, params=None, timeout=None, headers=None, **kwargs):
        merged_headers = {key: value for key, value in {**self.headers, **(headers or {})}.items() if value is not None}
        self.calls.append({"url": url, "params": params or {}, "timeout": timeout, "headers": merged_headers, **kwargs})
        if not self.responses:
            raise AssertionError("no fake response queued")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_login_stores_user_auth_token_and_membership_label():
    session = FakeSession()
    session.queue({
        "user_auth_token": "uat-123",
        "user": {"credential": {"parameters": {"short_label": "Sublime"}}},
    })
    client = QobuzClient("app-1", session=session)

    result = client.login(user_id="123", user_auth_token="uat-existing")

    assert result.membership == "Sublime"
    assert client.user_auth_token == "uat-123"
    assert "X-App-Id" not in session.headers
    assert "X-User-Auth-Token" not in session.headers
    assert session.calls[0]["headers"]["X-App-Id"] == "app-1"
    assert session.calls[0]["url"].endswith("/user/login")
    assert session.calls[0]["params"] == {
        "user_id": "123",
        "user_auth_token": "uat-existing",
        "app_id": "app-1",
    }


def test_login_rejects_missing_browser_session_credentials():
    client = QobuzClient("app-1", session=FakeSession())

    with pytest.raises(AuthenticationError, match="user ID and auth token are required"):
        client.login()


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
    assert "X-User-Auth-Token" not in session.headers
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

    result = client.login(user_id="123", user_auth_token="uat-existing")

    assert result.membership == "Purchased music account"
    assert client.user_auth_token == "uat-123"


def test_get_purchase_ids_calls_private_qobuz_purchase_ids_endpoint():
    session = FakeSession()
    session.queue({"albums": [111], "tracks": [222]})
    client = QobuzClient("app-1", session=session)
    client.user_auth_token = "uat-123"

    result = client.get_purchase_ids()

    assert result == {"albums": [111], "tracks": [222]}
    assert session.calls[0]["url"].endswith("/purchase/getUserPurchasesIds")
    assert session.calls[0]["params"] == {"user_auth_token": "uat-123"}
    assert session.calls[0]["headers"]["X-User-Auth-Token"] == "uat-123"


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


@pytest.mark.parametrize("purchase_type", ["albums", "tracks"])
@pytest.mark.parametrize("wrapped", [True, False])
def test_purchase_pagination_advances_by_returned_count(purchase_type, wrapped):
    session = FakeSession()
    for offset, ids in [(0, [1, 2]), (2, [3]), (3, [4, 5])]:
        block = {"items": [{"id": item_id} for item_id in ids], "total": 5, "offset": offset}
        session.queue({purchase_type: block} if wrapped else block)
    client = QobuzClient("app", session=session)
    client.user_auth_token = "token"
    assert [item["id"] for item in client.iter_purchases(purchase_type)] == [1, 2, 3, 4, 5]
    assert [call["params"]["offset"] for call in session.calls] == [0, 2, 3]


def test_purchase_pagination_accepts_empty_account():
    session = FakeSession()
    session.queue({"albums": {"items": [], "total": 0}})
    client = QobuzClient("app", session=session)
    client.user_auth_token = "token"
    assert client.iter_purchases("albums") == []


@pytest.mark.parametrize("block", [
    None, [], {}, {"items": {}, "total": 0}, {"items": []},
    *({"items": [], "total": total} for total in [None, -1, "bad", 1.5, True]),
    {"items": [], "total": 1},
    {"items": [{"id": 1}], "total": 0},
    {"items": [{"id": 1}], "total": 1, "offset": 1},
    {"items": [{"id": 1}, {"id": "1"}], "total": 2},
    {"items": [{}], "total": 1}, {"items": [None], "total": 1},
])
def test_purchase_pagination_rejects_malformed_first_page(block):
    session = FakeSession()
    session.queue({"albums": block})
    client = QobuzClient("app", session=session)
    client.user_auth_token = "token"
    with pytest.raises(QobuzError):
        client.iter_purchases("albums")


@pytest.mark.parametrize("block", [
    {"items": [], "total": 2},
    {"items": [{"id": "1"}], "total": 2},
    {"items": [{"id": 2}], "total": 1},
    {"items": [{"id": 2}], "total": 3},
    {"items": [{"id": 2}]},
    {"items": [{"id": 2}], "total": 2, "offset": 500},
    {"items": [{"id": 2}, {"id": 3}], "total": 2},
])
def test_purchase_pagination_never_returns_partial_or_inconsistent_listing(block):
    session = FakeSession()
    session.queue({"albums": {"items": [{"id": 1}], "total": 2}})
    session.queue({"albums": block})
    client = QobuzClient("app", session=session)
    client.user_auth_token = "token"
    with pytest.raises(QobuzError):
        client.iter_purchases("albums")
    assert [call["params"]["offset"] for call in session.calls] == [0, 1]


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
    assert session.calls[1]["headers"]["X-User-Auth-Token"] == "uat-123"


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
            yield b"fLa"
            yield b"Caudio"

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
    assert result.read_bytes() == b"fLaCaudio"
    assert progress == [(3, 9), (9, 9)]


class StreamResponse(FakeResponse):
    def __init__(self, chunks=(b"fLaCaudio",), length="9", status_code=200, close_error=False):
        super().__init__({}, status_code)
        self.headers = {} if length is None else {"content-length": length}
        self.chunks = chunks
        self.close_error = close_error

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.close_error:
            raise OSError("response close failed")

    def iter_content(self, chunk_size):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


@pytest.mark.parametrize("audio", [True, False])
@pytest.mark.parametrize("existing", [True, False])
@pytest.mark.parametrize("failure", ["network", "http", "partial", "stream", "empty", "truncated", "oversized", "invalid-length", "negative-length", "zero-length", "close"])
def test_failed_download_never_changes_destination(tmp_path, audio, existing, failure):
    destination = tmp_path / "song.flac"
    if existing:
        destination.write_bytes(b"old good audio")
    responses = {
        "network": requests.ConnectionError("offline"),
        "http": StreamResponse(status_code=503),
        "partial": StreamResponse(status_code=206),
        "stream": StreamResponse(chunks=(b"partial", requests.ConnectionError("lost connection"))),
        "empty": StreamResponse(chunks=(), length=None),
        "truncated": StreamResponse(length="10"),
        "oversized": StreamResponse(length="8"),
        "invalid-length": StreamResponse(length="bad"),
        "negative-length": StreamResponse(length="-1"),
        "zero-length": StreamResponse(length="0"),
        "close": StreamResponse(close_error=True),
    }
    session = FakeSession()
    session.responses.append(responses[failure])
    client = QobuzClient("app", session=session)
    client.get_track_file_url = lambda *args: "https://cdn.example/audio"

    with pytest.raises((QobuzError, requests.RequestException, RuntimeError, OSError)):
        if audio:
            client.download_track_file("1", destination, 6)
        else:
            client.download_url_file("https://cdn.example/extra", destination)

    assert destination.exists() == existing
    if existing:
        assert destination.read_bytes() == b"old good audio"
    assert list(tmp_path.iterdir()) == ([destination] if existing else [])


@pytest.mark.parametrize("failure", ["url", "callback", "fsync", "replace"])
def test_download_preserves_destination_on_non_network_failures(tmp_path, monkeypatch, failure):
    destination = tmp_path / "song.flac"
    destination.write_bytes(b"old")
    session = FakeSession()
    session.responses.append(StreamResponse())
    client = QobuzClient("app", session=session)
    client.get_track_file_url = lambda *args: "https://cdn.example/audio"

    def fail(*args, **kwargs):
        raise OSError("failure")

    if failure == "url":
        client.get_track_file_url = fail
    elif failure == "fsync":
        monkeypatch.setattr("qobuz_sync.qobuz_client.os.fsync", fail)
    elif failure == "replace":
        monkeypatch.setattr(type(destination), "replace", fail)
    with pytest.raises(OSError):
        client.download_track_file("1", destination, 6, progress_callback=fail if failure == "callback" else None)
    assert destination.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("length", ["9", None])
def test_download_publishes_only_complete_sibling_temp_file(tmp_path, length):
    destination = tmp_path / ("a" * 240 + ".flac")
    destination.write_bytes(b"old")
    destination.chmod(0o640)
    session = FakeSession()
    session.responses.append(StreamResponse(chunks=(b"fLaC", b"audio"), length=length))
    client = QobuzClient("app", session=session)
    client.get_track_file_url = lambda *args: "https://cdn.example/audio"
    progress = []

    def on_progress(done, total):
        assert destination.read_bytes() == b"old"
        assert len(list(tmp_path.glob(".qobuz-*.part"))) == 1
        progress.append((done, total))

    client.download_track_file("1", destination, 6, progress_callback=on_progress)
    assert destination.read_bytes() == b"fLaCaudio"
    assert destination.stat().st_mode & 0o777 == 0o640
    assert progress == [(4, int(length or 0)), (9, int(length or 0))]
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("quality,body", [
    (6, b"fLaCaudio"), (7, b"fLaCaudio"), (27, b"fLaCaudio"),
    (5, b"ID3\x04\x00\x00\x00\x00\x00\x00audio"),
    (5, b"\xff\xfb\x90\x00audio"), (5, b"\xff\xf3\x90\x00audio"),
])
def test_audio_download_accepts_expected_signature_across_chunks(tmp_path, quality, body):
    session = FakeSession()
    session.responses.append(StreamResponse(chunks=(body[:1], b"", body[1:]), length=str(len(body))))
    client = QobuzClient("app", session=session)
    client.get_track_file_url = lambda *args: "https://cdn.example/audio"
    destination = tmp_path / "audio.bin"
    assert client.download_track_file("1", destination, quality).read_bytes() == body
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("quality,body", [
    (6, b"<html>HTTP 200 error</html>"), (5, b"<html>HTTP 200 error</html>"),
    (6, b"ID3\x04audio"), (5, b"fLaCaudio"),
    (6, b"\x00" * 16), (5, b"\x00" * 16),
    (6, b"fLa"), (5, b"\xff\xfb"),
    (5, b"\xff\xff\xff\xff"), (5, b"\xff\xf1\x50\x80aac"),
])
def test_http_200_invalid_audio_never_replaces_destination(tmp_path, existing, quality, body):
    destination = tmp_path / "audio.bin"
    if existing:
        destination.write_bytes(b"old good archive")
        original_stat = destination.stat()
    session = FakeSession()
    session.responses.append(StreamResponse(chunks=(body,), length=str(len(body))))
    client = QobuzClient("app", session=session)
    client.get_track_file_url = lambda *args: "https://cdn.example/audio"

    with pytest.raises(QobuzError, match="expected .* signature"):
        client.download_track_file("1", destination, quality)

    if existing:
        assert destination.read_bytes() == b"old good archive"
        assert destination.stat().st_ino == original_stat.st_ino
        assert destination.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert list(tmp_path.iterdir()) == ([destination] if existing else [])


def test_extras_do_not_require_audio_signatures_or_https(tmp_path):
    body = b"<html>Album notes</html>"
    session = FakeSession()
    session.responses.append(StreamResponse(chunks=(body,), length=str(len(body))))
    client = QobuzClient("app", session=session)
    destination = tmp_path / "notes.html"
    assert client.download_url_file("http://cdn.example/notes.html", destination).read_bytes() == body


def test_api_redirects_are_not_followed():
    session = FakeSession()
    session.queue({}, status_code=302)
    client = QobuzClient("app", session=session)
    with pytest.raises(QobuzError, match="Refusing redirect"):
        client.login(user_id="123", user_auth_token="secret")
    assert len(session.calls) == 1
    assert session.calls[0]["allow_redirects"] is False


@pytest.mark.parametrize("api_request", [False, True])
def test_api_and_media_redirects_do_not_leak_qobuz_credentials(tmp_path, api_request):
    class RecordingAdapter(requests.adapters.BaseAdapter):
        def __init__(self):
            self.calls = []

        def send(self, request, **kwargs):
            self.calls.append(request)
            response = requests.Response()
            response.request = request
            response.url = request.url
            response.status_code = 302 if len(self.calls) == 1 else 200
            response.headers = requests.structures.CaseInsensitiveDict(
                {"Location": "https://external.example/audio"} if len(self.calls) == 1 else {"Content-Length": "5"}
            )
            response.raw = io.BytesIO(b"" if len(self.calls) == 1 else b"audio")
            return response

        def close(self):
            pass

    session = requests.Session()
    session.trust_env = False
    adapter = RecordingAdapter()
    session.mount("https://", adapter)
    client = QobuzClient("app", session=session)
    client.user_auth_token = "secret"
    session.headers.update({"X-User-Auth-Token": "stale-secret", "X-App-Id": "app"})
    if api_request:
        with pytest.raises(QobuzError, match="Refusing redirect"):
            client.get_artist("1")
        assert len(adapter.calls) == 1
        assert adapter.calls[0].headers["X-User-Auth-Token"] == "secret"
        assert adapter.calls[0].url.startswith(client.base_url)
        return
    client.download_url_file("https://cdn.example/audio", tmp_path / "audio.flac")
    assert len(adapter.calls) == 2
    for request in adapter.calls:
        assert "X-User-Auth-Token" not in request.headers
        assert "X-App-Id" not in request.headers
        assert "secret" not in request.url


def test_album_fetches_and_verifies_every_track_page():
    session = FakeSession()
    session.queue({"tracks_count": 3, "tracks": {"items": [{"id": "1"}], "total": 3}})
    session.queue({"tracks": {"items": [{"id": "2"}, {"id": "3"}], "total": 3, "offset": 1}})
    album = QobuzClient("app", session=session).get_album("album")
    assert [track["id"] for track in album["tracks"]["items"]] == ["1", "2", "3"]
    assert [call["params"]["offset"] for call in session.calls] == [0, 1]


@pytest.mark.parametrize("second_page", [
    {}, {"tracks": {"items": []}},
    {"tracks": {"items": [{"id": "1"}]}},
    {"tracks": {"items": [{"title": "Missing ID"}]}},
    {"tracks": {"items": [{"id": "2"}], "total": 3}},
    {"tracks": {"items": [{"id": "2"}], "offset": 0}},
    {"tracks": {"items": [{"id": "2"}, {"id": "3"}]}},
])
def test_incomplete_or_inconsistent_album_does_not_start_downloads(tmp_path, second_page):
    session = FakeSession()
    session.queue({"tracks": {"items": [{"id": "1"}], "total": 2}})
    session.queue(second_page)
    client = QobuzClient("app", session=session)
    downloaded = []
    client.download_track_file = lambda *args, **kwargs: downloaded.append(args)
    with pytest.raises(QobuzError):
        client.download_owned_item({"kind": "album", "id": "album"}, tmp_path, 6)
    assert downloaded == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("tracks_count,total", [(3, 2), (2, "bad"), (2, None), (0, -1)])
def test_album_rejects_invalid_totals(tracks_count, total):
    session = FakeSession()
    session.queue({"tracks_count": tracks_count, "tracks": {"items": [{"id": "1"}], "total": total}})
    with pytest.raises(QobuzError):
        QobuzClient("app", session=session).get_album("album")


@pytest.mark.parametrize("value", [".", "..", " ... ", "", " " * 10])
def test_safe_filename_rejects_dot_components(value):
    assert safe_filename(value) == "untitled"


def test_safe_filename_preserves_valid_legacy_names():
    assert safe_filename("Artist... (Live) [2025]") == "Artist... (Live) [2025]"
    assert safe_filename("Album...") == "Album..."


@pytest.mark.parametrize("item_id", ["../escape", "a/b", "a\\b", ".", "..", "", None, "x" * 81])
def test_download_rejects_unsafe_ids_before_api_or_file_access(tmp_path, item_id):
    session = FakeSession()
    client = QobuzClient("app", session=session)
    with pytest.raises(QobuzError, match="Invalid Qobuz item ID"):
        client.download_owned_item({"kind": "track", "id": item_id}, tmp_path, 6)
    assert session.calls == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("component", ["artist", "album", "audio"])
def test_download_rejects_symlinks_escaping_output_root(tmp_path, component):
    root = tmp_path / "downloads"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if component == "artist":
        (root / "Artist").symlink_to(outside, target_is_directory=True)
    else:
        artist = root / "Artist"
        artist.mkdir()
        if component == "album":
            (artist / "Singles").symlink_to(outside, target_is_directory=True)
        else:
            album = artist / "Singles"
            album.mkdir()
            (album / "Song [1].flac").symlink_to(outside / "audio.flac")
    session = FakeSession()
    session.queue({"title": "Song", "performer": {"name": "Artist"}})
    with pytest.raises(QobuzError, match="escapes output"):
        QobuzClient("app", session=session).download_owned_item({"kind": "track", "id": "1"}, root, 6)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("marked", [False, True])
def test_same_title_albums_preserve_legacy_audio_and_only_reuse_known_directories(tmp_path, marked):
    session = FakeSession()
    for _ in range(4):
        session.queue({"title": "Same Album", "artist": {"name": "Artist"}, "tracks": {"items": [{"id": "1", "title": "Song"}]}})
    legacy = tmp_path / "Artist" / "Same Album"
    legacy.mkdir(parents=True)
    (legacy / "booklet.pdf").write_bytes(b"legacy extra")
    (legacy / "01. Song.flac").write_bytes(b"legacy audio")
    if marked:
        (legacy / ".qobuz-album-id").write_text("first", encoding="ascii")
    client = QobuzClient("app", session=session)
    client.download_track_file = lambda track_id, destination, quality: destination
    client.embed_track_metadata = lambda *args, **kwargs: None
    first = client.download_owned_item({"kind": "album", "id": "first"}, tmp_path, 6, include_extras=False)
    second = client.download_owned_item({"kind": "album", "id": "second"}, tmp_path, 6, include_extras=False)
    repeated = QobuzClient("app", session=session)
    repeated.download_track_file = client.download_track_file
    repeated.embed_track_metadata = client.embed_track_metadata
    assert repeated.download_owned_item({"kind": "album", "id": "second"}, tmp_path, 6, include_extras=False) == second
    assert first == (legacy if marked else tmp_path / "Artist" / "Same Album [first]")
    assert second == tmp_path / "Artist" / "Same Album [second]"
    assert (legacy / "booklet.pdf").read_bytes() == b"legacy extra"
    assert (legacy / "01. Song.flac").read_bytes() == b"legacy audio"
    assert (legacy / ".qobuz-album-id").exists() == marked
    legacy.rename(tmp_path / "moved-legacy")
    assert repeated.download_owned_item({"kind": "album", "id": "second"}, tmp_path, 6, include_extras=False) == second


def test_album_directory_refuses_unmarked_nonempty_id_specific_directory(tmp_path):
    for name in ("Album", "Album [1]"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "song.flac").write_bytes(b"unknown audio")
    client = QobuzClient("app", session=FakeSession())
    with pytest.raises(QobuzError):
        client._album_directory(tmp_path, tmp_path, "Album", "1")
    for name in ("Album", "Album [1]"):
        assert list((tmp_path / name).iterdir()) == [tmp_path / name / "song.flac"]
        assert (tmp_path / name / "song.flac").read_bytes() == b"unknown audio"


@pytest.mark.parametrize("kind", ["track", "album"])
def test_mp3_quality_uses_mp3_extension(tmp_path, kind):
    session = FakeSession()
    session.queue({"title": "Title", "tracks": {"items": [{"id": "1", "title": "Song"}]}})
    client = QobuzClient("app", session=session)
    destinations = []

    def download(track_id, destination, quality):
        destinations.append(destination)
        return destination

    client.download_track_file = download
    client.embed_track_metadata = lambda *args, **kwargs: None
    client.download_owned_item({"kind": kind, "id": "1"}, tmp_path, 5, include_extras=False)
    assert len(destinations) == 1
    assert destinations[0].suffix == ".mp3"


def test_artwork_http_errors_do_not_prevent_album_audio_or_other_extras(tmp_path):
    session = FakeSession()
    session.queue({
        "title": "Album", "artist": {"name": "Artist", "image": "https://cdn.example/poster.jpg"},
        "image": {"large": "https://cdn.example/cover.jpg"},
        "goodies": [{"url": "https://cdn.example/booklet.pdf"}],
        "tracks": {"items": [{"id": "1", "title": "Song"}]},
    })
    client = QobuzClient("app", session=session)
    extras = []
    audio = []

    def download_extra(url, destination):
        extras.append(destination.name)
        raise requests.HTTPError("HTTP 404")

    def download_audio(track_id, destination, quality):
        audio.append(track_id)
        return destination

    client.download_url_file = download_extra
    client.download_track_file = download_audio
    client.embed_track_metadata = lambda *args, **kwargs: None
    client.download_owned_item({"kind": "album", "id": "1"}, tmp_path, 6)
    assert audio == ["1"]
    assert extras == ["cover.jpg", "01. extra-1.pdf", "artist-poster.jpg"]


def test_existing_nonempty_extras_and_poster_are_not_downloaded_again(tmp_path):
    for name in ("cover.jpg", "01. Booklet.pdf", "artist-poster.jpg"):
        (tmp_path / name).write_bytes(b"existing")
    client = QobuzClient("app", session=FakeSession())
    album = {"artist": {"id": "42"}, "image": {"large": "https://cdn.example/cover.jpg"}, "goodies": [{"name": "Booklet", "url": "https://cdn.example/booklet.pdf"}]}
    assert client.download_album_extras(album, tmp_path) == [tmp_path / "cover.jpg", tmp_path / "01. Booklet.pdf"]
    assert client.download_artist_poster(album, tmp_path) == tmp_path / "artist-poster.jpg"
    assert client.session.calls == []


@pytest.fixture(params=[".flac", ".mp3"])
def audio_file(tmp_path, request):
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3

    path = tmp_path / f"01. Song [Live]{request.param}"
    if request.param == ".flac":
        # Minimal STREAMINFO container; no decoder is needed to exercise real Mutagen tagging.
        streaminfo = b"\x10\x00\x10\x00" + b"\x00" * 6
        streaminfo += ((44100 << 44) | (1 << 41) | (15 << 36) | 44100).to_bytes(8, "big") + b"\x00" * 16
        path.write_bytes(b"fLaC\x80\x00\x00\x22" + streaminfo)
        tag_class = FLAC
    else:
        path.write_bytes(b"audio payload")
        tag_class = ID3
    path.chmod(0o640)
    return path, tag_class


@pytest.mark.parametrize("release", [{"release_date_original": "2025-01-02"}, {"released_at": 1735776000}])
def test_metadata_and_cover_backfill_is_idempotent(tmp_path, monkeypatch, audio_file, release):
    path, tag_class = audio_file
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"cover image")
    album = {
        "title": "Album", "artist": {"name": "Album Artist"}, **release,
        "image": {"large": "https://cdn.example/cover.jpg"},
        "tracks": {"items": [{"id": "1", "title": "Song [Live]", "performer": {"name": "Artist"}, "media_number": 2}]},
    }
    saves = []
    original_save = tag_class.save
    expected_bytes = path.read_bytes()

    def save(self, handle, **kwargs):
        assert Path(handle.name) != path
        assert Path(handle.name).parent == path.parent
        assert path.read_bytes() == expected_bytes
        saves.append(1)
        result = original_save(self, handle, **kwargs)
        assert path.read_bytes() == expected_bytes
        return result

    monkeypatch.setattr(tag_class, "save", save)
    session = FakeSession()
    session.queue(album)
    session.queue(album)
    session.queue(album)
    client = QobuzClient("app", session=session)
    item = {"kind": "album", "id": "album"}
    client.download_owned_item_extras(item, tmp_path)
    first_bytes = path.read_bytes()
    expected_bytes = first_bytes
    client.download_owned_item_extras(item, tmp_path)
    assert saves == [1]
    assert path.read_bytes() == first_bytes
    tags = tag_class(path)
    if path.suffix == ".flac":
        assert tags["TITLE"] == ["Song [Live]"]
        assert tags["ARTIST"] == ["Artist"]
        assert tags["ALBUMARTIST"] == ["Album Artist"]
        assert tags["DISCNUMBER"] == ["2"]
        assert tags["DATE"] == ["2025-01-02"]
        assert tags.pictures[0].data == b"cover image"
    else:
        assert str(tags["TIT2"]) == "Song [Live]"
        assert str(tags["TPE1"]) == "Artist"
        assert str(tags["TPE2"]) == "Album Artist"
        assert str(tags["TRCK"]) == "1/1"
        assert str(tags["TPOS"]) == "2"
        assert str(tags["TDRC"]) == "2025-01-02"
        assert tags.getall("APIC")[0].data == b"cover image"
    cover.write_bytes(b"replacement image")
    client.download_owned_item_extras(item, tmp_path)
    assert saves == [1, 1]
    assert path.stat().st_mode & 0o777 == 0o640
    assert not list(tmp_path.glob(".qobuz-tags-*.part"))


@pytest.mark.parametrize("failure", ["copy", "save", "fsync", "replace"])
def test_failed_metadata_backfill_preserves_original_and_does_not_log_secrets(tmp_path, monkeypatch, caplog, audio_file, failure):
    path, tag_class = audio_file
    original = path.read_bytes()
    original_stat = path.stat()
    calls = []

    def fail(*args, **kwargs):
        calls.append(failure)
        if failure in {"copy", "save"}:
            handle = args[1]
            handle.seek(0)
            handle.truncate()
            handle.write(b"partial corrupted file")
            handle.flush()
        raise OSError("https://example.test/?user_auth_token=secret-token&request_sig=secret-signature")

    if failure == "copy":
        monkeypatch.setattr("qobuz_sync.qobuz_client.shutil.copyfileobj", fail)
    elif failure == "save":
        monkeypatch.setattr(tag_class, "save", fail)
    elif failure == "fsync":
        monkeypatch.setattr("qobuz_sync.qobuz_client.os.fsync", fail)
    else:
        monkeypatch.setattr(type(path), "replace", fail)
    session = FakeSession()
    session.queue({"title": "Updated Song", "album": {"title": "Album"}})
    client = QobuzClient("app", session=session)
    client.download_owned_item_extras({"kind": "track", "id": "1"}, path)

    assert calls == [failure]
    assert path.read_bytes() == original
    assert path.stat().st_ino == original_stat.st_ino
    assert path.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert path.stat().st_mode == original_stat.st_mode
    assert list(tmp_path.iterdir()) == [path]
    assert "Could not embed metadata" in caplog.text
    assert "secret-token" not in caplog.text
    assert "secret-signature" not in caplog.text
