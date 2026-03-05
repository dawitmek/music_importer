#!/usr/bin/env python3
"""Music Vault Backend Server - Music Downloader & Library Manager"""

import asyncio
import base64
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
KEY_FILE = CONFIG_DIR / ".vaultkey"

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

# ── Encryption Helper ────────────────────────────────────────────────────────
class Vault:
    _memory_key: Optional[bytes] = None

    @classmethod
    def _get_key(cls) -> bytes:
        # 1. Try memory (already loaded)
        if cls._memory_key:
            return cls._memory_key
            
        # 2. Try local file (.vaultkey) — Primary source for persistence
        if KEY_FILE.exists():
            try:
                cls._memory_key = KEY_FILE.read_bytes().strip()
                if cls._memory_key:
                    # Ensure it's padded/truncated to 32 bytes for consistency
                    cls._memory_key = cls._memory_key.ljust(32, b'\0')[:32]
                    return cls._memory_key
            except Exception as e:
                logger.error(f"Failed to read vault key from file: {e}")

        # 3. Try Docker Secrets (High security)
        secret_path = Path("/run/secrets/MUSIC_VAULT_KEY")
        if secret_path.exists():
            try:
                cls._memory_key = secret_path.read_bytes().strip()
                if cls._memory_key:
                    cls._memory_key = cls._memory_key.ljust(32, b'\0')[:32]
                    return cls._memory_key
            except Exception: pass

        # 4. Try environment variable (Secondary source)
        env_key = os.environ.get("MUSIC_VAULT_KEY")
        if env_key:
            cls._memory_key = env_key.encode().ljust(32, b'\0')[:32]
            return cls._memory_key
            
        # 5. Generate and save if nothing else is found
        # This ensures the app still works "out of the box" and persists to the file
        key = os.urandom(32)
        try:
            KEY_FILE.write_bytes(key)
            cls._memory_key = key
            logger.info(f"Generated new vault key and saved to {KEY_FILE}")
        except Exception as e:
            logger.error(f"Failed to save vault key: {e}")
            # Fallback to a transient key if disk is read-only (not ideal for persistence)
            cls._memory_key = key
            
        return cls._memory_key

    @classmethod
    def encrypt(cls, data: str) -> str:
        if not data: return ""
        key = cls._get_key()
        raw = data.encode()
        encrypted = bytearray()
        for i in range(len(raw)):
            encrypted.append(raw[i] ^ key[i % len(key)])
        return base64.b64encode(encrypted).decode()

    @classmethod
    def decrypt(cls, data: str) -> str:
        if not data: return ""
        try:
            key = cls._get_key()
            raw = base64.b64decode(data)
            decrypted = bytearray()
            for i in range(len(raw)):
                decrypted.append(raw[i] ^ key[i % len(key)])
            return decrypted.decode()
        except Exception:
            return data

def read_config_raw() -> str:
    if not CONFIG_FILE.exists():
        return ""
    content = CONFIG_FILE.read_text()
    if not content:
        return ""

    # Try to decrypt. If it fails or returns garbage (non-UTF8),
    # the Vault.decrypt helper returns the original string.
    decrypted = Vault.decrypt(content)

    # If the decrypted version looks like valid config, use it.
    # Otherwise, assume it was already plain text.
    if "[deezer]" in decrypted or "[downloads]" in decrypted or "[spotify]" in decrypted:
        return decrypted

    return content
def write_config_raw(content: str):
    encrypted = Vault.encrypt(content)
    CONFIG_FILE.write_text(encrypted)

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
quality = __QUALITY__
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
    "batch_total": 0,
    "batch_completed": 0,
    "last_batch_finished_at": None,
    "library_size": 0,
}

DEEZER_MAX_QUALITY = None  # Persistent memory for account capability (0, 1, 2, 3)
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


def is_safe_path(target: Path, base: Path) -> bool:
    """Check that target is within base directory (prevents path traversal)."""
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def sanitize_filename(name: str) -> str:
    # Adding # to prevent URL fragment issues
    name = re.sub(r'[<>:"/\\|?*#]', "", name)
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


# Simple LRU cache for cover URLs: key=(artist, title) -> image bytes or None
_COVER_CACHE: dict = {}
_COVER_CACHE_MAX = 500

async def _fetch_cover_bytes(artist: str, title: str) -> Optional[bytes]:
    """Async: look up cover URL from Deezer and fetch image bytes. Uses in-memory cache."""
    cache_key = (artist.lower().strip(), title.lower().strip())
    if cache_key in _COVER_CACHE:
        return _COVER_CACHE[cache_key]

    # Look up URL via Deezer search (still sync, but runs in executor to avoid blocking)
    loop = asyncio.get_running_loop()
    url = await loop.run_in_executor(None, deezer_cover_url, artist, title)

    result = None
    if url:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        result = await r.read()
        except Exception:
            pass

    # Evict oldest entry if cache is full
    if len(_COVER_CACHE) >= _COVER_CACHE_MAX:
        _COVER_CACHE.pop(next(iter(_COVER_CACHE)))
    _COVER_CACHE[cache_key] = result
    return result


def read_arl() -> Optional[str]:
    # Check env first
    arl = os.environ.get("DEEZER_ARL", "").strip()
    if arl:
        return arl
    # Read from our config.toml
    content = read_config_raw()
    if content:
        m = re.search(r'arl\s*=\s*["' + "'" + r']([^"' + "'" + r']+)["' + "'" + r']', content)
        if m:
            val = m.group(1).strip()
            if val:
                return val
    return None


# ── Download engine ────────────────────────────────────────────────────────────
async def run_streamrip(track_id: str, out_dir: Path) -> bool:
    global DEEZER_MAX_QUALITY
    arl = read_arl()
    if not arl:
        logger.warning("No ARL configured — skipping streamrip")
        return False

    # Get user's preferred quality from config
    user_quality = 1 # Default 320kbps
    if CONFIG_FILE.exists():
        content = read_config_raw()
        m = re.search(r'quality\s*=\s*"([^"]*)"', content)
        if m:
            q_str = m.group(1)
            if q_str == "FLAC": user_quality = 2
            elif q_str == "MP3_320": user_quality = 1
            elif q_str == "MP3_128": user_quality = 0

    # Start from the lower of (User Preference) vs (Last Known Max Capability)
    starting_quality = user_quality
    if DEEZER_MAX_QUALITY is not None:
        starting_quality = min(user_quality, DEEZER_MAX_QUALITY)

    # Qualities to try in descending order (2=FLAC, 1=320, 0=128)
    qualities_to_try = [q for q in [2, 1, 0] if q <= starting_quality]
    
    sr_config = CONFIG_DIR / "streamrip_config.toml"

    for q in qualities_to_try:
        add_log(f"Attempting Deezer download at quality level {q}...")
        
        try:
            cfg_text = STREAMRIP_CONFIG_TEMPLATE.replace("__ARL__", arl) \
                                              .replace("__FOLDER__", str(out_dir)) \
                                              .replace("__QUALITY__", str(q))
            sr_config.write_text(cfg_text)
        except Exception as e:
            logger.warning(f"Failed to write streamrip config: {e}")
            return False

        # Standard check
        rip_cmd = "rip"
        if not shutil.which(rip_cmd):
            pipx_rip = Path("/root/.local/bin/rip")
            if pipx_rip.exists():
                rip_cmd = str(pipx_rip)

        cmd = [rip_cmd, "--config-path", str(sr_config), "url",
               f"https://www.deezer.com/track/{track_id}"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
            out = (stdout + stderr).decode(errors="replace")
            
            # Look for quality errors
            error_keywords = [
                "not available for your account",
                "does not support",
                "Codec not available",
                "not authorized",
                "not found" # Sometimes streamrip reports not found for forbidden quality
            ]
            
            if proc.returncode != 0:
                add_log(f"streamrip exited {proc.returncode} for quality {q}. Output: {out[:300]}", "WARNING")
                if any(k.lower() in out.lower() for k in error_keywords):
                    add_log(f"Quality {q} not supported by this account. Stepping down...", "WARNING")
                    continue
                else:
                    continue

            # Verify a file appeared
            files = list(out_dir.glob("*.*"))
            audio_exts = {".flac", ".mp3", ".m4a", ".ogg", ".opus"}
            if any(f.suffix.lower() in audio_exts for f in files):
                # SUCCESS! Remember this quality level
                if DEEZER_MAX_QUALITY is None or q > DEEZER_MAX_QUALITY:
                    # We only update if we haven't set it yet, or found a higher one (unlikely in step-down)
                    # but if we started at a lower preference, don't assume we can do higher
                    if DEEZER_MAX_QUALITY is None:
                        DEEZER_MAX_QUALITY = q
                        add_log(f"Account capability locked to quality level {q}")
                return True

        except FileNotFoundError:
            add_log("The 'rip' command was not found in the system PATH. Using YouTube fallback.", "ERROR")
            return False
        except asyncio.TimeoutError:
            logger.warning(f"streamrip timed out at quality {q}")
            continue
        except Exception as e:
            logger.error(f"streamrip error at quality {q}: {e}")
            continue

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
        if cover_src.exists():
            if cover_src != cover_dst:
                shutil.copy2(str(cover_src), str(cover_dst))
                # Remove the original to prevent duplicates
                try: cover_src.unlink()
                except Exception: pass
            logger.info(f"Saved cover art to {cover_dst}")
        else:
            # Fallback: search for any jpg/webp thumbnail yt-dlp may have written
            for thumb in out_dir.glob(f"{safe}.*"):
                if thumb.suffix.lower() in {".jpg", ".jpeg", ".webp", ".png"}:
                    shutil.copy2(str(thumb), str(cover_dst))
                    # Remove the original
                    try: thumb.unlink()
                    except Exception: pass
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
    
    # Determine output directory
    playlist_name = item.get("playlist_name")
    if playlist_name:
        out_dir = PLAYLISTS_DIR / sanitize_filename(str(playlist_name)) / sanitize_filename(f"{artist} - {title}" if artist else title)
    else:
        out_dir = SINGLES_DIR / sanitize_filename(f"{artist} - {title}" if artist else title)
        
    out_dir.mkdir(parents=True, exist_ok=True)

    add_log(f"Starting download: {artist} - {title}")

    await broadcast()

    success = False
    method = "unknown"

    # Step 1: Try streamrip (Deezer)
    # Ensure we have a valid numeric Deezer ID. Spotify IDs are alphanumeric strings.
    is_valid_deezer_id = str(deezer_id).isdigit() if deezer_id else False

    if not is_valid_deezer_id:
        # Try to find a real Deezer ID if we only have title/artist or a non-numeric ID
        search_query = f"{artist} {title}"
        add_log(f"Searching Deezer ID for: {search_query}")
        loop = asyncio.get_running_loop()
        search_results = await loop.run_in_executor(None, lambda: deezer_search(search_query, limit=1))
        if search_results:
            deezer_id = search_results[0].get("id")
            add_log(f"Found Deezer ID: {deezer_id}")
            is_valid_deezer_id = True
        else:
            add_log(f"No Deezer ID found for {search_query}", "WARNING")

    if is_valid_deezer_id and deezer_id:
        add_log(f"Trying Deezer (streamrip) for track {deezer_id}")
        success = await run_streamrip(str(deezer_id), out_dir)
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
        DOWNLOAD_STATUS["batch_completed"] += 1
        
        # If queue and active are now empty, the batch is done
        if not DOWNLOAD_STATUS["queue"] and not DOWNLOAD_STATUS["active"]:
            DOWNLOAD_STATUS["last_batch_finished_at"] = time.time()
            
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
    asyncio.create_task(refresh_library_size())


# ── Background worker ──────────────────────────────────────────────────────────
async def queue_worker():
    while True:
        item = None
        async with DOWNLOAD_LOCK:
            queue = DOWNLOAD_STATUS["queue"]
            active = DOWNLOAD_STATUS["active"]
            if queue and len(active) < 3:
                item = queue.pop(0)
                item["status"] = "downloading"
                item["started_at"] = time.time()
                DOWNLOAD_STATUS["active"].append(item)
                save_status()

        if item:
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


async def search_playlist(request):
    try:
        url = ""
        if request.content_type == 'application/json':
            body = await request.json()
            url = body.get("url", "")
        else:
            data = await request.post()
            url = data.get("url", "")
            
        if not url:
            # Try query params as last resort
            url = request.query.get("url", "")

        logger.info(f"Playlist search request for URL: {url}")
        
        if not url:
            return web.json_response({"error": "No URL provided"}, status=400)

        # Handle Spotify specifically
        if "spotify.com" in url and "playlist" in url:
            return await handle_spotify_playlist(url)

        # For YouTube and others, get playlist title first
        title_cmd = ["yt-dlp", "--flat-playlist", "--print", "%(playlist_title)s", "--playlist-items", "1", url]
        playlist_title = "Unknown Playlist"
        try:
            proc = await asyncio.create_subprocess_exec(*title_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode == 0:
                playlist_title = stdout.decode().strip() or "Unknown YouTube Playlist"
        except Exception: pass

        cmd = [
            "yt-dlp",
            "--flat-playlist",
            "--dump-json",
            "--quiet",
            url
        ]
        
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        
        if proc.returncode != 0:
            err = stderr.decode().strip()
            logger.error(f"yt-dlp playlist error: {err}")
            return web.json_response({"error": f"Failed to fetch playlist: {err[:100]}"}, status=500)

        tracks = []
        for line in stdout.decode().splitlines():
            if not line: continue
            try:
                data = json.loads(line)
                title = data.get("title", "Unknown")
                artist = data.get("artist") or data.get("uploader") or data.get("channel", "Unknown Artist")
                
                # If title looks like "Artist - Song", split it
                if " - " in title and (not data.get("artist") or data.get("artist") == "Unknown Artist"):
                    parts = title.split(" - ", 1)
                    artist = parts[0].strip()
                    title = parts[1].strip()

                tracks.append({
                    "title": title,
                    "artist": artist,
                    "album": data.get("album", ""),
                    "duration": data.get("duration", 0),
                    "cover": data.get("thumbnail", ""),
                    "id": data.get("id")
                })
            except Exception: continue
            
        return web.json_response({"tracks": tracks, "title": playlist_title})
    except Exception as e:
        logger.exception("Playlist search failed")
        return web.json_response({"error": str(e)}, status=500)


async def handle_spotify_playlist(url):
    try:
        playlist_id = re.search(r'playlist/([a-zA-Z0-9]+)', url).group(1)
        
        # Check the length first by parsing through the playlist embed
        embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(embed_url) as resp:
                if resp.status != 200:
                    return web.json_response({"error": f"Spotify returned status {resp.status}"}, status=resp.status)
                html = await resp.text()
                
        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">([^<]+)</script>', html)
        if not match:
            return web.json_response({"error": "Failed to parse Spotify metadata"}, status=500)
            
        data = json.loads(match.group(1))
        entity = data.get('props', {}).get('pageProps', {}).get('state', {}).get('data', {}).get('entity', {})
        track_list = entity.get('trackList', [])
        playlist_name = entity.get('name', 'Unknown Spotify Playlist')
        
        # Get playlist cover
        playlist_cover = ""
        sources = entity.get('coverArt', {}).get('sources', [])
        if sources:
            playlist_cover = sources[0].get('url', '')
        
        tracks = []
        for t in track_list:
            c_sources = t.get("coverArt", {}).get("sources", [])
            t_cover = c_sources[0].get("url", playlist_cover) if c_sources else playlist_cover
            
            tracks.append({
                "title": t.get("title", "Unknown"),
                "artist": t.get("subtitle", "Unknown Artist"),
                "album": "",
                "duration": t.get("duration", 0) / 1000,
                "cover": t_cover,
                "id": t.get("uri", "").split(":")[-1]
            })

        # If the length of the tracks are less than or equal to 99, move the tracks that were parsed to staging
        if len(tracks) <= 99:
            return web.json_response({"tracks": tracks, "title": playlist_name})

        # If the length is 100 (or more), call the previously wrapped function
        # Check for Spotify API credentials in config
        content = read_config_raw()
        client_id = get_val_from_content(content, "client_id", "spotify")
        client_secret = get_val_from_content(content, "client_secret", "spotify")
        user_access_token = get_val_from_content(content, "access_token", "spotify")
        user_refresh_token = get_val_from_content(content, "refresh_token", "spotify")

        if client_id and client_secret:
            token_to_use = None
            if user_access_token:
                # Try using user token first (unlimited tracks)
                token_to_use = user_access_token
                add_log(f"Fetching playlist {playlist_id} using Spotify User Auth...")
            
            # If we have a token (or can get a client credentials one as fallback)
            # handle_spotify_playlist_api now accepts an optional existing token
            return await handle_spotify_playlist_api(playlist_id, client_id, client_secret, token_to_use, user_refresh_token)
        
        # No Spotify API credentials, using scraped tracks
        add_log(f"No Spotify API credentials, using scraper for {playlist_id}...")
        return web.json_response({"tracks": tracks, "title": playlist_name})
    except Exception as e:
        logger.exception("Spotify playlist handling failed")
        return web.json_response({"error": f"Spotify error: {str(e)}"}, status=500)

async def handle_spotify_playlist_api(playlist_id, client_id, client_secret, token=None, refresh_token=None):
    try:
        access_token = token
        # Track whether we have a real user OAuth token or just client credentials.
        # Since Spotify's 2024 API change, client_credentials tokens are REJECTED (403)
        # on all playlist endpoints — only user-authenticated tokens work.
        using_user_token = token is not None

        async with aiohttp.ClientSession() as session:
            # No user token available — attempt client credentials as a last resort.
            # NOTE: As of Spotify's 2024 API policy change, this will receive a 403
            # on playlist endpoints. We try anyway so we can surface a clear error.
            if not access_token:
                auth_url = "https://accounts.spotify.com/api/token"
                auth_str = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
                async with session.post(auth_url, data={"grant_type": "client_credentials"}, headers={"Authorization": f"Basic {auth_str}"}) as resp:
                    if resp.status == 200:
                        auth_data = await resp.json()
                        access_token = auth_data.get("access_token")
                        using_user_token = False
                    else:
                        err = await resp.text()
                        logger.error(f"Client credentials auth failed ({resp.status}): {err}")
                        return web.json_response({
                            "error": "Spotify authentication failed. Check your Client ID and Secret in Settings.",
                            "requires_spotify_login": True
                        }, status=401)

            if not access_token:
                logger.warning("No Spotify token available after auth attempt")
                return web.json_response({"error": "Spotify authentication failed. No token available."}, status=401)

            # Returns (tracks, name, error_status).
            # error_status is None on success, or the HTTP status code on auth failure.
            async def fetch_tracks(token_to_use):
                headers = {"Authorization": f"Bearer {token_to_use}"}
                playlist_url = f"https://api.spotify.com/v1/playlists/{playlist_id}"
                playlist_name = "Unknown Spotify Playlist"

                async with session.get(playlist_url, headers=headers) as resp:
                    if resp.status == 200:
                        p_data = await resp.json()
                        playlist_name = p_data.get("name", "Unknown Spotify Playlist")
                    elif resp.status in (401, 403):
                        err_body = await resp.text()
                        logger.warning(f"Spotify playlist metadata fetch {resp.status}: {err_body[:200]}")
                        return None, None, resp.status
                    else:
                        err_text = await resp.text()
                        logger.error(f"Spotify playlist fetch failed ({resp.status}): {err_text[:200]}")
                        raise Exception(f"Spotify API error {resp.status}: {err_text}")

                tracks = []
                next_url = f"https://api.spotify.com/v1/playlists/{playlist_id}/items?limit=100"
                while next_url:
                    logger.info(f"API: Fetching page from {next_url}")
                    async with session.get(next_url, headers=headers) as resp:
                        if resp.status == 200:
                            t_data = await resp.json()
                            items = t_data.get("items", [])
                            null_count = 0
                            local_count = 0
                            logger.info(f"API: Received {len(items)} items")

                            # Log the first item's raw structure once so we can
                            # diagnose unexpected API response shapes.
                            if items and len(tracks) == 0:
                                first_item = items[0]
                                first_t = first_item.get("item") or first_item.get("track")
                                if first_t is None:
                                    logger.warning(f"API: First item has no track data. Keys: {list(first_item.keys())}")
                            for item in items:
                                # Spotify renamed this field from "track" to "item"
                                # in their 2026 playlist endpoint response.
                                # We check both for backwards compatibility.
                                t = item.get("item") or item.get("track")
                                if t is None:
                                    null_count += 1
                                    continue
                                if not isinstance(t, dict):
                                    logger.warning(f"API: track field is not a dict: {type(t)}")
                                    continue
                                # Skip local files — they have no Spotify ID
                                if t.get("is_local"):
                                    local_count += 1
                                    continue
                                artists = ", ".join([a.get("name", "Unknown") for a in t.get("artists", [])])
                                album = t.get("album", {})
                                images = album.get("images", [])
                                cover = images[0].get("url", "") if images else ""
                                tracks.append({
                                    "title": t.get("name", "Unknown"),
                                    "artist": artists,
                                    "album": album.get("name", ""),
                                    "duration": t.get("duration_ms", 0) / 1000,
                                    "cover": cover,
                                    "id": t.get("id", "")
                                })

                            if null_count:
                                logger.warning(f"API: Skipped {null_count} null/unavailable tracks on this page")
                            if local_count:
                                logger.info(f"API: Skipped {local_count} local files on this page")
                            logger.info(f"API: Running total — {len(tracks)} valid tracks so far")
                            next_url = t_data.get("next")
                            logger.info(f"API: Next page URL: {next_url}")
                            if len(tracks) > 2000:
                                break
                        elif resp.status in (401, 403):
                            err_body = await resp.text()
                            logger.warning(f"API items loop got {resp.status}: {err_body[:200]}")
                            return None, None, resp.status
                        else:
                            err_text = await resp.text()
                            logger.error(f"Spotify items fetch failed ({resp.status}): {err_text[:200]}")
                            raise Exception(f"Spotify API error {resp.status}: {err_text}")
                return tracks, playlist_name, None

            # ── First attempt ──────────────────────────────────────────────────
            tracks, name, err_status = await fetch_tracks(access_token)

            # ── Handle auth errors ─────────────────────────────────────────────
            if tracks is None:
                if err_status == 401 and refresh_token:
                    # Token expired — refresh and retry once
                    add_log("Spotify access token expired, refreshing...", "WARNING")
                    new_token = await refresh_spotify_token(client_id, client_secret, refresh_token)
                    if new_token:
                        tracks, name, err_status = await fetch_tracks(new_token)

                # After refresh attempt (or if no refresh token), still failing
                if tracks is None:
                    if err_status == 403 or not using_user_token:
                        # 403 = Spotify is rejecting our token type entirely.
                        # This is expected when using client_credentials since Spotify's
                        # 2024 policy change removed client credentials access to playlist
                        # endpoints. A user OAuth token is now required.
                        msg = "Spotify login required for this playlist. Please log in via Settings."
                        add_log(msg, "WARNING")
                        return web.json_response({
                            "error": msg,
                            "requires_spotify_login": True
                        }, status=403)
                    else:
                        # 401 after refresh failed — token may be revoked
                        msg = (
                            "Spotify session expired and could not be refreshed. "
                            "Please re-login via Settings → Spotify."
                        )
                        add_log(msg, "WARNING")
                        return web.json_response({
                            "error": msg,
                            "requires_spotify_login": True
                        }, status=401)

            add_log(f"Successfully fetched {len(tracks)} tracks from Spotify API.")
            return web.json_response({"tracks": tracks, "title": name})

    except Exception as e:
        logger.exception("Spotify API handling failed")
        return web.json_response({"error": f"Spotify API handling failed: {str(e)}"}, status=500)

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
        if not DOWNLOAD_STATUS["queue"] and not DOWNLOAD_STATUS["active"]:
            DOWNLOAD_STATUS["batch_total"] = 1
            DOWNLOAD_STATUS["batch_completed"] = 0
        else:
            DOWNLOAD_STATUS["batch_total"] += 1
            
        DOWNLOAD_STATUS["queue"].append(item)
        save_status()
    await broadcast()
    return web.json_response({"ok": True, "id": item["id"]})


_AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".ogg", ".opus"}

def is_already_downloaded(artist: str, title: str, playlist_name: str | None) -> bool:
    """Return True if the expected output directory already contains an audio file."""
    label = sanitize_filename(f"{artist} - {title}" if artist else title)
    if playlist_name:
        out_dir = PLAYLISTS_DIR / sanitize_filename(str(playlist_name)) / label
    else:
        out_dir = SINGLES_DIR / label
    if not out_dir.exists():
        return False
    return any(f.suffix.lower() in _AUDIO_EXTS for f in out_dir.iterdir() if f.is_file())


async def check_downloaded(request):
    body = await request.json()
    tracks = body.get("tracks", [])
    results = []
    for t in tracks:
        already = is_already_downloaded(t.get("artist", ""), t.get("title", ""), t.get("playlist_name"))
        results.append({"title": t.get("title"), "artist": t.get("artist"), "playlist_name": t.get("playlist_name"), "downloaded": already})
    return web.json_response(results)


async def download_playlist(request):
    body = await request.json()
    tracks = body.get("tracks", [])
    ids = []
    skipped = 0
    async with DOWNLOAD_LOCK:
        to_queue = []
        for t in tracks:
            if is_already_downloaded(t.get("artist", ""), t.get("title", ""), t.get("playlist_name")):
                skipped += 1
            else:
                to_queue.append(t)

        if not DOWNLOAD_STATUS["queue"] and not DOWNLOAD_STATUS["active"]:
            DOWNLOAD_STATUS["batch_total"] = len(to_queue)
            DOWNLOAD_STATUS["batch_completed"] = 0
        else:
            DOWNLOAD_STATUS["batch_total"] += len(to_queue)

        for t in to_queue:
            p_name = t.get("playlist_name")
            item = {
                "id": str(uuid.uuid4()),
                "title": t.get("title", "Unknown"),
                "artist": t.get("artist", ""),
                "deezer_id": t.get("deezer_id"),
                "cover": t.get("cover", ""),
                "playlist_name": p_name,
                "status": "pending",
                "queued_at": time.time(),
                "type": "playlist" if p_name else "single",
            }
            DOWNLOAD_STATUS["queue"].append(item)
            ids.append(item["id"])
        save_status()
    await broadcast()
    return web.json_response({"ok": True, "ids": ids, "count": len(ids), "skipped": skipped})


async def clear_queue(request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    clear_all = body.get("all", False)
    async with DOWNLOAD_LOCK:
        DOWNLOAD_STATUS["queue"] = []
        DOWNLOAD_STATUS["batch_total"] = 0
        DOWNLOAD_STATUS["batch_completed"] = 0
        if clear_all:
            DOWNLOAD_STATUS["completed"] = []
            DOWNLOAD_STATUS["failed"] = []
        save_status()
    await broadcast()
    return web.json_response({"ok": True})


async def remove_from_queue(request):
    try:
        body = await request.json()
        tid = body.get("id")
        if not tid:
            return web.json_response({"error": "No ID provided"}, status=400)
        
        async with DOWNLOAD_LOCK:
            # Check queue
            original_len = len(DOWNLOAD_STATUS["queue"])
            DOWNLOAD_STATUS["queue"] = [x for x in DOWNLOAD_STATUS["queue"] if x["id"] != tid]
            removed = len(DOWNLOAD_STATUS["queue"]) < original_len
            
            # Note: Removing from "active" is trickier as a process is running. 
            # For now we'll just remove it from the list so it doesn't show in UI.
            # Real cancellation would require tracking task objects.
            original_active_len = len(DOWNLOAD_STATUS["active"])
            DOWNLOAD_STATUS["active"] = [x for x in DOWNLOAD_STATUS["active"] if x["id"] != tid]
            removed = removed or (len(DOWNLOAD_STATUS["active"]) < original_active_len)
            
            if removed:
                save_status()
                
        await broadcast()
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def stop_downloads(request):
    async with DOWNLOAD_LOCK:
        DOWNLOAD_STATUS["queue"] = []
        DOWNLOAD_STATUS["active"] = []
        DOWNLOAD_STATUS["batch_total"] = 0
        DOWNLOAD_STATUS["batch_completed"] = 0
        save_status()
    await broadcast()
    return web.json_response({"ok": True})


async def retry_track(request):
    try:
        body = await request.json()
        tid = body.get("id")
        if not tid:
            return web.json_response({"error": "No ID provided"}, status=400)
        
        async with DOWNLOAD_LOCK:
            # Find in failed
            failed_item = next((x for x in DOWNLOAD_STATUS["failed"] if x["id"] == tid), None)
            if failed_item:
                # Remove from failed
                DOWNLOAD_STATUS["failed"] = [x for x in DOWNLOAD_STATUS["failed"] if x["id"] != tid]
                # Prepare for retry
                failed_item["status"] = "pending"
                failed_item["queued_at"] = time.time()
                # Clean up old timestamps
                failed_item.pop("finished_at", None)
                failed_item.pop("started_at", None)
                # Add back to queue
                DOWNLOAD_STATUS["queue"].append(failed_item)
                save_status()
                
        await broadcast()
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def track_cover(request):
    artist = request.rel_url.query.get("artist", "")
    title = request.rel_url.query.get("title", "")
    folder = request.rel_url.query.get("folder", "")

    # 1. Check local cover.jpg
    if folder:
        # folder is relative to DOWNLOADS_DIR
        base = DOWNLOADS_DIR / folder
        for name in ["cover.jpg", "cover.png", "folder.jpg", "folder.png"]:
            p = base / name
            if p.exists():
                return web.FileResponse(p)

    # 2. Fetch from Deezer (async, cached)
    if artist or title:
        data = await _fetch_cover_bytes(artist, title)
        if data:
            return web.Response(body=data, content_type="image/jpeg")

    # 3. Return placeholder SVG
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="250" height="250" viewBox="0 0 250 250">
  <rect width="250" height="250" fill="#1a1a2e"/>
  <circle cx="125" cy="125" r="60" fill="none" stroke="#6c63ff" stroke-width="3"/>
  <circle cx="125" cy="125" r="20" fill="#6c63ff"/>
  <text x="125" y="220" text-anchor="middle" fill="#6c63ff" font-size="12" font-family="monospace">♪ Music Vault</text>
</svg>"""
    return web.Response(text=svg, content_type="image/svg+xml")


def get_folder_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob('*') if f.is_file())


async def refresh_library_size():
    """Recompute DOWNLOADS_DIR size in a thread and push it via broadcast."""
    loop = asyncio.get_running_loop()
    size = await loop.run_in_executor(None, get_folder_size, DOWNLOADS_DIR)
    DOWNLOAD_STATUS["library_size"] = size
    await broadcast()


async def list_files(request):
    path_param = request.rel_url.query.get("path", "")
    base = DOWNLOADS_DIR
    target = (base / path_param).resolve()
    if not is_safe_path(target, base):
        return web.json_response({"error": "forbidden"}, status=403)
    if not target.exists():
        return web.json_response({"error": "not found"}, status=404)

    items = []
    try:
        # Use target.iterdir() but ensure paths are relative to DOWNLOADS_DIR for frontend
        for entry in sorted(target.iterdir(), key=lambda e: (e.is_file(), e.name.lower())):
            stat = entry.stat()
            rel_path = str(entry.relative_to(DOWNLOADS_DIR))
            items.append({
                "name": entry.name,
                "type": "file" if entry.is_file() else "dir",
                "size": stat.st_size if entry.is_file() else 0,
                "modified": stat.st_mtime,
                "ext": entry.suffix.lower() if entry.is_file() else "",
                "path": rel_path,
            })
    except PermissionError:
        pass

    # Disk usage
    total, used, free = shutil.disk_usage(str(base))
    folder_size = get_folder_size(base)
    
    return web.json_response({
        "items": items,
        "path": path_param,
        "folder_size": folder_size,
        "disk": {"total": total, "used": used, "free": free},
    })


async def rename_file(request):
    body = await request.json()
    rel = body.get("path", "")
    new_name = sanitize_filename(body.get("new_name", ""))
    src = (DOWNLOADS_DIR / rel).resolve()
    if not is_safe_path(src, DOWNLOADS_DIR):
        return web.json_response({"error": "forbidden"}, status=403)
    dst = src.parent / new_name
    src.rename(dst)
    return web.json_response({"ok": True})


async def delete_file(request):
    body = await request.json()
    rel = body.get("path", "")
    target = (DOWNLOADS_DIR / rel).resolve()
    if not is_safe_path(target, DOWNLOADS_DIR):
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
    if not is_safe_path(folder, DOWNLOADS_DIR) or not folder.is_dir():
        return web.json_response({"error": "invalid"}, status=400)

    zip_path = folder.parent / f"{folder.name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in folder.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(folder.parent))

    rel_zip = str(zip_path.relative_to(DOWNLOADS_DIR))
    return web.json_response({"ok": True, "zip_path": rel_zip})


async def zip_files_batch(request):
    try:
        body = await request.json()
        paths = body.get("paths", [])
        if not paths:
            return web.json_response({"error": "no paths provided"}, status=400)
            
        zip_name = f"batch_export_{int(time.time())}.zip"
        zip_path = DOWNLOADS_DIR / zip_name
        
        def create_batch_zip():
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in paths:
                    target = (DOWNLOADS_DIR / p).resolve()
                    if not is_safe_path(target, DOWNLOADS_DIR) or not target.exists():
                        continue
                    if target.is_dir():
                        for f in target.rglob("*"):
                            if f.is_file():
                                zf.write(f, f.relative_to(target.parent))
                    else:
                        zf.write(target, target.name)
                        
        await asyncio.to_thread(create_batch_zip)
        return web.json_response({"ok": True, "zip_path": zip_name})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def serve_file(request):
    rel = request.match_info.get("path", "")
    target = (DOWNLOADS_DIR / rel).resolve()
    if not is_safe_path(target, DOWNLOADS_DIR) or not target.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(target)


async def get_status(request):
    return web.json_response(DOWNLOAD_STATUS)


async def save_config(request):
    global DEEZER_MAX_QUALITY
    body = await request.json()
    arl = body.get("arl", "")
    quality = body.get("quality", "FLAC")
    spotify_id = body.get("spotify_id", "")
    spotify_secret = body.get("spotify_secret", "")
    spotify_redirect = body.get("spotify_redirect", "")

    # Keep existing tokens if not provided in the save (don't overwrite with empty)
    existing_content = read_config_raw()
    spotify_access_token = get_val_from_content(existing_content, "access_token", "spotify")
    spotify_refresh_token = get_val_from_content(existing_content, "refresh_token", "spotify")

    toml_content = f"""[deezer]
arl = "{arl}"

[spotify]
client_id = "{spotify_id}"
client_secret = "{spotify_secret}"
redirect_uri = "{spotify_redirect}"
access_token = "{spotify_access_token}"
refresh_token = "{spotify_refresh_token}"

[downloads]
folder = "{DOWNLOADS_DIR}"
quality = "{quality}"
"""
    write_config_raw(toml_content)
    DEEZER_MAX_QUALITY = None
    return web.json_response({"ok": True})

def get_val_from_content(content, key, section):
    if not content: return ""
    s_match = re.search(r'\[' + section + r'\](.*?)(?=\[|$)', content, re.DOTALL)
    if s_match:
        section_text = s_match.group(1)
        m = re.search(key + r'\s*=\s*["' + "'" + r']([^"' + "'" + r']+)["' + "'" + r']', section_text)
        if not m: m = re.search(key + r'\s*=\s*([^\s,]+)', section_text)
        return m.group(1).strip() if m else ""
    return ""

async def spotify_login(request):
    import urllib.parse
    content = read_config_raw()
    client_id = get_val_from_content(content, "client_id", "spotify")
    if not client_id:
        return web.json_response({"error": "Spotify Client ID not configured"}, status=400)

    # Use configured redirect_uri or auto-detect from request
    config_redirect = get_val_from_content(content, "redirect_uri", "spotify")
    if config_redirect:
        if not config_redirect.startswith(("http://", "https://")):
            config_redirect = "http://" + config_redirect
        if not config_redirect.rstrip("/").endswith("/api/spotify/callback"):
            config_redirect = config_redirect.rstrip("/") + "/api/spotify/callback"
    redirect_uri = config_redirect or f"http://{request.host}/api/spotify/callback"
    
    logger.info(f"Spotify Login: Using client_id={client_id[:5]}..., redirect_uri={redirect_uri}")
    
    scope = "playlist-read-private playlist-read-collaborative user-library-read"
    state = str(uuid.uuid4())

    params = {
        "response_type": "code",
        "client_id": client_id,
        "scope": scope,
        "redirect_uri": redirect_uri,
        "state": state
    }
    auth_url = f"https://accounts.spotify.com/authorize?{urllib.parse.urlencode(params)}"
    return web.HTTPFound(auth_url)

async def spotify_callback(request):
    code = request.query.get("code")
    error = request.query.get("error")
    if error:
        return web.Response(text=f"Spotify Auth Error: {error}", status=400)
    if not code:
        return web.Response(text="No code received", status=400)

    content = read_config_raw()
    client_id = get_val_from_content(content, "client_id", "spotify")
    client_secret = get_val_from_content(content, "client_secret", "spotify")
    config_redirect = get_val_from_content(content, "redirect_uri", "spotify")
    if config_redirect:
        if not config_redirect.startswith(("http://", "https://")):
            config_redirect = "http://" + config_redirect
        if not config_redirect.rstrip("/").endswith("/api/spotify/callback"):
            config_redirect = config_redirect.rstrip("/") + "/api/spotify/callback"
    redirect_uri = config_redirect or f"http://{request.host}/api/spotify/callback"

    auth_str = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://accounts.spotify.com/api/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri
            },
            headers={"Authorization": f"Basic {auth_str}"}
        ) as resp:
            if resp.status != 200:
                err = await resp.text()
                return web.Response(text=f"Failed to exchange token: {err}", status=400)

            data = await resp.json()
            access_token = data.get("access_token")
            refresh_token = data.get("refresh_token")
            logger.info(f"spotify_callback: Received access_token={access_token[:10]}..., refresh_token={refresh_token[:10] if refresh_token else 'NONE'}")

            # Update config with new tokens
            cfg_text = read_config_raw()
            logger.info(f"spotify_callback: Current config length: {len(cfg_text)}")
            # Surgical update to just tokens
            if 'access_token =' in cfg_text:
                new_cfg = re.sub(r'access_token\s*=\s*".*?"', f'access_token = "{access_token}"', cfg_text)
            else:
                new_cfg = cfg_text.replace('[spotify]', f'[spotify]\naccess_token = "{access_token}"')
            
            if 'refresh_token =' in new_cfg:
                new_cfg = re.sub(r'refresh_token\s*=\s*".*?"', f'refresh_token = "{refresh_token}"', new_cfg)
            else:
                new_cfg = new_cfg.replace('access_token =', f'refresh_token = "{refresh_token}"\naccess_token =')
                
            write_config_raw(new_cfg)
            logger.info("spotify_callback: Config updated with new tokens")

    return web.Response(text="Successfully logged into Spotify! You can close this window and try your import again.", content_type="text/html")

async def refresh_spotify_token(client_id, client_secret, refresh_token):
    logger.info(f"Refreshing Spotify token using refresh_token: {refresh_token[:10]}...")
    auth_str = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            headers={"Authorization": f"Basic {auth_str}"}
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                new_access = data.get("access_token")
                logger.info("Spotify token refresh successful")
                # Save new token
                cfg_text = read_config_raw()
                new_cfg = re.sub(r'access_token\s*=\s*".*?"', f'access_token = "{new_access}"', cfg_text)
                write_config_raw(new_cfg)
                return new_access
            else:
                err = await resp.text()
                logger.error(f"Spotify token refresh failed ({resp.status}): {err}")
    return None

async def get_config(request):
    cfg = {"arl": "", "quality": "FLAC", "spotify_id": "", "spotify_secret": "", "deps": {}}
    content = read_config_raw()
    if content:
        cfg["arl"] = get_val_from_content(content, "arl", "deezer")
        cfg["spotify_id"] = get_val_from_content(content, "client_id", "spotify")
        cfg["spotify_secret"] = get_val_from_content(content, "client_secret", "spotify")
        cfg["spotify_redirect"] = get_val_from_content(content, "redirect_uri", "spotify")

        val_q = get_val_from_content(content, "quality", "downloads")
        if val_q: cfg["quality"] = val_q
            
    # Check dependencies
    # Standard check
    rip_path = shutil.which("rip")
    # Fallback for pipx if not in immediate shutil path during runtime
    if not rip_path:
        pipx_rip = Path("/root/.local/bin/rip")
        if pipx_rip.exists():
            rip_path = str(pipx_rip)

    cfg["deps"]["streamrip"] = rip_path is not None
    cfg["deps"]["ytdlp"] = shutil.which("yt-dlp") is not None
    cfg["download_path"] = str(DOWNLOADS_DIR)
    
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
    DOWNLOAD_STATUS["library_size"] = get_folder_size(DOWNLOADS_DIR)
    await broadcast()


def create_app():
    app = web.Application()
    app.on_startup.append(on_startup)

    # Routes
    app.router.add_get("/ws/status", ws_handler)
    app.router.add_get("/api/search/suggestions", search_suggestions)
    app.router.add_post("/api/search/playlist", search_playlist)
    app.router.add_get("/api/spotify/login", spotify_login)
    app.router.add_get("/api/spotify/callback", spotify_callback)
    app.router.add_post("/api/download/single", download_single)
    app.router.add_post("/api/download/playlist", download_playlist)
    app.router.add_post("/api/download/check", check_downloaded)
    app.router.add_post("/api/download/clear", clear_queue)
    app.router.add_post("/api/download/stop", stop_downloads)
    app.router.add_post("/api/download/retry", retry_track)
    app.router.add_post("/api/download/remove", remove_from_queue)
    app.router.add_get("/api/track-cover", track_cover)
    app.router.add_get("/api/files", list_files)
    app.router.add_post("/api/files/rename", rename_file)
    app.router.add_post("/api/files/delete", delete_file)
    app.router.add_post("/api/files/zip", zip_folder)
    app.router.add_post("/api/files/zip/batch", zip_files_batch)
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