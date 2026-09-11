# Changelog

## 0.1.56

- Checks disk during normal sync before downloading unrecorded or incomplete purchases, registers complete matches, and reuses valid tracks in partially downloaded app-owned albums.
- Uses conservative filename, album-ownership, audio-format, and duration checks; preserves uncertain folders and keeps full-library resync as an explicit forced download.
- Retains Library cards through purchase completion and updates cards in place to preserve artwork nodes and scroll position.
- Keeps large albums visible during downloads and refreshes artwork when local cover files change.
- Adds disk-discovery, archive-repair, completion-handoff, and real-browser regression coverage.

## 0.1.55

- Accepts Safari form submissions with `Origin: null` when a signed, browser-bound CSRF token is valid, while retaining authentication and cross-site request protection.
- Adds form tokens to settings, login, and sync actions, with reload guidance for expired forms after an app restart.
- Bounds CSRF form parsing to 64 KiB and safely rejects malformed multipart data.
- Adds regression coverage for null-origin saves, missing or tampered tokens, login, sync, and form parsing limits.

## 0.1.54

- Fixes Save settings rejection when a reverse proxy replaces the browser-facing HTTPS scheme with HTTP, using browser same-origin fetch metadata when available.
- Handles forwarded hosts without a forwarded scheme and preserves IPv6 host syntax during origin checks.
- Retains authentication and cross-site request protection, with regression tests for empty settings, manual credentials, and pasted browser sessions.

## 0.1.53

- Fixes Qobuz Web Player browser-session paste behind Umbrel/reverse proxies by honoring forwarded origin headers.
- Accepts browser storage row copies like `localuser\t{...}` in addition to the raw `localuser` JSON value.

## 0.1.50

- Makes dashboard polling local-only, caches file metadata, and uses one non-overlapping browser polling loop with visible errors and session-expiry handling.
- Fixes repeated album downloads with file manifests, paginates complete album/purchase listings, and continues syncing after individual purchase failures.
- Uses atomic downloads and metadata updates, checks transfer sizes and basic audio signatures, and preserves existing archives during full resync.
- Adds MP3 filenames and ID3 metadata/artwork support; avoids repeated writes of matching artwork and tags.
- Serializes scheduled/manual jobs, recovers from worker failures, throttles progress writes, and closes SQLite connections explicitly.
- Isolates API credentials from media requests, fixes log redaction, tightens database permissions, and restricts artwork responses to safe image types.
- Validates settings, fixes account switching, improves mobile/keyboard feedback, and limits standalone Compose exposure to loopback by default.
- Adds backend and optional real-browser regression coverage.
- Older albums without file manifests receive a one-time verification redownload. Existing unmarked folders are preserved; replacement folders may have an album-ID suffix.

## 0.1.49

- Removes the broken Qobuz email/password login flow from the UI and sync client.
- Requires Qobuz Web Player browser-session credentials (`localuser`, or user ID plus auth token) for sync.
- Clears and ignores legacy saved password hashes so they cannot configure login.

## 0.1.48

- Removes the "running without an access token" security notice from the dashboard (the optional `QOBUZ_SYNC_AUTH_TOKEN` login still works if you set it).
- Uses a randomized internal web port (23809) for the container instead of the default 8080.

## 0.1.47

- Fixes the `/sync-now` job lock so overlapping sync/re-sync jobs cannot run concurrently (previously the lock was released before the worker started).
- Adds opt-in dashboard authentication via the `QOBUZ_SYNC_AUTH_TOKEN` environment variable (login page + HttpOnly SameSite cookie + Bearer/API-key support), with CSRF origin checks on all state-changing requests.
- Redacts Qobuz credentials from API error messages before they reach the UI, database, or logs.
- Stops committing third-party binaries: removes the `.local/` git/openssl/apk tree from the repository.
- Caches cover-art recovery so `/art/...` requests stop triggering repeated authenticated Qobuz logins.
- Reuses the logged-in Qobuz client for the live-refresh dashboard instead of logging in on every poll.
- Strips local filesystem paths from the public JSON progress API.
- Uses SQLite WAL + busy timeout to reduce `database is locked` errors under concurrent access.
- Removes partially-written FLAC/cover files if an interrupted download fails.
- Escapes the last unescaped status label and drops the unused `jinja2` dependency.

## 0.1.45

- Keeps the dashboard responsive without manual page refreshes by adding a lightweight live refresh loop.
- Refreshes progress, counters, recent downloads, last sync text, and status badge while the page is open.
- Softens the hero card corner flare so it no longer visually squares off the rounded top-right corner.

## 0.1.44

- Simplify Qobuz Web Player token instructions in Settings & status.
- Remove the Downloads path note from the authentication help block.

## 0.1.43

- Clear stale completed track progress at sync completion/error so Library cards no longer permanently show `Writing to disk`.
- Hide completed track-progress rows from the live progress payload when the master sync status is complete/idle/error.

## 0.1.42

- Fixes active progress-only track cards so they no longer render without artwork.
- Uses the active track's download path to serve cover art or the standard generated fallback artwork while the file is being written.
- Places active progress cards ahead of older recent downloads and removes the unused separate track-progress list output.

## 0.1.41

- Integrates per-track download progress directly into the corresponding Library track cards.
- Restores Settings & status to a single master downloads progress bar under Sync status.
- Removes the separate track-progress list from Settings so detailed progress follows the tracks it belongs to.

## 0.1.40

- Moves the per-track download progress list into the Settings & status tab's Sync status card.
- Replaces the old yellow overall progress bar under the All Good/status message with the track-level download progress bars.
- Keeps the Library tab focused on recent downloaded tracks only.

## 0.1.39

- Re-downloads tracks/albums when the app has a previous download record but the audio file was deleted from disk, instead of only backfilling cover art/extras.
- Adds per-track byte progress tracking and progress bars so the UI shows what is actively being written to disk.
- Prevents overlapping sync/re-sync jobs so a full re-sync cannot clear files while another download job is still running.

## 0.1.38

- Makes **Re Sync Entire Library** start in the background instead of blocking the page until the full sync finishes.
- Clears the recent downloads list/counter during full re-sync and refreshes the library panel as newly downloaded content is recorded again.
- Adds live progress polling for the full re-sync flow so the progress card and downloads panel update while content is being downloaded.

## 0.1.37

- Shows the recent changelog directly in the Umbrel App Store description so it remains visible even when umbrelOS does not surface manifest release notes for an already-installed app.
- Keeps the manifest `releaseNotes` field current for the update flow.

## 0.1.36

- Patches security hardening findings from automated screening.
- Stops storing the plaintext Qobuz password after settings are saved.
- Stops rendering saved passwords or auth tokens back into the settings form.
- Tightens full-library resync cleanup to only the configured Qobuz Sync downloads directory.
- Annotates intentional Qobuz MD5 protocol/signature use and Umbrel container interface binding for security scanners.

## 0.1.35

- Adds a versioned What's New section for the Umbrel App Store using the app manifest release notes.
- Starts tracking user-visible release history in this changelog so future Umbrel updates can keep release notes current.

## 0.1.34

- Updates the App Store and dashboard tagline to mention Umbrel Downloads/QobuzSync, artwork, metadata, and Navidrome.

## 0.1.33

- Adds the red **Re Sync Entire Library** action.
- Clears previous downloaded content and download records before re-downloading the full Qobuz library.
- Adds matching AJAX check-mark confirmation behavior for the full re-sync action.

## 0.1.32

- Makes **Sync now** run through AJAX without refreshing the whole UI.
- Adds a fading check mark once the sync request is accepted.

## 0.1.31

- Removes remaining archive topbar styling and source remnants from the dashboard.

## 0.1.30

- Switches Umbrel App Store screenshots to PNG assets.
- Removes old screenshot assets.
- Updates music storage layout to `Artist/Album/Tracks`.
- Saves artist posters in the artist folder when Qobuz provides artwork.
