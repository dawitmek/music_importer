#!/usr/bin/env python3
"""Music Vault Backend Server - Music Downloader & Library Manager"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from pathlib import Path
from typing import Optional
from datetime import datetime

import requests
from aiohttp import web, WSMsgType
import aiohttp
import aiofiles

# ── Paths ──────────────────────────────────────────────────────────────────────
# Resolve BASE_DIR: walk up from server.py until we find the project root
# (identified by presence of downloads/ or start.sh)
_HERE = Path(__file__).resolve().parent
if (_HERE / "downloads").exists() or (_HERE / "start.sh").exists():
    BASE_DIR = _HERE            # flat layout: server.py at project root
else:
    BASE_DIR = _HERE.parent     # nested layout: server.py inside backend/

DOWNLOADS_DIR = BASE_DIR / "downloads"
SINGLES_DIR = DOWNLOADS_DIR / "singles"
PLAYLISTS_DIR = DOWNLOADS_DIR / "playlists"
DATA_DIR = BASE_DIR / "data"
CONFIG_DIR = BASE_DIR / "config"
LOGS_DIR = DATA_DIR / "logs"
STATUS_FILE = DATA_DIR / "status.json"
SONGS_FILE = DATA_DIR / "extracted_songs.json"
CONFIG_FILE = CONFIG_DIR / "config.toml"
# index.html may sit at root (flat) or inside frontend/ (nested)
FRONTEND_DIR = BASE_DIR if (BASE_DIR / "index.html").exists() else BASE_DIR / "frontend"

for d in [SINGLES_DIR, PLAYLISTS_DIR, DATA_DIR, CONFIG_DIR, LOGS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ── Logging ────────────────────────────────────────────────────────────────────
log_file = LOGS_DIR / f"musicvault_{datetime.now().strftime('%Y%m%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("musicvault")


# ── Streamrip config template ──────────────────────────────────────────────────
STREAMRIP_CONFIG_TEMPLATE = """
[downloads]
folder = "__FOLDER__"
source_subdirectories = false
disc_subdirectories = true
concurrency = true
max_connections = 6
requests_per_minute = 60
verify_ssl = true

[qobuz]
quality = 3
download_booklets = true
use_auth_token = false
email_or_userid = ""
password_or_token = ""
app_id = ""
secrets = []

[tidal]
quality = 3
download_videos = true
user_id = ""
country_code = ""
access_token = ""
refresh_token = ""
token_expiry = ""

[deezer]
quality = 2
arl = "__ARL__"
use_deezloader = true
deezloader_warnings = true

[soundcloud]
quality = 0
client_id = ""
app_version = ""

[youtube]
quality = 0
download_videos = false
video_downloads_folder = ""

[database]
downloads_enabled = false
downloads_path = ""
failed_downloads_enabled = false
failed_downloads_path = ""

[conversion]
enabled = false
codec = "ALAC"
sampling_rate = 48000
bit_depth = 24
lossy_bitrate = 320

[qobuz_filters]
extras = false
repeats = false
non_albums = false
features = false
non_studio_albums = false
non_remaster = false

[artwork]
embed = true
embed_size = "large"
embed_max_width = -1
save_artwork = true
saved_max_width = -1

[metadata]
set_playlist_to_album = true
renumber_playlist_tracks = true
exclude = []

[filepaths]
add_singles_to_folder = false
folder_format = "{albumartist} - {title} ({year}) [{container}]"
track_format = "{tracknumber:02}. {artist} - {title}{explicit}"
restrict_characters = false
truncate_to = 120

[lastfm]
source = "qobuz"
fallback_source = ""

[cli]
text_output = true
progress_bars = true
max_search_results = 100

[misc]
version = "2.0.6"
check_for_updates = false
"""

# ── Global state ───────────────────────────────────────────────────────────────
WS_CLIENTS: set = set()
DOWNLOAD_STATUS: dict = {
    "queue": [],
    "active": [],
    "completed": [],
    "failed": [],
    "logs": [],
}
DOWNLOAD_LOCK = asyncio.Lock()


def load_status():
    global DOWNLOAD_STATUS
    if STATUS_FILE.exists():
        try:
            DOWNLOAD_STATUS = json.loads(STATUS_FILE.read_text())
            # Clear active tasks on restart (they were orphaned)
            DOWNLOAD_STATUS["active"] = []
        except Exception:
            pass


def save_status():
    STATUS_FILE.write_text(json.dumps(DOWNLOAD_STATUS, indent=2))


def add_log(msg: str, level: str = "INFO"):
    entry = {"ts": time.time(), "msg": msg, "level": level}
    DOWNLOAD_STATUS["logs"] = DOWNLOAD_STATUS["logs"][-499:] + [entry]
    logger.info(msg)


# ── WebSocket broadcast ────────────────────────────────────────────────────────
async def broadcast(event: str = "status"):
    global WS_CLIENTS
    dead = set()
    payload = json.dumps({"event": event, "data": DOWNLOAD_STATUS})
    for ws in WS_CLIENTS:
        try:
            await ws.send_str(payload)
        except Exception:
            dead.add(ws)
    WS_CLIENTS -= dead


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    WS_CLIENTS.add(ws)
    logger.info(f"WS client connected. Total: {len(WS_CLIENTS)}")
    try:
        # Send current state immediately
        await ws.send_str(json.dumps({"event": "status", "data": DOWNLOAD_STATUS}))
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        WS_CLIENTS.discard(ws)
        logger.info(f"WS client disconnected. Total: {len(WS_CLIENTS)}")
    return ws


# ── Helpers ────────────────────────────────────────────────────────────────────
DEEZER_BASE = "https://api.deezer.com"


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:200]


def deezer_search(q: str, limit: int = 10):
    try:
        r = requests.get(f"{DEEZER_BASE}/search", params={"q": q, "limit": limit}, timeout=8)
        return r.json().get("data", [])
    except Exception as e:
        logger.warning(f"Deezer search error: {e}")
        return []


def deezer_cover_url(artist: str, title: str) -> Optional[str]:
    tracks = deezer_search(f"{artist} {title}", limit=1)
    if tracks and tracks[0].get("album", {}).get("cover_medium"):
        return tracks[0]["album"]["cover_medium"]
    return None


def read_arl() -> Optional[str]:
    # Check env first
    arl = os.environ.get("DEEZER_ARL", "").strip()
    if arl:
        return arl
    # Read from our config.toml
    if CONFIG_FILE.exists():
        content = CONFIG_FILE.read_text()
        m = re.search(r'arl\s*=\s*["' + "'" + r']([^"' + "'" + r']+)["' + "'" + r']', content)
        if m:
            val = m.group(1).strip()
            if val:
                return val
    return None


# ── Download engine ────────────────────────────────────────────────────────────
async def run_streamrip(track_id: str, out_dir: Path) -> bool:
    arl = read_arl()
    if not arl:
        logger.warning("No ARL configured — skipping streamrip")
        return False

    sr_config = CONFIG_DIR / "streamrip_config.toml"

    # Write complete valid streamrip 2.x config from template
    try:
        cfg_text = STREAMRIP_CONFIG_TEMPLATE.replace("__ARL__", arl).replace("__FOLDER__", str(out_dir))
        sr_config.write_text(cfg_text)
    except Exception as e:
        logger.warning(f"Failed to write streamrip config: {e}")
        return False

    # Step 3: run the download
    cmd = ["rip", "--config-path", str(sr_config), "url",
           f"https://www.deezer.com/track/{track_id}"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
        out = (stdout + stderr).decode(errors="replace")
        if out.strip():
            logger.info(f"streamrip output: {out[:600]}")
        if proc.returncode != 0:
            logger.warning(f"streamrip exited {proc.returncode}")
            return False
        # Verify a file actually appeared in out_dir
        files = list(out_dir.glob("*.*"))
        audio_exts = {".flac", ".mp3", ".m4a", ".ogg", ".opus"}
        has_audio = any(f.suffix.lower() in audio_exts for f in files)
        if not has_audio:
            logger.warning(f"streamrip returned 0 but no audio file found in {out_dir}")
            return False
        return True
    except asyncio.TimeoutError:
        logger.warning("streamrip timed out")
        return False
    except FileNotFoundError:
        logger.warning("streamrip (rip) not found in PATH")
        return False


async def run_ytdlp(query: str, out_dir: Path, filename: str) -> bool:
    safe = sanitize_filename(filename)
    out_tmpl = str(out_dir / f"{safe}.%(ext)s")
    cmd = [
        "yt-dlp",
        f"ytsearch1:{query}",
        "-x", "--audio-format", "mp3",
        "--audio-quality", "0",
        "-o", out_tmpl,
        "--no-playlist",
        "--embed-thumbnail",          # embed cover art into the .mp3 file
        "--write-thumbnail",          # also save thumbnail as a separate file
        "--convert-thumbnails", "jpg", # ensure thumbnail is saved as JPEG
        "--add-metadata",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        if proc.returncode != 0:
            return False

        # Find the downloaded thumbnail and copy it to cover.jpg
        # yt-dlp saves it as "{safe}.jpg" (after --convert-thumbnails jpg)
        cover_src = out_dir / f"{safe}.jpg"
        cover_dst = out_dir / "cover.jpg"
        if cover_src.exists() and cover_src != cover_dst:
            shutil.copy2(str(cover_src), str(cover_dst))
            logger.info(f"Saved cover art to {cover_dst}")
        else:
            # Fallback: search for any jpg/webp thumbnail yt-dlp may have written
            for thumb in out_dir.glob(f"{safe}.*"):
                if thumb.suffix.lower() in {".jpg", ".jpeg", ".webp", ".png"}:
                    shutil.copy2(str(thumb), str(cover_dst))
                    logger.info(f"Saved cover art (fallback) to {cover_dst}")
                    break

        return True
    except (asyncio.TimeoutError, FileNotFoundError):
        return False


async def process_download(item: dict):
    tid = item["id"]
    artist = item.get("artist", "")
    title = item.get("title", "")
    deezer_id = item.get("deezer_id")
    out_dir = SINGLES_DIR / sanitize_filename(f"{artist} - {title}" if artist else title)
    out_dir.mkdir(parents=True, exist_ok=True)

    add_log(f"Starting download: {artist} - {title}")

    # Move from queue to active
    async with DOWNLOAD_LOCK:
        DOWNLOAD_STATUS["queue"] = [x for x in DOWNLOAD_STATUS["queue"] if x["id"] != tid]
        item["status"] = "downloading"
        item["started_at"] = time.time()
        DOWNLOAD_STATUS["active"].append(item)
        save_status()
    await broadcast()

    success = False
    method = "unknown"

    # Step 1: Try streamrip (Deezer)
    if deezer_id:
        add_log(f"Trying Deezer (streamrip) for track {deezer_id}")
        success = await run_streamrip(deezer_id, out_dir)
        if success:
            method = "deezer"
            add_log(f"✓ Deezer download succeeded: {title}")

    # Step 2: Fallback to yt-dlp
    if not success:
        add_log(f"Falling back to yt-dlp for: {artist} - {title}", "WARNING")
        query = f"{artist} {title} official audio" if artist else title
        success = await run_ytdlp(query, out_dir, f"{artist} - {title}" if artist else title)
        if success:
            method = "youtube"
            add_log(f"✓ YouTube fallback succeeded: {title}")

    # Finalize
    async with DOWNLOAD_LOCK:
        DOWNLOAD_STATUS["active"] = [x for x in DOWNLOAD_STATUS["active"] if x["id"] != tid]
        item["status"] = "completed" if success else "failed"
        item["method"] = method
        item["finished_at"] = time.time()
        item["path"] = str(out_dir)
        if success:
            DOWNLOAD_STATUS["completed"].append(item)
            add_log(f"✓ Completed [{method}]: {artist} - {title}")
        else:
            DOWNLOAD_STATUS["failed"].append(item)
            add_log(f"✗ Failed: {artist} - {title}", "ERROR")
        save_status()
    await broadcast()


# ── Background worker ──────────────────────────────────────────────────────────
async def queue_worker():
    while True:
        async with DOWNLOAD_LOCK:
            queue = DOWNLOAD_STATUS["queue"]
            active = DOWNLOAD_STATUS["active"]

        if queue and len(active) < 3:
            item = queue[0]
            asyncio.create_task(process_download(item))

        await asyncio.sleep(2)


# ── API Routes ─────────────────────────────────────────────────────────────────

async def search_suggestions(request):
    q = request.rel_url.query.get("q", "")
    if not q:
        return web.json_response([])
    tracks = deezer_search(q, limit=15)
    results = []
    for t in tracks:
        results.append({
            "id": t.get("id"),
            "title": t.get("title", ""),
            "artist": t.get("artist", {}).get("name", ""),
            "album": t.get("album", {}).get("title", ""),
            "duration": t.get("duration", 0),
            "cover": t.get("album", {}).get("cover_medium", ""),
            "preview": t.get("preview", ""),
        })
    return web.json_response(results)


async def download_single(request):
    body = await request.json()
    item = {
        "id": str(uuid.uuid4()),
        "title": body.get("title", "Unknown"),
        "artist": body.get("artist", ""),
        "album": body.get("album", ""),
        "deezer_id": body.get("deezer_id"),
        "cover": body.get("cover", ""),
        "status": "pending",
        "queued_at": time.time(),
        "type": "single",
    }
    async with DOWNLOAD_LOCK:
        DOWNLOAD_STATUS["queue"].append(item)
        save_status()
    await broadcast()
    return web.json_response({"ok": True, "id": item["id"]})


async def download_playlist(request):
    body = await request.json()
    tracks = body.get("tracks", [])
    ids = []
    async with DOWNLOAD_LOCK:
        for t in tracks:
            item = {
                "id": str(uuid.uuid4()),
                "title": t.get("title", "Unknown"),
                "artist": t.get("artist", ""),
                "deezer_id": t.get("deezer_id"),
                "cover": t.get("cover", ""),
                "status": "pending",
                "queued_at": time.time(),
                "type": "playlist",
            }
            DOWNLOAD_STATUS["queue"].append(item)
            ids.append(item["id"])
        save_status()
    await broadcast()
    return web.json_response({"ok": True, "ids": ids, "count": len(ids)})


async def clear_queue(request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    clear_all = body.get("all", False)
    async with DOWNLOAD_LOCK:
        DOWNLOAD_STATUS["queue"] = []
        if clear_all:
            DOWNLOAD_STATUS["completed"] = []
            DOWNLOAD_STATUS["failed"] = []
        save_status()
    await broadcast()
    return web.json_response({"ok": True})


async def track_cover(request):
    artist = request.rel_url.query.get("artist", "")
    title = request.rel_url.query.get("title", "")
    folder = request.rel_url.query.get("folder", "")

    # 1. Check local cover.jpg
    if folder:
        for name in ["cover.jpg", "cover.png", "folder.jpg", "folder.png"]:
            p = Path(folder) / name
            if p.exists():
                return web.FileResponse(p)

    # 2. Fetch from Deezer
    if artist or title:
        url = deezer_cover_url(artist, title)
        if url:
            try:
                r = requests.get(url, timeout=8)
                if r.status_code == 200:
                    return web.Response(
                        body=r.content,
                        content_type=r.headers.get("Content-Type", "image/jpeg"),
                    )
            except Exception:
                pass

    # 3. Return placeholder SVG
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="250" height="250" viewBox="0 0 250 250">
  <rect width="250" height="250" fill="#1a1a2e"/>
  <circle cx="125" cy="125" r="60" fill="none" stroke="#6c63ff" stroke-width="3"/>
  <circle cx="125" cy="125" r="20" fill="#6c63ff"/>
  <text x="125" y="220" text-anchor="middle" fill="#6c63ff" font-size="12" font-family="monospace">♪ Music Vault</text>
</svg>"""
    return web.Response(text=svg, content_type="image/svg+xml")


async def list_files(request):
    path_param = request.rel_url.query.get("path", "")
    base = DOWNLOADS_DIR
    target = (base / path_param).resolve()
    if not str(target).startswith(str(base)):
        return web.json_response({"error": "forbidden"}, status=403)
    if not target.exists():
        return web.json_response({"error": "not found"}, status=404)

    items = []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: (e.is_file(), e.name.lower())):
            stat = entry.stat()
            items.append({
                "name": entry.name,
                "type": "file" if entry.is_file() else "dir",
                "size": stat.st_size if entry.is_file() else 0,
                "modified": stat.st_mtime,
                "ext": entry.suffix.lower() if entry.is_file() else "",
                "path": str(entry.relative_to(base)),
            })
    except PermissionError:
        pass

    # Disk usage
    total, used, free = shutil.disk_usage(str(base))
    return web.json_response({
        "items": items,
        "path": path_param,
        "disk": {"total": total, "used": used, "free": free},
    })


async def rename_file(request):
    body = await request.json()
    rel = body.get("path", "")
    new_name = sanitize_filename(body.get("new_name", ""))
    src = (DOWNLOADS_DIR / rel).resolve()
    if not str(src).startswith(str(DOWNLOADS_DIR)):
        return web.json_response({"error": "forbidden"}, status=403)
    dst = src.parent / new_name
    src.rename(dst)
    return web.json_response({"ok": True})


async def delete_file(request):
    body = await request.json()
    rel = body.get("path", "")
    target = (DOWNLOADS_DIR / rel).resolve()
    if not str(target).startswith(str(DOWNLOADS_DIR)):
        return web.json_response({"error": "forbidden"}, status=403)
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return web.json_response({"ok": True})


async def zip_folder(request):
    body = await request.json()
    rel = body.get("path", "")
    folder = (DOWNLOADS_DIR / rel).resolve()
    if not str(folder).startswith(str(DOWNLOADS_DIR)) or not folder.is_dir():
        return web.json_response({"error": "invalid"}, status=400)

    zip_path = folder.parent / f"{folder.name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in folder.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(folder.parent))

    rel_zip = str(zip_path.relative_to(DOWNLOADS_DIR))
    return web.json_response({"ok": True, "zip_path": rel_zip})


async def serve_file(request):
    rel = request.match_info.get("path", "")
    target = (DOWNLOADS_DIR / rel).resolve()
    if not str(target).startswith(str(DOWNLOADS_DIR)) or not target.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(target)


async def get_status(request):
    return web.json_response(DOWNLOAD_STATUS)


async def save_config(request):
    body = await request.json()
    arl = body.get("arl", "")
    quality = body.get("quality", "MP3_320")
    toml_content = f"""[deezer]
arl = "{arl}"

[downloads]
folder = "{DOWNLOADS_DIR}"
quality = "{quality}"
"""
    CONFIG_FILE.write_text(toml_content)
    return web.json_response({"ok": True})


async def get_config(request):
    cfg = {"arl": "", "quality": "MP3_320"}
    if CONFIG_FILE.exists():
        content = CONFIG_FILE.read_text()
        m = re.search(r'arl\s*=\s*["' + "'" + r']([^"' + "'" + r']+)["' + "'" + r']', content)
        if m:
            cfg["arl"] = m.group(1)
        m = re.search(r'quality\s*=\s*"([^"]*)"', content)
        if m:
            cfg["quality"] = m.group(1)
    return web.json_response(cfg)


async def get_logs(request):
    return web.json_response(DOWNLOAD_STATUS.get("logs", []))


async def serve_index(request):
    return web.FileResponse(FRONTEND_DIR / "index.html")


# ── App setup ──────────────────────────────────────────────────────────────────

async def on_startup(app):
    load_status()
    asyncio.create_task(queue_worker())
    add_log("Music Vault server started")
    await broadcast()


def create_app():
    app = web.Application()
    app.on_startup.append(on_startup)

    # Routes
    app.router.add_get("/ws/status", ws_handler)
    app.router.add_get("/api/search/suggestions", search_suggestions)
    app.router.add_post("/api/download/single", download_single)
    app.router.add_post("/api/download/playlist", download_playlist)
    app.router.add_post("/api/download/clear", clear_queue)
    app.router.add_get("/api/track-cover", track_cover)
    app.router.add_get("/api/files", list_files)
    app.router.add_post("/api/files/rename", rename_file)
    app.router.add_post("/api/files/delete", delete_file)
    app.router.add_post("/api/files/zip", zip_folder)
    app.router.add_get("/api/status", get_status)
    app.router.add_get("/api/config", get_config)
    app.router.add_post("/api/config", save_config)
    app.router.add_get("/api/logs", get_logs)
    app.router.add_get("/files/{path:.+}", serve_file)
    app.router.add_get("/", serve_index)
    static_dir = FRONTEND_DIR / "static"
    if static_dir.exists():
        app.router.add_static("/static", static_dir, show_index=False)

    return app


if __name__ == "__main__":
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("MV_PORT", 8080)))