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

## Development

Run tests:

```bash
uv run pytest
```

Run locally:

```bash
QOBUZ_SYNC_DATA_DIR=./data QOBUZ_SYNC_BACKGROUND=1 uv run python -m qobuz_sync
```

Or with Docker:

```bash
docker compose up --build
```

Container image:

```text
ghcr.io/9vibes/qobuzsync
```

## Legal

Qobuz Sync is not affiliated with Qobuz. Use it only to archive music purchased from your own account.
