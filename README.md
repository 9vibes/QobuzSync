# Qobuz Sync

Self-hosted web app for archiving music purchased from your Qobuz account.

Qobuz Sync downloads purchased albums/tracks, artwork, extras, and metadata to:

```text
/Home/Downloads/QobuzSync
```

## Features

- Finds purchased Qobuz music.
- Downloads missing or newly purchased tracks.
- Re-downloads audio if files were deleted from disk.
- Includes a web UI for login, sync, progress, and recent downloads.
- Publishes a home-screen widget for Umbrel.

## Install on Umbrel

Qobuz Sync is packaged in the KUNAS Umbrel store:

```text
https://github.com/9vibes/KNS-Umbrel
```

Add that store in umbrelOS, then install **Qobuz Sync**.

This repo is only the app source and container build. Umbrel app-store files live in [`9vibes/KNS-Umbrel`](https://github.com/9vibes/KNS-Umbrel).

## Qobuz login

Qobuz Sync uses your existing Qobuz Web Player browser session. Email/password API login is not supported because Qobuz can block it with captcha or web-login protections.

1. Log in to the Qobuz Web Player.
2. Open Developer Tools / Inspect Element, then choose Console.
3. Run `copy(localStorage.getItem('localuser'))`.
4. Paste that value into **Paste Qobuz browser session** in Qobuz Sync and save settings.

Manual fallback: open Storage / Local Storage, find `localuser`, then copy `token` and `id` into the separate token fields.

## Archive safety

- **Sync now** checks disk before downloading unrecorded or incomplete purchases. Complete matching purchases are registered without downloading their audio again, and valid tracks in partially downloaded app-owned albums are reused. Scheduled sync uses the same checks.
- Discovery checks QobuzSync's artist/album layout, album-ID-suffixed folders, and previously recorded paths. It does not fuzzy-match arbitrary renamed files. Unmarked legacy albums need a complete filename set and matching embedded title, album, and artist tags; uncertain folders remain untouched.
- Registering a verified album adds an album-ID marker so future repairs reuse its folder. The disk check itself is read-only, and dry runs never register purchases or write markers. Unmarked folders containing extra audio in another format are not adopted.
- Existing audio must match the requested MP3/FLAC format and have readable stream information and a compatible duration when Qobuz provides one. This is not a full decode/checksum check and does not distinguish FLAC quality tiers. Complete albums get a file manifest so later syncs detect deleted files.
- **Re Sync Entire Library** redownloads the selected purchases without first deleting the library. Each file is replaced only after its transfer succeeds and its basic audio signature is checked. This is not a full audio integrity/decode check.
- Dry runs only discover purchases; they never create completed-download records or replace audio.
- Artwork and metadata repair runs during sync, not while viewing the dashboard. Missing local artwork gets a placeholder until the next repair.
- Keep backups. Atomic per-file replacement does not make a whole multi-track album transactionally replaceable, and changed metadata or quality can leave older files alongside new ones.

## Deployment

The standalone Compose file binds to `127.0.0.1:23809` by default. Before exposing it to your network, configure `QOBUZ_SYNC_AUTH_TOKEN` or put it behind an authenticating reverse proxy. Without a token, the app intentionally trusts the Umbrel proxy and does not authenticate direct clients.

Run one application process per library. Scheduled and manual jobs share a process-local lock; multiple workers or containers must not write to the same library. Configure forwarded headers only for trusted reverse proxies so HTTPS cookies and same-origin checks use the external scheme and host.

## Development

Run tests:

```bash
uv run --extra dev python -m pytest
```

Run locally:

```bash
QOBUZ_SYNC_DATA_DIR=./data WEB_HOST=127.0.0.1 QOBUZ_SYNC_BACKGROUND=1 uv run python -m qobuz_sync
```

Or with Docker:

```bash
docker compose up --build
```

Browser regression tests are optional and skip when Playwright or Chromium is unavailable. Install Python Playwright and Chromium to exercise polling, keyboard navigation, and 320px/375px/1280px layouts. On Alpine, use the system Node runtime with `PLAYWRIGHT_NODEJS_PATH=/usr/bin/node`.

Container image:

```text
ghcr.io/9vibes/qobuzsync
```

## Legal

Qobuz Sync is not affiliated with Qobuz. Use it only to archive music purchased from your own account.
