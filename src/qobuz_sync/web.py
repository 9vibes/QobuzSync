from __future__ import annotations

import json
import mimetypes
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from .qobuz_client import md5_password
from .state import AppConfig, DEFAULT_DOWNLOAD_DIR, SyncState
from .sync import SyncService


SYNC_JOB_LOCK = threading.Lock()


def data_dir() -> Path:
    return Path(os.environ.get("QOBUZ_SYNC_DATA_DIR", str(Path.cwd() / "data")))


def create_app() -> FastAPI:
    state = SyncState(data_dir() / "qobuz-sync.db")
    stop_event = threading.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker: threading.Thread | None = None
        if os.environ.get("QOBUZ_SYNC_BACKGROUND", "0") == "1":
            worker = threading.Thread(target=run_background_sync, args=(state, stop_event), daemon=True)
            worker.start()
        try:
            yield
        finally:
            stop_event.set()
            if worker:
                worker.join(timeout=2)

    app = FastAPI(title="Qobuz Sync", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> str:
        config = state.load_config()
        latest = state.latest_sync()
        downloads = enrich_downloads(state.list_downloads(limit=20), state=state)
        total_downloads = state.count_downloads()
        progress = state.latest_progress()
        track_progress = build_track_progress_rows(state)
        return render_home(config, latest, downloads, total_downloads=total_downloads, progress=progress, track_progress=track_progress)

    @app.get("/art/{kind}/{purchase_id}")
    def download_art(kind: str, purchase_id: str):
        path = state.downloaded_path(kind, purchase_id)
        if not path:
            raise HTTPException(status_code=404)
        download_path = Path(path)
        art = first_cover_for_path(download_path)
        if art is None:
            embedded = embedded_art_for_path(download_path)
            if embedded is not None:
                data, media_type = embedded
                return Response(data, media_type=media_type)
        if art is None:
            art = recover_cover_from_qobuz(state, kind, purchase_id, download_path)
        if art is None:
            title = download_title(state, kind, purchase_id)
            return Response(fallback_cover_svg(title or purchase_id, kind), media_type="image/svg+xml")
        return FileResponse(art, media_type=mimetypes.guess_type(art.name)[0] or "image/jpeg")

    @app.get("/progress-art/{purchase_id}")
    def progress_art(purchase_id: str):
        for row in state.list_track_progress(limit=200):
            if str(row.get("purchase_id")) != str(purchase_id):
                continue
            path = Path(str(row.get("path") or ""))
            art = first_cover_for_path(path)
            if art is None:
                embedded = embedded_art_for_path(path)
                if embedded is not None:
                    data, media_type = embedded
                    return Response(data, media_type=media_type)
            if art is not None:
                return FileResponse(art, media_type=mimetypes.guess_type(art.name)[0] or "image/jpeg")
            return Response(fallback_cover_svg(str(row.get("title") or purchase_id), "track"), media_type="image/svg+xml")
        raise HTTPException(status_code=404)

    @app.get("/api/progress")
    def api_progress() -> dict[str, object]:
        return progress_payload(state)

    @app.get("/widget")
    def widget() -> dict[str, object]:
        latest = state.latest_sync()
        progress = state.latest_progress()
        total = state.count_downloads()
        status = "All Good!" if latest and latest.get("success") else (progress or {}).get("message") or "Not synced yet"
        return {
            "text": str(status),
            "items": [
                {"title": "Status", "text": str(status)},
                {"title": "Downloaded", "text": str(total)},
                {"title": "Progress", "text": progress_text(progress)},
            ],
        }

    @app.post("/settings")
    def save_settings(
        qobuz_email: str = Form(""),
        qobuz_password: str = Form(""),
        qobuz_localuser: str = Form(""),
        qobuz_user_id: str = Form(""),
        qobuz_user_auth_token: str = Form(""),
        quality: int = Form(6),
        interval_minutes: int = Form(60),
        embed_art: str | None = Form(None),
        include_albums: str | None = Form(None),
        include_tracks: str | None = Form(None),
    ) -> RedirectResponse:
        current = state.load_config()
        pasted_user_id, pasted_auth_token, pasted_email = parse_qobuz_localuser(qobuz_localuser)
        email = qobuz_email.strip() or current.qobuz_email
        if not email and pasted_email:
            email = pasted_email
        password = qobuz_password.strip() or current.qobuz_password
        password_md5 = md5_password(password) if password else current.qobuz_password_md5
        user_id = qobuz_user_id.strip() or pasted_user_id or current.qobuz_user_id
        auth_token = qobuz_user_auth_token.strip() or pasted_auth_token or current.qobuz_user_auth_token
        state.save_config(
            AppConfig(
                qobuz_email=email,
                qobuz_password=password,
                qobuz_password_md5=password_md5,
                qobuz_user_id=user_id,
                qobuz_user_auth_token=auth_token,
                download_dir=DEFAULT_DOWNLOAD_DIR,
                quality=quality,
                interval_minutes=interval_minutes,
                embed_art=embed_art == "on",
                include_albums=include_albums == "on",
                include_tracks=include_tracks == "on",
            )
        )
        return RedirectResponse("/", status_code=303)

    @app.post("/sync-now")
    def sync_now(request: Request):
        def run_sync() -> None:
            try:
                SyncService(state).sync_once()
            finally:
                SYNC_JOB_LOCK.release()

        if not SYNC_JOB_LOCK.acquire(blocking=False):
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse({"started": False, "message": "A sync is already running"}, status_code=409)
            return RedirectResponse("/", status_code=303)
        SYNC_JOB_LOCK.release()
        threading.Thread(target=run_sync, daemon=True).start()
        if "application/json" in request.headers.get("accept", ""):
            return {"started": True, "message": "Sync started"}
        return RedirectResponse("/", status_code=303)

    @app.post("/resync-all")
    def resync_all(request: Request):
        def run_resync() -> None:
            try:
                SyncService(state).resync_entire_library()
            finally:
                SYNC_JOB_LOCK.release()

        if not SYNC_JOB_LOCK.acquire(blocking=False):
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse({"started": False, "message": "A sync is already running"}, status_code=409)
            return RedirectResponse("/", status_code=303)
        threading.Thread(target=run_resync, daemon=True).start()
        if "application/json" in request.headers.get("accept", ""):
            return {"started": True, "message": "Full library re-sync started"}
        return RedirectResponse("/", status_code=303)

    return app


def parse_qobuz_localuser(raw_value: str) -> tuple[str, str, str]:
    """Extract useful login values from Qobuz Web Player's localuser blob.

    Users can paste the full JSON value returned by
    ``localStorage.getItem('localuser')`` instead of manually hunting for and
    copying separate ``id`` and ``token`` fields. Invalid or incomplete paste
    values are ignored so the legacy fields continue to work.
    """
    value = str(raw_value or "").strip()
    if not value:
        return "", "", ""
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return "", "", ""
    if not isinstance(payload, dict):
        return "", "", ""
    user_id = payload.get("id") or payload.get("user_id")
    token = payload.get("token") or payload.get("user_auth_token")
    email = payload.get("email") or payload.get("login")
    return str(user_id or "").strip(), str(token or "").strip(), str(email or "").strip()


def run_background_sync(state: SyncState, stop_event: threading.Event) -> None:
    """Run sync periodically while the web process is alive."""
    # Delay first run a little so the web UI becomes reachable immediately after container start.
    stop_event.wait(10)
    while not stop_event.is_set():
        config = state.load_config()
        if config.is_configured:
            SyncService(state).sync_once()
        interval_seconds = max(5, state.load_config().interval_minutes * 60)
        stop_event.wait(interval_seconds)


def render_home(
    config: AppConfig,
    latest: dict | None,
    downloads: list[dict],
    *,
    total_downloads: int | None = None,
    progress: dict | None = None,
    track_progress: list[dict] | None = None,
) -> str:
    configured = "Configured" if config.is_configured else "Not configured"
    status_class = "ready" if config.is_configured else "needs-setup"
    latest_time = "Never synced"
    latest_status = "Not synced yet."
    latest_class = "muted"
    found = "—"
    downloaded = escape(total_downloads if total_downloads is not None else len(downloads))
    if latest:
        ok = "Successful" if latest["success"] else "Failed"
        latest_class = "success" if latest["success"] else "error"
        latest_time = escape(latest["finished_at"])
        latest_status = "All Good!" if latest["success"] else f"{ok}: {escape(latest['message'])}"
        found = escape(latest["found"])
        if total_downloads is None:
            downloaded = escape(latest["downloaded"])
    recent_downloads = downloads[:20]
    track_progress_rows = track_progress or []
    download_rows = render_download_rows(recent_downloads, track_progress=track_progress_rows)
    track_progress_rows = track_progress or []
    progress_label = progress_text(progress)
    progress_message = escape((progress or {}).get("message", "Idle"))
    progress_phase = escape(str((progress or {}).get("phase", "idle")).replace("_", " ").title())
    progress_current = int((progress or {}).get("current") or 0)
    progress_total = int((progress or {}).get("total") or 0)
    progress_percent = 100 if (progress or {}).get("phase") == "complete" else 0
    if progress_total:
        progress_percent = max(0, min(100, round((progress_current / progress_total) * 100)))
    sync_badge = latest_status if latest else "Waiting"
    password_note = "Saved password hash is stored; enter a new password to replace it." if config.qobuz_password_md5 else "Password is converted to a Qobuz-compatible hash before storage."
    token_note = "Saved token is stored; enter a new token to replace it." if config.qobuz_user_auth_token else "Use when Qobuz blocks password API login."
    localuser_note = "Paste the full localuser value from Qobuz Web Player to fill user ID and token automatically."
    # Render a static HTML template with escaped dynamic values; this is not a SQL query.
    return f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Qobuz Sync</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #05060a;
      --panel: rgba(255, 255, 255, .075);
      --panel-strong: rgba(255, 255, 255, .105);
      --line: rgba(255, 255, 255, .14);
      --text: #f8fafc;
      --muted: rgba(255, 255, 255, .66);
      --accent: #f59e0b;
      --accent-2: #f97316;
      --green: #34d399;
      --red: #fb7185;
      --shadow: 0 32px 100px rgba(0, 0, 0, .48);
      --glass-inset: inset 0 1px 0 rgba(255, 255, 255, .16);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      min-height: 100vh;
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      background:
        radial-gradient(circle at 16% 10%, rgba(245, 158, 11, .22), transparent 28rem),
        radial-gradient(circle at 78% 8%, rgba(99, 102, 241, .22), transparent 28rem),
        radial-gradient(circle at 52% 102%, rgba(14, 165, 233, .16), transparent 30rem),
        linear-gradient(145deg, #05060a 0%, #0c1020 48%, #05060a 100%);
      color: var(--text);
      overflow-x: hidden;
    }}
    body::before {{ content: ''; position: fixed; inset: 0; pointer-events: none; background-image: linear-gradient(rgba(255,255,255,.035) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.025) 1px, transparent 1px); background-size: 72px 72px; mask-image: radial-gradient(circle at 50% 0%, black, transparent 75%); }}
    body::after {{ content: ''; position: fixed; inset: auto -12rem -22rem -12rem; height: 30rem; pointer-events: none; background: radial-gradient(ellipse at center, rgba(255,255,255,.12), transparent 70%); filter: blur(18px); }}
    main {{ width: min(1180px, 100%); margin: 0 auto; padding: clamp(.85rem, 2.4vw, 1.9rem); position: relative; z-index: 1; }}
    .umbrel-shell {{ min-height: 100vh; }}
    .hero {{
      display: grid;
      grid-template-columns: 1fr;
      gap: .75rem;
      align-items: stretch;
      margin-bottom: .75rem;
    }}
    .hero-card, section, .umbrel-glass {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 32px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(28px) saturate(1.25);
      -webkit-backdrop-filter: blur(28px) saturate(1.25);
      box-shadow: var(--shadow), var(--glass-inset);
      overflow: hidden;
    }}
    .hero-card {{ padding: clamp(1rem, 2.6vw, 1.55rem); overflow: hidden; position: relative; isolation: isolate; }}
    .hero-card::after {{
      content: '';
      position: absolute;
      width: 12rem;
      height: 12rem;
      right: -4.25rem;
      top: -4.25rem;
      border-radius: 999px;
      background: radial-gradient(circle, rgba(245, 158, 11, .18) 0%, rgba(249, 115, 22, .08) 46%, transparent 72%);
      filter: blur(10px);
      opacity: .72;
      pointer-events: none;
      z-index: 0;
    }}
    .hero-copy {{ position: relative; z-index: 1; }}
    .app-icon {{ width: 3rem; height: 3rem; margin-bottom: .7rem; border-radius: 1rem; display: grid; place-items: center; background: linear-gradient(145deg, #fb923c, #f59e0b 46%, #7c2d12); box-shadow: 0 14px 36px rgba(249,115,22,.30), inset 0 1px 0 rgba(255,255,255,.38); }}
    .app-icon svg {{ width: 1.8rem; height: 1.8rem; color: #fff7ed; filter: drop-shadow(0 8px 12px rgba(0,0,0,.24)); }}

    .eyebrow {{ color: var(--accent); font-size: .78rem; font-weight: 800; letter-spacing: .16em; text-transform: uppercase; }}
    h1 {{ margin: .42rem 0 .55rem; font-size: clamp(2rem, 5.4vw, 3.7rem); letter-spacing: -.075em; line-height: .9; }}
    h2 {{ margin: 0 0 1rem; letter-spacing: -.03em; }}
    p {{ color: var(--muted); line-height: 1.65; }}
    .hero p {{ max-width: 58rem; font-size: .98rem; }}
    .hero-actions {{ display: flex; align-items: center; gap: .75rem; flex-wrap: wrap; margin-top: .95rem; }}
    .sync-form {{ display: inline-flex; align-items: center; gap: .65rem; }}
    .sync-check {{ display: inline-grid; place-items: center; width: 2.2rem; height: 2.2rem; border-radius: 999px; color: #ecfdf5; background: rgba(52, 211, 153, .18); border: 1px solid rgba(52, 211, 153, .42); box-shadow: 0 0 26px rgba(52, 211, 153, .28), var(--glass-inset); opacity: 0; transform: scale(.78); transition: opacity .22s ease, transform .22s ease; pointer-events: none; font-size: 1.15rem; font-weight: 950; }}
    .sync-check.visible {{ opacity: 1; transform: scale(1); }}
    .danger-button {{ color: #fff7ed; background: linear-gradient(135deg, #ef4444, #b91c1c); box-shadow: 0 14px 32px rgba(239, 68, 68, .28); }}
    .danger-button:hover {{ box-shadow: 0 18px 42px rgba(239, 68, 68, .36); }}
    button {{
      border: 0;
      border-radius: 999px;
      padding: .85rem 1.15rem;
      font-weight: 850;
      cursor: pointer;
      color: #111827;
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      box-shadow: 0 14px 32px rgba(245, 158, 11, .28);
      transition: transform .16s ease, box-shadow .16s ease, filter .16s ease;
    }}
    button:hover {{ transform: translateY(-1px) scale(1.01); filter: brightness(1.06); box-shadow: 0 18px 42px rgba(245, 158, 11, .36); }}
    .status-panel {{ grid-column: 1 / -1; padding: .8rem; display: grid; grid-template-columns: auto repeat(3, minmax(0, 1fr)); align-items: center; gap: .65rem; }}
    .status-pill {{ display: inline-flex; align-items: center; gap: .4rem; width: fit-content; padding: .38rem .62rem; border-radius: 999px; font-size: .9rem; font-weight: 800; }}
    .status-pill::before {{ content: ''; width: .55rem; height: .55rem; border-radius: 999px; background: currentColor; box-shadow: 0 0 18px currentColor; }}
    .ready {{ color: var(--green); background: rgba(52, 211, 153, .12); }}
    .needs-setup {{ color: var(--accent); background: rgba(245, 158, 11, .13); }}
    .stat {{ padding: .68rem .78rem; border-radius: 18px; background: rgba(255, 255, 255, .07); border: 1px solid rgba(255,255,255,.12); box-shadow: var(--glass-inset); }}
    .stat small {{ display: block; color: var(--muted); text-transform: uppercase; letter-spacing: .12em; font-size: .61rem; font-weight: 800; }}
    .stat strong {{ display: block; margin-top: .18rem; font-size: 1.05rem; }}
    .tabs {{ margin-bottom: 1rem; }}
    .tab-input {{ position: absolute; inline-size: 1px; block-size: 1px; opacity: 0; pointer-events: none; }}
    .tab-list {{
      display: inline-flex;
      gap: .35rem;
      padding: .32rem;
      margin-bottom: 1rem;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: rgba(0, 0, 0, .26);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, .07);
      backdrop-filter: blur(22px);
    }}
    .tab-list label {{
      margin: 0;
      padding: .72rem 1rem;
      border-radius: 999px;
      color: var(--muted);
      cursor: pointer;
      transition: color .15s, background .15s, box-shadow .15s;
    }}
    #tab-library:checked ~ .tab-list label[for="tab-library"],
    #tab-settings:checked ~ .tab-list label[for="tab-settings"] {{
      color: #111827;
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      box-shadow: 0 12px 30px rgba(245, 158, 11, .22);
    }}
    .tab-panel {{ display: none; }}
    #tab-library:checked ~ .tab-panels #library-panel,
    #tab-settings:checked ~ .tab-panels #settings-panel {{ display: block; }}
    .grid {{ display: grid; grid-template-columns: .95fr 1.05fr; gap: 1rem; align-items: start; }}
    section {{ padding: clamp(1rem, 2vw, 1.4rem); }}
    .field-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .9rem 1rem; }}
    .wide-field {{ grid-column: 1 / -1; }}
    label {{ display: block; color: #e5e7eb; font-weight: 750; }}
    label span {{ display: block; margin-bottom: .42rem; }}
    input, select {{
      width: 100%;
      margin: 0;
      padding: .85rem .95rem;
      border-radius: 16px;
      border: 1px solid rgba(255, 255, 255, .12);
      background: rgba(5, 7, 12, .54);
      color: #fff;
      outline: none;
      transition: border-color .15s, box-shadow .15s, background .15s;
    }}
    input:focus, select:focus {{ border-color: rgba(245, 158, 11, .7); box-shadow: 0 0 0 4px rgba(245, 158, 11, .13); background: rgba(5, 7, 12, .84); }}
    .select-wrap {{ position: relative; }}
    .select-wrap::after {{
      content: '⌄';
      position: absolute;
      right: .95rem;
      top: 50%;
      transform: translateY(-55%);
      pointer-events: none;
      color: var(--accent);
      font-size: 1.25rem;
      font-weight: 900;
    }}
    select {{
      appearance: none;
      padding-right: 2.8rem;
      border-color: rgba(245, 158, 11, .24);
      background:
        linear-gradient(135deg, rgba(245, 158, 11, .10), rgba(249, 115, 22, .055)),
        rgba(5, 7, 12, .72);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, .05);
      font-weight: 750;
    }}
    select option {{ background: #111827; color: #f8fafc; }}
    .checks {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: .75rem; margin: 1rem 0; }}
    .checks label {{ display: flex; align-items: center; gap: .55rem; padding: .75rem; border-radius: 16px; background: rgba(255, 255, 255, .065); border: 1px solid var(--line); box-shadow: var(--glass-inset); }}
    .checks input {{ width: 1.05rem; height: 1.05rem; accent-color: var(--accent); }}
    .notice {{ border-left: 3px solid var(--accent); padding: .8rem .95rem; border-radius: 14px; background: rgba(245, 158, 11, .10); color: #fde68a; }}
    .notice p {{ margin: 0 0 .65rem; color: #fde68a; }}
    .notice ol {{ margin: .55rem 0 .65rem 1.25rem; padding: 0; color: #fde68a; }}
    .notice li {{ margin: .28rem 0; }}
    .notice code {{ padding: .08rem .32rem; border-radius: 7px; background: rgba(0, 0, 0, .22); color: #fff7ed; }}
    .sync-status-card {{ margin-bottom: 1rem; padding: 1rem; border-radius: 22px; border: 1px solid var(--line); background: linear-gradient(135deg, rgba(255,255,255,.075), rgba(255,255,255,.035)); background-clip: padding-box; overflow: hidden; isolation: isolate; clip-path: inset(0 round 22px); }}
    .sync-head {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem; margin-bottom: .9rem; }}
    .sync-meta {{ margin: 0; color: var(--muted); }}
    .sync-meta strong {{ color: var(--text); }}
    .sync-badge {{ display: inline-flex; align-items: center; gap: .4rem; padding: .42rem .66rem; border-radius: 999px; font-size: .84rem; font-weight: 900; white-space: nowrap; }}
    .sync-badge::before {{ content: ''; width: .45rem; height: .45rem; border-radius: 999px; background: currentColor; box-shadow: 0 0 14px currentColor; }}
    .sync-badge.success {{ color: var(--green); background: rgba(52, 211, 153, .12); }}
    .sync-badge.error {{ color: var(--red); background: rgba(251, 113, 133, .12); }}
    .sync-badge.muted {{ color: var(--accent); background: rgba(245, 158, 11, .12); }}
    .sync-message {{ margin: 0 0 .85rem; color: var(--text); font-weight: 850; font-size: 1.05rem; }}
    .progress-meter {{ width: 100%; height: .7rem; overflow: hidden; border-radius: 999px; background: rgba(5, 7, 12, .7); border: 1px solid rgba(255,255,255,.08); background-clip: padding-box; clip-path: inset(0 round 999px); transform: translateZ(0); }}
    .progress-fill {{ height: 100%; border-radius: inherit; background: linear-gradient(135deg, var(--accent), var(--accent-2)); box-shadow: 0 0 22px rgba(245, 158, 11, .38); transition: width .25s ease; }}
    .sync-detail-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: .6rem; margin-top: .85rem; }}
    .sync-detail {{ padding: .72rem; border-radius: 14px; background: rgba(5, 7, 12, .42); border: 1px solid rgba(255,255,255,.075); }}
    .sync-detail small {{ display: block; color: var(--muted); text-transform: uppercase; letter-spacing: .12em; font-size: .62rem; font-weight: 850; }}
    .sync-detail strong {{ display: block; margin-top: .22rem; color: var(--text); }}
    .section-head {{ display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; margin-bottom: 1rem; }}
    .section-head.compact {{ margin: .35rem 0 .65rem; }}
    .section-head h2 {{ margin: 0; }}
    .section-head h3 {{ margin: 0; font-size: 1rem; letter-spacing: -.02em; }}
    .section-head span {{ color: var(--muted); font-size: .84rem; font-weight: 750; }}
    .downloads-grid {{ display: grid; gap: .75rem; max-height: 32.8rem; overflow-y: auto; padding-right: .35rem; scroll-snap-type: y proximity; }}
    .downloads-grid::-webkit-scrollbar {{ width: .55rem; }}
    .downloads-grid::-webkit-scrollbar-track {{ background: rgba(0,0,0,.18); border-radius: 999px; }}
    .downloads-grid::-webkit-scrollbar-thumb {{ background: linear-gradient(var(--accent), var(--accent-2)); border-radius: 999px; }}
    .download-card {{ display: grid; grid-template-columns: 5rem minmax(0, 1fr); gap: .85rem; align-items: center; min-height: 5.8rem; padding: .72rem; border-radius: 24px; background: rgba(255, 255, 255, .06); background-clip: padding-box; border: 1px solid var(--line); box-shadow: var(--glass-inset); scroll-snap-align: start; overflow: hidden; isolation: isolate; clip-path: inset(0 round 24px); }}
    .track-progress-card {{ display: grid; gap: .5rem; padding: .72rem; border-radius: 18px; background: rgba(255, 255, 255, .055); background-clip: padding-box; border: 1px solid var(--line); overflow: hidden; isolation: isolate; clip-path: inset(0 round 18px); }}
    .track-progress-copy {{ display: flex; align-items: center; justify-content: space-between; gap: .8rem; color: var(--muted); font-size: .88rem; }}
    .track-progress-copy strong {{ color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .track-progress-meter {{ height: .52rem; overflow: hidden; border-radius: 999px; background: rgba(255,255,255,.09); background-clip: padding-box; clip-path: inset(0 round 999px); transform: translateZ(0); }}
    .track-progress-fill {{ height: 100%; border-radius: inherit; background: linear-gradient(90deg, var(--green), var(--accent)); transition: width .25s ease; }}
    .cover-wrap {{ position: relative; width: 5rem; height: 5rem; overflow: hidden; border-radius: 18px; background: linear-gradient(145deg, rgba(245, 158, 11, .28), rgba(99, 102, 241, .22)); background-clip: padding-box; box-shadow: 0 16px 34px rgba(0,0,0,.34), var(--glass-inset); clip-path: inset(0 round 18px); transform: translateZ(0); }}
    .album-art {{ position: relative; z-index: 1; width: 100%; height: 100%; object-fit: cover; display: block; }}
    .album-art[src=''] {{ display: none; }}
    .cover-fallback {{ position: absolute; inset: 0; display: grid; place-items: center; color: rgba(255,255,255,.74); font-size: 1.8rem; font-weight: 900; }}
    .track-copy {{ min-width: 0; }}
    .track-topline {{ display: flex; align-items: center; gap: .5rem; margin-bottom: .24rem; }}
    .track-copy h3 {{ margin: 0; color: var(--text); font-size: 1.03rem; letter-spacing: -.025em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .track-copy p {{ margin: .18rem 0 .45rem; color: rgba(255,255,255,.78); line-height: 1.35; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .track-meta {{ display: flex; flex-wrap: wrap; gap: .42rem; color: var(--muted); font-size: .78rem; }}
    .track-meta span {{ padding: .24rem .48rem; border-radius: 999px; background: rgba(0,0,0,.22); border: 1px solid rgba(255,255,255,.08); }}
    .track-card-progress {{ grid-column: 1 / -1; margin-top: -.2rem; }}
    .track-progress-caption {{ display: flex; justify-content: space-between; gap: .75rem; margin-bottom: .35rem; color: var(--muted); font-size: .78rem; }}
    .kind-badge {{ display: inline-flex; padding: .25rem .55rem; border-radius: 999px; background: rgba(245, 158, 11, .14); color: #fbbf24; font-weight: 800; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: #cbd5e1; }}
    .path {{ color: #cbd5e1; word-break: break-all; }}
    .empty {{ text-align: center; color: var(--muted); border-radius: 16px !important; }}
    @media (max-width: 860px) {{ .hero, .grid, .field-grid, .status-panel {{ grid-template-columns: 1fr; }} .checks {{ grid-template-columns: 1fr; }} .tab-list {{ display: flex; width: 100%; }} .tab-list label {{ flex: 1; text-align: center; }} .download-card {{ grid-template-columns: 4.25rem minmax(0, 1fr); }} .cover-wrap {{ width: 4.25rem; height: 4.25rem; }} }}
  </style>
</head>
<body>
<main class="umbrel-shell">
  <div class="hero">
    <div class="hero-card">
      <div class="hero-copy">
        <div class="app-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><path d="M8 18.5V8.7l10-2.2v9.7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><path d="M8 18.5c0 1.1-1.15 2-2.55 2S2.9 19.6 2.9 18.5s1.15-2 2.55-2S8 17.4 8 18.5Zm10-2.3c0 1.1-1.15 2-2.55 2s-2.55-.9-2.55-2 1.15-2 2.55-2 2.55.9 2.55 2Z" fill="currentColor"/><path d="M8 11.2 18 9" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg></div>
        <div class="eyebrow">Umbrel · Qobuz archive</div>
        <h1>Your Qobuz library, beautifully archived.</h1>
        <p>Monitor purchased albums and tracks, sync them to your Umbrel Downloads/QobuzSync folder, and keep artwork, and metadata in one place for Navidrome or your preferred music server to digest.</p>
        <div class="hero-actions">
          <form id="sync-now-form" class="sync-form" method="post" action="/sync-now"><button id="sync-now-button" type="submit">Sync now</button><span id="sync-now-check" class="sync-check" aria-live="polite" aria-label="Sync started">✓</span></form>
          <form id="resync-all-form" class="sync-form" method="post" action="/resync-all"><button id="resync-all-button" class="danger-button" type="submit">Re Sync Entire Library</button><span id="resync-all-check" class="sync-check" aria-live="polite" aria-label="Entire library re-synced">✓</span></form>
        </div>
      </div>
    </div>
    <aside class="status-panel hero-card">
      <span class="status-pill {status_class}">{configured}</span>
      <div class="stat"><small>Account</small><strong>{escape(config.qobuz_email) or 'Not set'}</strong></div>
      <div id="found-stat" class="stat"><small>Found</small><strong>{found}</strong></div>
      <div id="downloaded-stat" class="stat"><small>Downloaded</small><strong>{downloaded}</strong></div>
    </aside>
  </div>

  <div class="tabs">
    <input class="tab-input" type="radio" name="tabs" id="tab-library" checked>
    <input class="tab-input" type="radio" name="tabs" id="tab-settings">
    <div class="tab-list" role="tablist" aria-label="Qobuz Sync sections">
      <label role="tab" for="tab-library">Library</label>
      <label role="tab" for="tab-settings">Settings &amp; status</label>
    </div>

    <div class="tab-panels">
      <section id="library-panel" class="tab-panel" role="tabpanel">
        <div class="section-head"><h2>Recent downloads</h2><span>Latest 20 Tracks, Scroll Down To See More</span></div>
        <div id="downloads-grid" class="downloads-grid">{download_rows}</div>
      </section>

      <div id="settings-panel" class="tab-panel" role="tabpanel">
        <div class="grid">
          <section>
            <h2>Settings</h2>
            <form method="post" action="/settings">
              <div class="field-grid">
                <label><span>Qobuz email</span><input name="qobuz_email" type="email" value="{escape(config.qobuz_email)}" autocomplete="username"></label>
                <label><span>Qobuz password</span><input name="qobuz_password" type="password" autocomplete="current-password" value="" placeholder="{password_note}"></label>
                <label class="wide-field"><span>Paste Qobuz browser session</span><input name="qobuz_localuser" type="password" autocomplete="off" value="" placeholder="{localuser_note}"></label>
                <label><span>Qobuz user ID</span><input name="qobuz_user_id" value="{escape(config.qobuz_user_id)}" placeholder="Required with auth token"></label>
                <label><span>User auth token</span><input name="qobuz_user_auth_token" type="password" autocomplete="off" value="" placeholder="{token_note}"></label>
                <label><span>Quality</span>
                  <span class="select-wrap"><select name="quality">
                    {quality_option(config.quality, 5, 'MP3 320')}
                    {quality_option(config.quality, 6, 'Lossless 16-bit / 44.1 kHz')}
                    {quality_option(config.quality, 7, 'Hi-Res 24-bit / <96 kHz')}
                    {quality_option(config.quality, 27, 'Hi-Res 24-bit / >96 kHz')}
                  </select></span>
                </label>
                <label><span>Sync interval minutes</span><input name="interval_minutes" type="number" min="5" value="{config.interval_minutes}"></label>
              </div>
              <div class="checks">
                <label><input name="include_albums" type="checkbox" {'checked' if config.include_albums else ''}> Albums</label>
                <label><input name="include_tracks" type="checkbox" {'checked' if config.include_tracks else ''}> Tracks</label>
                <label><input name="embed_art" type="checkbox" {'checked' if config.embed_art else ''}> Album art &amp; extras</label>
              </div>
              <button type="submit">Save settings</button>
            </form>
          </section>

          <section>
            <h2>Sync status</h2>
            <div class="sync-status-card">
              <div class="sync-head">
                <p class="sync-meta"><strong>Last sync:</strong> <span id="last-sync-time">{latest_time}</span></p>
                <span id="sync-status-badge" class="sync-badge {latest_class}">{sync_badge}</span>
              </div>
              <p id="sync-progress-message" class="sync-message">{progress_message}</p>
              <div class="progress-meter" aria-label="Master downloads progress"><div id="master-downloads-progress-fill" class="progress-fill" style="width: {progress_percent}%"></div></div>
              <div class="sync-detail-grid">
                <div id="progress-phase" class="sync-detail"><small>Phase</small><strong>{progress_phase}</strong></div>
                <div id="progress-label" class="sync-detail"><small>Progress</small><strong>{progress_label}</strong></div>
                <div id="progress-found" class="sync-detail"><small>Found</small><strong>{found}</strong></div>
              </div>
            </div>
            <div class="notice">
              <p>Easiest login: use Qobuz in your browser, then paste its browser session here. This avoids Qobuz's captcha-prone username/password API login.</p>
              <ol>
                <li>Log in to the Qobuz Web Player.</li>
                <li>Open your browser’s Developer Tools / Inspect Element, then choose Console.</li>
                <li>Run <code>copy(localStorage.getItem('localuser'))</code>.</li>
                <li>Paste into <strong>Paste Qobuz browser session</strong>, then save settings.</li>
                <li>Manual fallback: Storage → Local Storage → Qobuz Web Player → <code>localuser</code>; copy <code>token</code> and <code>id</code>.</li>
              </ol>
            </div>
          </section>
        </div>
      </div>
    </div>
  </div>
</main>
<script>
  const syncForm = document.getElementById('sync-now-form');
  const syncButton = document.getElementById('sync-now-button');
  const syncCheck = document.getElementById('sync-now-check');
  const resyncForm = document.getElementById('resync-all-form');
  const resyncButton = document.getElementById('resync-all-button');
  const resyncCheck = document.getElementById('resync-all-check');
  let syncCheckTimer;
  let resyncCheckTimer;
  let progressPollTimer;
  let liveRefreshTimer;
  const ACTIVE_REFRESH_MS = 2000;
  const IDLE_REFRESH_MS = 8000;

  function formatLatestSync(latestSync) {{
    if (!latestSync) return 'Never synced';
    return `${{latestSync.finished_at || '—'}} — ${{latestSync.message || 'Sync completed'}}`;
  }}

  function syncBadgeText(latestSync) {{
    if (!latestSync) return 'Waiting';
    return latestSync.success ? 'All Good!' : 'Needs attention';
  }}

  function syncBadgeClass(latestSync) {{
    if (!latestSync) return 'sync-badge';
    return `sync-badge ${{latestSync.success ? 'ok' : 'error'}}`;
  }}

  function progressLabel(progress) {{
    const current = Number(progress?.current || 0);
    const total = Number(progress?.total || 0);
    const phase = String(progress?.phase || 'idle').replaceAll('_', ' ').replace(/\\b\\w/g, c => c.toUpperCase());
    return total ? `${{phase}} ${{current}}/${{total}}` : phase;
  }}

  async function refreshProgressOnce() {{
    const response = await fetch('/api/progress', {{ headers: {{ 'Accept': 'application/json' }} }});
    if (!response.ok) throw new Error(`Progress request failed: ${{response.status}}`);
    const payload = await response.json();
    const progress = payload.progress || {{ phase: 'idle', message: 'Idle', current: 0, total: 0 }};
    document.querySelector('#downloaded-stat strong')?.replaceChildren(String(payload.downloaded_total ?? 0));
    const latestFound = payload.latest_sync?.found ?? '—';
    document.querySelector('#found-stat strong')?.replaceChildren(String(latestFound));
    document.querySelector('#progress-found strong')?.replaceChildren(String(latestFound));
    document.getElementById('sync-progress-message')?.replaceChildren(String(progress.message || 'Idle'));
    const current = Number(progress.current || 0);
    const total = Number(progress.total || 0);
    const percent = total ? Math.max(0, Math.min(100, Math.round((current / total) * 100))) : (progress.phase === 'complete' ? 100 : 0);
    const masterFill = document.getElementById('master-downloads-progress-fill');
    if (masterFill) masterFill.style.width = `${{percent}}%`;
    document.querySelector('#progress-phase strong')?.replaceChildren(String(progress.phase || 'idle').replaceAll('_', ' ').replace(/\\b\\w/g, c => c.toUpperCase()));
    document.querySelector('#progress-label strong')?.replaceChildren(progressLabel(progress));
    const latestSync = payload.latest_sync || null;
    document.getElementById('last-sync-time')?.replaceChildren(formatLatestSync(latestSync));
    const badge = document.getElementById('sync-status-badge');
    if (badge) {{
      badge.className = syncBadgeClass(latestSync);
      badge.replaceChildren(syncBadgeText(latestSync));
    }}
    const grid = document.getElementById('downloads-grid');
    // downloads_html is generated by render_download_rows(), which escapes every
    // dynamic value before returning the small fixed card template.
    if (grid && typeof payload.downloads_html === 'string') grid.innerHTML = payload.downloads_html;
    return progress.phase;
  }}

  function activePhase(phase) {{
    return !['complete', 'error', 'idle'].includes(String(phase || 'idle'));
  }}

  function startProgressPolling() {{
    clearInterval(progressPollTimer);
    refreshProgressOnce().catch(console.error);
    progressPollTimer = setInterval(async () => {{
      try {{
        const phase = await refreshProgressOnce();
        if (!activePhase(phase)) clearInterval(progressPollTimer);
      }} catch (error) {{
        console.error(error);
      }}
    }}, ACTIVE_REFRESH_MS);
  }}

  function startLiveRefresh() {{
    clearTimeout(liveRefreshTimer);
    const tick = async () => {{
      let delay = IDLE_REFRESH_MS;
      try {{
        const phase = await refreshProgressOnce();
        delay = activePhase(phase) ? ACTIVE_REFRESH_MS : IDLE_REFRESH_MS;
      }} catch (error) {{
        console.error(error);
      }}
      liveRefreshTimer = setTimeout(tick, delay);
    }};
    liveRefreshTimer = setTimeout(tick, IDLE_REFRESH_MS);
  }}

  syncForm?.addEventListener('submit', async (event) => {{
    event.preventDefault();
    if (!syncButton || !syncCheck) return;
    const originalLabel = syncButton.textContent;
    syncButton.disabled = true;
    syncButton.textContent = 'Syncing…';
    try {{
      const response = await fetch(syncForm.action, {{
        method: 'POST',
        headers: {{ 'Accept': 'application/json', 'X-Requested-With': 'fetch' }},
      }});
      if (!response.ok) throw new Error(`Sync request failed: ${{response.status}}`);
      startProgressPolling();
      syncCheck.classList.add('visible');
      clearTimeout(syncCheckTimer);
      syncCheckTimer = setTimeout(() => syncCheck.classList.remove('visible'), 2000);
    }} catch (error) {{
      console.error(error);
    }} finally {{
      syncButton.disabled = false;
      syncButton.textContent = originalLabel || 'Sync now';
    }}
  }});

  resyncForm?.addEventListener('submit', async (event) => {{
    event.preventDefault();
    if (!resyncButton || !resyncCheck) return;
    const originalLabel = resyncButton.textContent;
    resyncButton.disabled = true;
    resyncButton.textContent = 'Re Syncing…';
    try {{
      const response = await fetch(resyncForm.action, {{
        method: 'POST',
        headers: {{ 'Accept': 'application/json', 'X-Requested-With': 'fetch' }},
      }});
      if (!response.ok) throw new Error(`Re-sync request failed: ${{response.status}}`);
      const result = await response.json();
      if (!result.started) throw new Error(result.message || 'Re-sync failed');
      startProgressPolling();
      resyncCheck.classList.add('visible');
      clearTimeout(resyncCheckTimer);
      resyncCheckTimer = setTimeout(() => resyncCheck.classList.remove('visible'), 2000);
    }} catch (error) {{
      console.error(error);
    }} finally {{
      resyncButton.disabled = false;
      resyncButton.textContent = originalLabel || 'Re Sync Entire Library';
    }}
  }});

  startLiveRefresh();
</script>
</body>
</html>
"""


def progress_text(progress: dict | None) -> str:
    if not progress:
        return "Idle"
    current = int(progress.get("current") or 0)
    total = int(progress.get("total") or 0)
    phase = str(progress.get("phase") or "idle").replace("_", " ").title()
    return f"{phase} {current}/{total}" if total else phase


def render_download_rows(downloads: list[dict], *, track_progress: list[dict] | None = None) -> str:
    active_progress = [row for row in (track_progress or []) if str(row.get("status") or "") != "downloaded"]
    progress_by_key = {(str(row.get("kind") or "track"), str(row.get("purchase_id") or "")): row for row in active_progress}
    rows = []
    row_index_by_key: dict[tuple[str, str], int] = {}
    seen_keys: set[tuple[str, str]] = set()
    for key, progress in progress_by_key.items():
        rows.append(progress_only_download_row(key, progress))
        row_index_by_key[key] = len(rows) - 1
        seen_keys.add(key)
    for row in downloads:
        key = (str(row.get("kind") or "track"), str(row.get("purchase_id") or ""))
        if key in seen_keys:
            if progress_by_key.get(key):
                rows[row_index_by_key[key]] = {**row, "progress": progress_by_key[key]}
            continue
        seen_keys.add(key)
        rows.append({**row, "progress": progress_by_key.get(key)})
    return "".join(
        f"""
        <article class="download-card">
          <div class="cover-wrap">
            <img class="album-art" src="{escape(row['art_url'])}" alt="Album art for {escape(row['display_title'])}" loading="lazy">
            <span class="cover-fallback">♪</span>
          </div>
          <div class="track-copy">
            <div class="track-topline"><span class="kind-badge">{escape(row['kind'])}</span><span class="mono">#{escape(row['purchase_id'])}</span></div>
            <h3>{escape(row['display_title'])}</h3>
            <p>{escape(row['artist'])}</p>
            <div class="track-meta"><span>{escape(row['album'])}</span><span>{escape(row['duration'])}</span><span>{escape(row['downloaded_at'])}</span></div>
          </div>
          {render_track_card_progress(row.get('progress'))}
        </article>
        """
        for row in rows
    ) or "<div class='empty'>No downloads recorded yet.</div>"


def progress_only_download_row(key: tuple[str, str], progress: dict) -> dict[str, object]:
    return {
        "kind": key[0],
        "purchase_id": key[1],
        "display_title": progress.get("title") or key[1] or "Track",
        "artist": "Writing to disk",
        "album": "Download in progress",
        "duration": "—",
        "downloaded_at": progress.get("status") or "downloading",
        "art_url": f"/progress-art/{key[1]}",
        "progress": progress,
    }


def render_track_card_progress(progress: object) -> str:
    if not isinstance(progress, dict):
        return ""
    status = escape(progress.get("status") or "pending")
    downloaded = escape(format_bytes(progress.get("downloaded_bytes", 0)))
    total = escape(format_bytes(progress.get("total_bytes", 0)))
    percent = progress_percent_value(progress.get("percent"))
    return f"""
          <div class="track-card-progress">
            <div class="track-progress-caption"><span>{status}</span><span>{downloaded} / {total}</span></div>
            <div class="track-progress-meter" aria-label="Track download progress"><div class="track-progress-fill" style="width: {percent}%"></div></div>
          </div>
    """


def format_bytes(value: object) -> str:
    size = int(value or 0)
    if size <= 0:
        return "unknown"
    units = ["B", "KB", "MB", "GB"]
    amount = float(size)
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    return f"{amount:.1f} {unit}" if unit != "B" else f"{size} B"


def progress_percent_value(value: object) -> int:
    try:
        percent = int(value or 0)
    except (TypeError, ValueError):
        percent = 0
    return max(0, min(100, percent))


def enrich_downloads(downloads: list[dict], state: SyncState | None = None) -> list[dict[str, object]]:
    client = logged_in_qobuz_client(state) if state else None
    return [enrich_download(row, qobuz_client=client) for row in downloads]


def enrich_download(row: dict, qobuz_client: object | None = None) -> dict[str, object]:
    path = Path(str(row.get("path") or ""))
    metadata = media_metadata(path)
    if not metadata.get("artist") or not metadata.get("duration"):
        remote_metadata = qobuz_download_metadata(qobuz_client, row)
        metadata = {**remote_metadata, **{key: value for key, value in metadata.items() if value}}
    row_title = str(row.get("title") or "")
    parsed_artist, parsed_title = split_artist_title(row_title)
    title = metadata.get("title") or parsed_title or row_title or "Untitled"
    artist = metadata.get("artist") or parsed_artist or "Unknown artist"
    album = metadata.get("album") or (parsed_title if row.get("kind") == "album" else "Single") or "Album"
    return {
        **row,
        "display_title": title,
        "artist": artist,
        "album": album,
        "duration": metadata.get("duration") or "—",
        "art_url": f"/art/{row.get('kind')}/{row.get('purchase_id')}",
    }


def media_metadata(path: Path) -> dict[str, str]:
    flac = first_audio_for_path(path)
    if flac is None:
        return {}
    try:
        from mutagen import File as MutagenFile
    except Exception:
        return {}
    try:
        audio = MutagenFile(flac)
    except Exception:
        return {}
    if audio is None:
        return {}
    tags = getattr(audio, "tags", None) or {}
    info = getattr(audio, "info", None)
    duration = format_duration(getattr(info, "length", None))
    return {
        "title": first_tag(tags, "title"),
        "artist": first_tag(tags, "artist", "albumartist", "performer"),
        "album": first_tag(tags, "album"),
        "duration": duration,
    }


def logged_in_qobuz_client(state: SyncState | None) -> object | None:
    if state is None:
        return None
    config = state.load_config()
    if not config.is_configured:
        return None
    try:
        client = SyncService(state)._build_client()
        client.login(
            config.qobuz_email,
            config.qobuz_password_md5,
            user_id=config.qobuz_user_id,
            user_auth_token=config.qobuz_user_auth_token,
        )
        return client
    except Exception:
        return None


def qobuz_download_metadata(client: object | None, row: dict) -> dict[str, str]:
    if client is None:
        return {}
    kind = str(row.get("kind") or "")
    purchase_id = str(row.get("purchase_id") or "")
    if not purchase_id:
        return {}
    try:
        if kind == "track":
            track = client.get_track(purchase_id)  # type: ignore[attr-defined]
            album = track.get("album") if isinstance(track.get("album"), dict) else {}
            performer = track.get("performer", {}).get("name") if isinstance(track.get("performer"), dict) else ""
            album_artist = album.get("artist", {}).get("name") if isinstance(album.get("artist"), dict) else ""
            return {
                "title": str(track.get("title") or track.get("name") or ""),
                "artist": str(performer or album_artist or ""),
                "album": str(album.get("title") or album.get("name") or ""),
                "duration": format_duration(track.get("duration")),
            }
        if kind == "album":
            album = client.get_album(purchase_id)  # type: ignore[attr-defined]
            artist = album.get("artist", {}).get("name") if isinstance(album.get("artist"), dict) else ""
            return {
                "title": str(album.get("title") or album.get("name") or ""),
                "artist": str(artist or ""),
                "album": str(album.get("title") or album.get("name") or ""),
                "duration": format_duration(album.get("duration")),
            }
    except Exception:
        return {}
    return {}


def split_artist_title(value: str) -> tuple[str, str]:
    """Split Qobuz Sync's stored 'Artist - Title' fallback names."""
    value = str(value or "").strip()
    if " - " not in value:
        return "", value
    artist, title = value.split(" - ", 1)
    return artist.strip(), title.strip()


def first_tag(tags: object, *names: str) -> str:
    for name in names:
        try:
            value = tags.get(name)  # type: ignore[attr-defined]
        except AttributeError:
            continue
        if isinstance(value, (list, tuple)) and value:
            return str(value[0])
        if value:
            return str(value)
    return ""


def first_audio_for_path(path: Path) -> Path | None:
    if path.is_file():
        return path
    if path.is_dir():
        return next(iter(sorted(path.glob("*.flac"))), None)
    return None


def first_cover_for_path(path: Path) -> Path | None:
    directory = path.parent if path.is_file() else path
    if not directory.is_dir():
        return None
    for pattern in ("cover.jpg", "cover.jpeg", "cover.png", "cover.webp", "folder.jpg", "folder.png"):
        candidate = directory / pattern
        if candidate.is_file():
            return candidate
    for candidate in sorted(directory.iterdir()):
        if candidate.is_file() and candidate.stem.lower() in {"cover", "folder", "front"} and candidate.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            return candidate
    for candidate in sorted(directory.glob("cover.*")):
        if candidate.is_file():
            return candidate
    return None


def recover_cover_from_qobuz(state: SyncState, kind: str, purchase_id: str, path: Path) -> Path | None:
    """Fetch missing cover art for old downloads that predate artwork backfill."""
    if kind not in {"album", "track"}:
        return None
    config = state.load_config()
    if not config.is_configured:
        return None
    try:
        client = SyncService(state)._build_client()
        client.login(
            config.qobuz_email,
            config.qobuz_password_md5,
            user_id=config.qobuz_user_id,
            user_auth_token=config.qobuz_user_auth_token,
        )
        client.download_owned_item_extras({"kind": kind, "id": purchase_id, "title": ""}, path)
    except Exception:
        return None
    return first_cover_for_path(path)


def download_title(state: SyncState, kind: str, purchase_id: str) -> str:
    for row in state.list_downloads(limit=200):
        if str(row.get("kind")) == kind and str(row.get("purchase_id")) == purchase_id:
            return str(row.get("title") or "")
    return ""


def fallback_cover_svg(title: str, subtitle: str) -> str:
    title = str(title or "Qobuz Sync")
    initials = "".join(part[:1] for part in title.replace("-", " ").split()[:2]).upper() or "♪"
    safe_title = escape(title[:46])
    safe_subtitle = escape(subtitle.title() or "Track")
    safe_initials = escape(initials[:2])
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-label="Artwork placeholder for {safe_title}">
  <defs>
    <linearGradient id="bg" x1="0" x2="1" y1="0" y2="1">
      <stop offset="0" stop-color="#f59e0b"/>
      <stop offset="0.48" stop-color="#7c3aed"/>
      <stop offset="1" stop-color="#0284c7"/>
    </linearGradient>
    <radialGradient id="glow" cx="35%" cy="22%" r="75%">
      <stop offset="0" stop-color="#fff7ed" stop-opacity="0.55"/>
      <stop offset="1" stop-color="#020617" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="512" height="512" rx="88" fill="url(#bg)"/>
  <rect width="512" height="512" rx="88" fill="url(#glow)"/>
  <circle cx="408" cy="108" r="132" fill="#fff" opacity="0.12"/>
  <circle cx="82" cy="416" r="154" fill="#020617" opacity="0.23"/>
  <text x="256" y="248" text-anchor="middle" dominant-baseline="middle" font-family="Inter, Arial, sans-serif" font-size="116" font-weight="900" fill="#fff">{safe_initials}</text>
  <text x="256" y="356" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="31" font-weight="800" fill="#fff">{safe_subtitle}</text>
  <text x="256" y="402" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="24" font-weight="600" fill="#fff" opacity="0.78">{safe_title}</text>
</svg>"""


def embedded_art_for_path(path: Path) -> tuple[bytes, str] | None:
    flac = first_audio_for_path(path)
    if flac is None:
        return None
    try:
        from mutagen.flac import FLAC
    except Exception:
        return None
    try:
        audio = FLAC(flac)
    except Exception:
        return None
    for picture in getattr(audio, "pictures", []) or []:
        data = getattr(picture, "data", b"")
        if data:
            return bytes(data), str(getattr(picture, "mime", "image/jpeg") or "image/jpeg")
    return None


def format_duration(seconds: object) -> str:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return ""
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def build_track_progress_rows(state: SyncState, limit: int = 50) -> list[dict]:
    progress_state = state.latest_progress() or {}
    if str(progress_state.get("phase") or "") in {"complete", "error", "idle"}:
        return []
    track_progress = []
    for row in state.list_track_progress(limit=limit):
        total_bytes = int(row.get("total_bytes") or 0)
        downloaded_bytes = int(row.get("downloaded_bytes") or 0)
        percent = 100 if row.get("status") == "downloaded" else 0
        if total_bytes:
            percent = max(0, min(100, round((downloaded_bytes / total_bytes) * 100)))
        track_progress.append({**row, "percent": percent})
    return track_progress


def progress_payload(state: SyncState) -> dict[str, object]:
    progress = state.latest_progress() or {"phase": "idle", "message": "Idle", "current": 0, "total": 0, "updated_at": ""}
    latest = state.latest_sync()
    downloads = enrich_downloads(state.list_downloads(limit=20), state=state)
    track_progress = build_track_progress_rows(state)
    return {
        "progress": progress,
        "latest_sync": latest,
        "downloaded_total": state.count_downloads(),
        "downloads": downloads,
        "downloads_html": render_download_rows(downloads, track_progress=track_progress),
        "track_progress": track_progress,
    }


def quality_option(current: int, value: int, label: str) -> str:
    selected = "selected" if current == value else ""
    return f'<option value="{value}" {selected}>{label}</option>'


def escape(value: object) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


app = create_app()
