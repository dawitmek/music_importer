#!/usr/bin/env python3
"""Music Vault Backend Server - Music Downloader & Library Manager"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import time
import uuid
import zipfile
from collections import OrderedDict
from pathlib import Path
from typing import Optional
from datetime import datetime

import requests
from aiohttp import web, WSMsgType
import aiohttp

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
STAGING_FILE = DATA_DIR / "staging.json"
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

# ── File helpers ─────────────────────────────────────────────────────────────
def atomic_write(path: Path, text: str):
    """Write via a temp file + rename, so a crash mid-write can't leave a
    truncated file behind. status.json is rewritten constantly and start.sh
    kills the server with SIGKILL, which made this a real risk."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def chmod_private(path: Path):
    """Best-effort 0600. These files hold account credentials."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ── Encryption Helper ────────────────────────────────────────────────────────
# Config at rest is encrypted with the vault key. When `cryptography` is
# installed we use Fernet (AES-128-CBC + HMAC); otherwise we fall back to the
# legacy repeating-key XOR, which is obfuscation rather than encryption — it is
# trivially recovered given known plaintext like `[deezer]`. Ciphertext carries
# a prefix naming the scheme that wrote it so both can always be read back.
try:
    from cryptography.fernet import Fernet
    HAVE_FERNET = True
except ImportError:  # pragma: no cover - depends on the install
    Fernet = None
    HAVE_FERNET = False

_ENC_FERNET = "MVF1:"
_ENC_XOR = "MVX1:"


class ConfigUnreadableError(Exception):
    """config.toml exists but no available key decrypts it.

    Callers must not treat this as "no config" — overwriting would destroy the
    stored Spotify refresh token and the ARL.
    """


class Vault:
    _memory_key: Optional[bytes] = None

    @staticmethod
    def _norm(raw: bytes) -> bytes:
        return raw.ljust(32, b"\0")[:32]

    @classmethod
    def candidate_keys(cls) -> list:
        """All keys available on this machine, as (source, key) pairs.

        We try every candidate rather than picking one by precedence: if a host
        has both a .vaultkey file and MUSIC_VAULT_KEY set, the right key is
        whichever one actually decrypts the existing config, not whichever we
        happen to check first.
        """
        out, seen = [], set()

        def add(source: str, raw: Optional[bytes]):
            if not raw:
                return
            key = cls._norm(raw)
            if key in seen:
                return
            seen.add(key)
            out.append((source, key))

        add("memory", cls._memory_key)
        secret_path = Path("/run/secrets/MUSIC_VAULT_KEY")
        if secret_path.exists():
            try:
                add("docker-secret", secret_path.read_bytes().strip())
            except Exception as e:
                logger.error(f"Failed to read vault key from Docker secret: {e}")
        env_key = os.environ.get("MUSIC_VAULT_KEY")
        if env_key:
            add("env:MUSIC_VAULT_KEY", env_key.encode())
        if KEY_FILE.exists():
            try:
                add("file:.vaultkey", KEY_FILE.read_bytes().strip())
            except Exception as e:
                logger.error(f"Failed to read vault key from file: {e}")
        return out

    @classmethod
    def _get_key(cls) -> bytes:
        if cls._memory_key:
            return cls._memory_key

        candidates = cls.candidate_keys()
        if candidates:
            cls._memory_key = candidates[0][1]
            return cls._memory_key

        # Nothing configured — generate one so the app works out of the box
        key = os.urandom(32)
        try:
            KEY_FILE.write_bytes(key)
            chmod_private(KEY_FILE)
            logger.info(f"Generated new vault key and saved to {KEY_FILE}")
        except Exception as e:
            logger.error(
                f"Failed to save vault key ({e}) — falling back to a transient key. "
                "Encrypted settings will NOT be readable after a restart."
            )
        cls._memory_key = key
        return key

    @staticmethod
    def _xor(raw: bytes, key: bytes) -> bytes:
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(raw))

    @classmethod
    def _fernet(cls, key: bytes):
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key).digest()))

    @classmethod
    def encrypt(cls, data: str) -> str:
        if not data:
            return ""
        key = cls._get_key()
        if HAVE_FERNET:
            return _ENC_FERNET + cls._fernet(key).encrypt(data.encode()).decode()
        return _ENC_XOR + base64.b64encode(cls._xor(data.encode(), key)).decode()

    @classmethod
    def decrypt_with(cls, data: str, key: bytes) -> Optional[str]:
        """Decrypt using one specific key. Returns None if that key doesn't fit."""
        try:
            if data.startswith(_ENC_FERNET):
                if not HAVE_FERNET:
                    return None
                return cls._fernet(key).decrypt(data[len(_ENC_FERNET):].encode()).decode()
            # Bare base64 is the pre-prefix legacy XOR format
            payload = data[len(_ENC_XOR):] if data.startswith(_ENC_XOR) else data
            return cls._xor(base64.b64decode(payload), key).decode()
        except Exception:
            return None


def _looks_like_config(text: str) -> bool:
    return any(marker in text for marker in ("[deezer]", "[downloads]", "[spotify]"))


def read_config_raw() -> str:
    """Decrypt and return config.toml.

    Raises ConfigUnreadableError when the file exists but cannot be decrypted,
    so a lost or mismatched vault key surfaces as a loud error instead of
    silently looking like an empty config.
    """
    if not CONFIG_FILE.exists():
        return ""
    content = CONFIG_FILE.read_text().strip()
    if not content:
        return ""

    # Plaintext (hand-edited, or written by start.sh's default template)
    if _looks_like_config(content):
        return content

    for source, key in Vault.candidate_keys():
        plain = Vault.decrypt_with(content, key)
        if plain and _looks_like_config(plain):
            if Vault._memory_key != key:
                logger.info(f"Config decrypted with vault key from {source}")
                Vault._memory_key = key  # keep writing with the key that works
            return plain

    raise ConfigUnreadableError(
        f"{CONFIG_FILE} could not be decrypted with any available vault key "
        "(config/.vaultkey, /run/secrets/MUSIC_VAULT_KEY, $MUSIC_VAULT_KEY). "
        "Restore the original key to recover these settings — saving new ones "
        "will overwrite them."
    )


def read_config_safe() -> str:
    """read_config_raw() for read-only callers that can tolerate a failure."""
    try:
        return read_config_raw()
    except ConfigUnreadableError as e:
        logger.error(str(e))
        return ""


def write_config_raw(content: str):
    atomic_write(CONFIG_FILE, Vault.encrypt(content))
    chmod_private(CONFIG_FILE)


def toml_escape(value) -> str:
    """Escape a value for a TOML basic string. Without this, an ARL or secret
    containing a quote or newline silently corrupts (or injects into) the file."""
    out = str(value or "")
    out = out.replace("\\", "\\\\").replace('"', '\\"')
    return out.replace("\r", "").replace("\n", "\\n").replace("\t", "\\t")


def toml_unescape(value: str) -> str:
    out = value.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t")
    return out.replace("\\\\", "\\")

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

# History is capped: `completed` used to grow without bound, and the whole dict
# is both written to disk and pushed to every websocket client on every tick.
MAX_COMPLETED = 200
MAX_FAILED = 200
MAX_LOGS = 500
# Clients only need the tail of the history, not all of it, on every update
BROADCAST_COMPLETED = 25
BROADCAST_LOGS = 100

DEFAULT_STATUS: dict = {
    "queue": [],
    "active": [],
    "completed": [],
    "failed": [],
    "logs": [],
    "batch_total": 0,
    "batch_completed": 0,
    "completed_total": 0,
    "last_batch_finished_at": None,
    "library_size": 0,
    "is_paused": False,
}
DOWNLOAD_STATUS: dict = json.loads(json.dumps(DEFAULT_STATUS))

DEEZER_MAX_QUALITY = None  # Persistent memory for account capability (0, 1, 2, 3)
DOWNLOAD_LOCK = asyncio.Lock()

# tid -> the subprocess currently running for it, so Stop can actually
# terminate downloads instead of only hiding them from the UI.
ACTIVE_PROCS: dict = {}
CANCELLED: set = set()

_LOG_SEQ = 0


def trim_status():
    DOWNLOAD_STATUS["completed"] = DOWNLOAD_STATUS["completed"][-MAX_COMPLETED:]
    DOWNLOAD_STATUS["failed"] = DOWNLOAD_STATUS["failed"][-MAX_FAILED:]
    DOWNLOAD_STATUS["logs"] = DOWNLOAD_STATUS["logs"][-MAX_LOGS:]


def load_status():
    global DOWNLOAD_STATUS, _LOG_SEQ
    DOWNLOAD_STATUS = json.loads(json.dumps(DEFAULT_STATUS))
    if not STATUS_FILE.exists():
        return
    try:
        saved = json.loads(STATUS_FILE.read_text())
    except Exception as e:
        logger.error(f"status.json is unreadable ({e}) — starting from a clean state")
        return
    if not isinstance(saved, dict):
        return

    # Merge over the defaults so a file written by an older version can never
    # leave a required key missing (which used to KeyError at runtime)
    for key, value in saved.items():
        if key in DOWNLOAD_STATUS:
            DOWNLOAD_STATUS[key] = value

    # Downloads that were mid-flight at shutdown are orphaned. Move them to the
    # failed list rather than dropping them, so they stay visible and retryable
    # and the batch counter can still reach 100%.
    orphaned = [x for x in (DOWNLOAD_STATUS.get("active") or []) if isinstance(x, dict)]
    for item in orphaned:
        item["status"] = "failed"
        item["error"] = "interrupted by server restart"
        item["finished_at"] = time.time()
    if orphaned:
        DOWNLOAD_STATUS["failed"].extend(orphaned)
        DOWNLOAD_STATUS["batch_completed"] += len(orphaned)
        logger.warning(f"Recovered {len(orphaned)} interrupted download(s) into the failed list")
    DOWNLOAD_STATUS["active"] = []

    # Seed the running total from history the first time we load a file written
    # before this counter existed
    if not DOWNLOAD_STATUS["completed_total"]:
        DOWNLOAD_STATUS["completed_total"] = len(DOWNLOAD_STATUS["completed"])

    # Nothing pending means the previous batch is over, so its counters carry no
    # meaning — clear them rather than inheriting drift into the next batch
    if not DOWNLOAD_STATUS["queue"]:
        DOWNLOAD_STATUS["batch_total"] = 0
        DOWNLOAD_STATUS["batch_completed"] = 0

    _LOG_SEQ = max([e.get("seq", 0) for e in DOWNLOAD_STATUS["logs"] if isinstance(e, dict)] or [0])
    trim_status()


def save_status():
    trim_status()
    atomic_write(STATUS_FILE, json.dumps(DOWNLOAD_STATUS))


# The staging area is kept out of DOWNLOAD_STATUS so it isn't broadcast on every
# download tick — it changes only when the user edits it.
STAGING: list = []


def load_staging():
    global STAGING
    if STAGING_FILE.exists():
        try:
            STAGING = json.loads(STAGING_FILE.read_text())
        except Exception:
            pass


def save_staging():
    STAGING_FILE.write_text(json.dumps(STAGING, indent=2))


def add_log(msg: str, level: str = "INFO"):
    global _LOG_SEQ
    _LOG_SEQ += 1
    # seq lets clients append only what's new instead of re-rendering every line
    entry = {"seq": _LOG_SEQ, "ts": time.time(), "msg": msg, "level": level}
    DOWNLOAD_STATUS["logs"] = DOWNLOAD_STATUS["logs"][-(MAX_LOGS - 1):] + [entry]
    logger.info(msg)


# ── WebSocket broadcast ────────────────────────────────────────────────────────
def broadcast_payload() -> dict:
    """Trimmed view of the status for websocket pushes.

    The full history reached hundreds of KB; sending all of it on every download
    tick, to every client, was by far the largest source of traffic here.
    """
    return {
        **DOWNLOAD_STATUS,
        "completed": DOWNLOAD_STATUS["completed"][-BROADCAST_COMPLETED:],
        "logs": DOWNLOAD_STATUS["logs"][-BROADCAST_LOGS:],
    }


async def broadcast(event: str = "status"):
    global WS_CLIENTS
    if not WS_CLIENTS:
        return
    dead = set()
    payload = json.dumps({"event": event, "data": broadcast_payload()})
    # Iterate over a copy of the set to avoid RuntimeError: Set changed size during iteration
    for ws in list(WS_CLIENTS):
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
        await ws.send_str(json.dumps({"event": "status", "data": broadcast_payload()}))
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        WS_CLIENTS.discard(ws)
        logger.info(f"WS client disconnected. Total: {len(WS_CLIENTS)}")
    return ws


# ── Access control ─────────────────────────────────────────────────────────────
# Off by default so existing local setups keep working. Set MV_AUTH_TOKEN to
# require a shared token — strongly recommended if this port is reachable beyond
# localhost, since the API exposes credentials and can delete from the library.
AUTH_TOKEN = os.environ.get("MV_AUTH_TOKEN", "").strip()
SECRET_MASK = "••••••••"

# Paths reachable without a token: the Spotify redirect (Spotify can't send our
# header) and the static shell that lets a user present ?token=…
_AUTH_EXEMPT = {"/api/spotify/callback"}


@web.middleware
async def auth_middleware(request, handler):
    if not AUTH_TOKEN or request.path in _AUTH_EXEMPT:
        return await handler(request)

    query_token = request.query.get("token", "")
    supplied = (
        request.headers.get("X-Auth-Token", "")
        or query_token
        or request.cookies.get("mv_auth", "")
    )
    if not (supplied and hmac.compare_digest(supplied, AUTH_TOKEN)):
        return web.json_response(
            {"error": "unauthorized — open this page once as /?token=YOUR_TOKEN"},
            status=401,
        )

    response = await handler(request)
    # Presenting the token in the URL once establishes a session cookie
    if query_token and hasattr(response, "set_cookie"):
        response.set_cookie(
            "mv_auth", AUTH_TOKEN, httponly=True, samesite="Lax", max_age=30 * 24 * 3600
        )
    return response


# ── OAuth state ────────────────────────────────────────────────────────────────
_OAUTH_STATES: dict = {}
_OAUTH_STATE_TTL = 600


def new_oauth_state() -> str:
    now = time.time()
    for state, created in list(_OAUTH_STATES.items()):
        if now - created > _OAUTH_STATE_TTL:
            _OAUTH_STATES.pop(state, None)
    state = secrets.token_urlsafe(24)
    _OAUTH_STATES[state] = now
    return state


def consume_oauth_state(state: str) -> bool:
    """Single-use check that a callback belongs to a login we started."""
    created = _OAUTH_STATES.pop(state or "", None)
    return created is not None and (time.time() - created) <= _OAUTH_STATE_TTL


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
    name = re.sub(r'[<>:"/\\|?*#]', "", name or "")
    name = re.sub(r"\s+", " ", name).strip()
    # Leading/trailing dots are hidden files or filesystem-hostile on some platforms
    name = name.strip(". ")
    # Never return "": that would resolve to the parent directory, dumping the
    # download into downloads/singles itself and making the "already downloaded"
    # check match every track.
    return name[:200] or "untitled"


def clean_track_title(title: str) -> str:
    """
    Removes common YouTube/Spotify fluff like (Official Video), [Prod by ...], etc.
    This ensures clean filenames and improves matching when searching Deezer for high-quality audio.
    """
    if not title: return ""
    
    # 1. Remove tags in brackets/parentheses
    # Repeatedly loops to catch multiple adjacent tags like "(Dir by X)(Produced by Y)"
    pattern = r'(?i)\s*[\[\(](official.*?|music\s+video|video|audio|lyric.*?|visualizer|prod\..*?|produced.*?|best\s+version|slowed.*?|reverb.*?|(?:shot|directed|dir\.?)\s*by.*?)[\]\)]'
    while re.search(pattern, title):
        title = re.sub(pattern, '', title)

    # 2. Remove trailing "Shot By" or "Directed By" or "Dir by" that aren't wrapped in brackets
    title = re.sub(r'(?i)\s*(?:shot|directed|dir\.?)\s*by\s*@?.*$', '', title)
    
    # 3. Remove "- Official Video" or "- Prod. by" appended at the very end
    title = re.sub(r'(?i)\s*-\s*(official.*?|prod\..*?|produced.*?).*$', '', title)
    
    # 4. Clean up any leftover trailing dashes or whitespace that the previous steps left behind
    title = re.sub(r'\s*-\s*$', '', title)
    
    # 5. Remove surrounding quotes if the entire title is wrapped in them (e.g. '"Song Name"')
    title = re.sub(r'^["\'](.*)["\']$', r'\1', title)
    
    return title.strip()

def normalize_features(artist_str: str, title_str: str, channel_name: str = "") -> tuple:
    """
    Intelligently separates main artist from featured artists, placing the features cleanly into the title.
    When a title has multiple artists (e.g., A & B), it uses the YouTube channel name to figure out
    who the primary artist is.
    """
    main_artist = artist_str
    features = ""
    
    # 1. First, check if features are explicitly in the title: "Song (feat. X)"
    feat_match = re.search(r'(?i)[\[\(](?:feat\.?|ft\.?|featuring)\s+(.*?)[\]\)]', title_str)
    if feat_match:
        features = feat_match.group(1).strip()
        # Remove from title temporarily
        title_str = re.sub(r'(?i)\s*[\[\(](?:feat\.?|ft\.?|featuring)\s+(.*?)[\]\)]', '', title_str).strip()
        
    # 2. If no features in title, check if artist string is a list (e.g. "Artist1 & Artist2")
    if not features and artist_str:
        # Split by common delimiters
        parts = re.split(r'(?i)\s+(?:&|x|ft\.?|feat\.?|featuring|,)\s+', artist_str)
        if len(parts) > 1:
            # We have multiple artists. Try to use channel name to find the main one.
            if channel_name:
                for i, p in enumerate(parts):
                    # If part matches channel name (or close enough), it's the main artist
                    if p.lower() in channel_name.lower() or channel_name.lower() in p.lower():
                        main_artist = p.strip()
                        features = ", ".join([x.strip() for j, x in enumerate(parts) if j != i])
                        break
                else:
                    # If channel doesn't match any, assume first is main
                    main_artist = parts[0].strip()
                    features = ", ".join([x.strip() for x in parts[1:]])
            else:
                main_artist = parts[0].strip()
                features = ", ".join([x.strip() for x in parts[1:]])
                
    # 3. Format output
    # If we found features, append them cleanly to the title
    if features:
        title_str = f"{title_str} (feat. {features})"
        
    return main_artist, title_str


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


# LRU cache for covers: key=(artist, title) -> image bytes or None. Bounded by
# both entry count and total bytes, since these are full JPEGs.
_COVER_CACHE: "OrderedDict[tuple, Optional[bytes]]" = OrderedDict()
_COVER_CACHE_MAX = 500
_COVER_CACHE_MAX_BYTES = 32 * 1024 * 1024
_cover_cache_bytes = 0


def _cover_cache_put(key: tuple, value: Optional[bytes]):
    global _cover_cache_bytes
    if key in _COVER_CACHE:
        _cover_cache_bytes -= len(_COVER_CACHE.pop(key) or b"")
    _COVER_CACHE[key] = value
    _cover_cache_bytes += len(value or b"")
    while _COVER_CACHE and (
        len(_COVER_CACHE) > _COVER_CACHE_MAX or _cover_cache_bytes > _COVER_CACHE_MAX_BYTES
    ):
        _, evicted = _COVER_CACHE.popitem(last=False)
        _cover_cache_bytes -= len(evicted or b"")


async def _fetch_cover_bytes(artist: str, title: str) -> Optional[bytes]:
    """Async: look up cover URL from Deezer and fetch image bytes. Uses in-memory cache."""
    cache_key = (artist.lower().strip(), title.lower().strip())
    if cache_key in _COVER_CACHE:
        _COVER_CACHE.move_to_end(cache_key)  # keep it hot
        return _COVER_CACHE[cache_key]

    # Look up URL via Deezer search (still sync, so keep it off the event loop)
    url = await asyncio.to_thread(deezer_cover_url, artist, title)

    result = None
    if url:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        result = await r.read()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    _cover_cache_put(cache_key, result)
    return result


def read_arl() -> Optional[str]:
    # Check env first
    arl = os.environ.get("DEEZER_ARL", "").strip()
    if arl:
        return arl
    # Read from our config.toml
    val = get_val_from_content(read_config_safe(), "arl", "deezer").strip()
    return val or None


# ── Download engine ────────────────────────────────────────────────────────────
def find_rip() -> Optional[str]:
    """Locate the streamrip CLI, including the usual pipx install locations."""
    found = shutil.which("rip")
    if found:
        return found
    for candidate in (
        Path.home() / ".local" / "bin" / "rip",
        Path("/root/.local/bin/rip"),
        Path("/usr/local/bin/rip"),
    ):
        try:
            if candidate.exists():
                return str(candidate)
        except OSError:
            continue
    return None


def kill_download(tid: str):
    """Terminate the subprocess for a download and mark it cancelled so the
    pipeline doesn't move on to the next fallback."""
    CANCELLED.add(tid)
    proc = ACTIVE_PROCS.get(tid)
    if proc is not None and proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.warning(f"Failed to kill subprocess for {tid}: {e}")


async def _run_tracked(tid: str, cmd: list, timeout: int) -> tuple:
    """Run a subprocess registered against `tid` so it can be cancelled.

    Returns (returncode, stdout, stderr). On timeout the process is killed
    rather than left running detached, which is what `wait_for` alone did.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if tid:
        ACTIVE_PROCS[tid] = proc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, stdout or b"", stderr or b""
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        raise
    finally:
        if tid:
            ACTIVE_PROCS.pop(tid, None)


AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".ogg", ".opus"}
# Errors that specifically mean "this account can't have this bitrate". Anything
# else (network, region block, bad ARL) must NOT trigger a quality step-down.
QUALITY_REJECTED = (
    "not available for your account",
    "does not support",
    "codec not available",
    "not authorized",
)


async def run_streamrip(track_id: str, out_dir: Path, tid: str = "") -> bool:
    """
    Attempts to download a track from Deezer using streamrip.
    It will automatically 'step down' in quality if the user's account doesn't support
    the requested bitrate (e.g. trying to download FLAC on a free account).
    """
    global DEEZER_MAX_QUALITY
    arl = read_arl()
    if not arl:
        logger.warning("No ARL configured — skipping streamrip")
        return False

    rip_cmd = find_rip()
    if not rip_cmd:
        add_log("The 'rip' command was not found. Using YouTube fallback.", "ERROR")
        return False

    # Get user's preferred quality from config
    user_quality = 1 # Default 320kbps
    q_str = get_val_from_content(read_config_safe(), "quality", "downloads")
    if q_str == "FLAC": user_quality = 2
    elif q_str == "MP3_320": user_quality = 1
    elif q_str == "MP3_128": user_quality = 0

    # Start from the lower of (User Preference) vs (Last Known Max Capability)
    starting_quality = user_quality
    if DEEZER_MAX_QUALITY is not None:
        starting_quality = min(user_quality, DEEZER_MAX_QUALITY)

    # Qualities to try in descending order (2=FLAC, 1=320, 0=128)
    qualities_to_try = [q for q in [2, 1, 0] if q <= starting_quality]

    # Unique per download: concurrent downloads sharing one config file could
    # overwrite each other's output folder between the write and the exec,
    # landing one track's audio in another track's directory.
    sr_config = CONFIG_DIR / f"streamrip_{uuid.uuid4().hex}.toml"
    stepped_down = False

    try:
        for q in qualities_to_try:
            if tid and tid in CANCELLED:
                return False
            add_log(f"Attempting Deezer download at quality level {q}...")

            try:
                cfg_text = STREAMRIP_CONFIG_TEMPLATE.replace("__ARL__", arl) \
                                                  .replace("__FOLDER__", str(out_dir)) \
                                                  .replace("__QUALITY__", str(q))
                sr_config.write_text(cfg_text)
                chmod_private(sr_config)  # contains the ARL in plaintext
            except Exception as e:
                logger.warning(f"Failed to write streamrip config: {e}")
                return False

            cmd = [rip_cmd, "--config-path", str(sr_config), "url",
                   f"https://www.deezer.com/track/{track_id}"]
            try:
                returncode, stdout, stderr = await _run_tracked(tid, cmd, timeout=180)
                out = (stdout + stderr).decode(errors="replace")

                if returncode != 0:
                    if tid and tid in CANCELLED:
                        return False
                    is_quality_issue = any(k in out.lower() for k in QUALITY_REJECTED)
                    if is_quality_issue and q != qualities_to_try[-1]:
                        add_log(f"Quality {q} not supported by this account. Stepping down...", "WARNING")
                        stepped_down = True
                        continue
                    # Not a quality problem — stepping down wouldn't help, and
                    # succeeding at a lower level would wrongly pin the account
                    # to that bitrate for the rest of the session.
                    add_log(f"streamrip exited {returncode} at quality {q}. Output: {out[:300]}", "WARNING")
                    return False

                # Verify a file appeared
                if any(f.suffix.lower() in AUDIO_EXTS for f in out_dir.rglob("*") if f.is_file()):
                    # Only conclude anything about the account's ceiling if we
                    # actually saw it reject a higher quality
                    if stepped_down and DEEZER_MAX_QUALITY is None:
                        DEEZER_MAX_QUALITY = q
                        add_log(f"Account capability locked to quality level {q}")
                    return True

                add_log(f"streamrip exited cleanly at quality {q} but produced no audio", "WARNING")
                return False

            except FileNotFoundError:
                add_log("The 'rip' command was not found in the system PATH. Using YouTube fallback.", "ERROR")
                return False
            except asyncio.TimeoutError:
                # A timeout says nothing about quality, so don't step down
                logger.warning(f"streamrip timed out at quality {q}")
                return False
            except Exception as e:
                logger.error(f"streamrip error at quality {q}: {e}")
                return False

        return False
    finally:
        try:
            sr_config.unlink(missing_ok=True)
        except OSError:
            pass


async def run_ytdlp(query: str, out_dir: Path, filename: str, metadata_artist: str = "", metadata_title: str = "", metadata_album: str = "", tid: str = "") -> bool:
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
        "--embed-metadata",
    ]

    if metadata_artist:
        cmd.extend(["--parse-metadata", f"{metadata_artist}:%(artist)s"])
    if metadata_title:
        cmd.extend(["--parse-metadata", f"{metadata_title}:%(title)s"])
    if metadata_album:
        cmd.extend(["--parse-metadata", f"{metadata_album}:%(album)s"])

    try:
        returncode, _, _ = await _run_tracked(tid, cmd, timeout=300)
        if returncode != 0:
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


async def _attempt_download(item: dict, out_dir: Path) -> tuple:
    """Run the Deezer → YouTube pipeline. Returns (success, method)."""
    tid = item["id"]
    artist = item.get("artist", "")
    title = item.get("title", "")
    album = item.get("album", "")
    deezer_id = item.get("deezer_id")

    # Step 1: Try streamrip (Deezer)
    # We prioritize downloading the official, high-bitrate studio file from Deezer.
    # If the track came from YouTube, it won't have a numeric deezer_id, so we must search for it first.
    is_valid_deezer_id = str(deezer_id).isdigit() if deezer_id else False

    if not is_valid_deezer_id:
        # Try to find a real Deezer ID if we only have title/artist or a non-numeric ID
        search_query = f"{artist} {title}"
        add_log(f"Searching Deezer ID for: {search_query}")
        search_results = await asyncio.to_thread(deezer_search, search_query, 1)
        if search_results:
            deezer_id = search_results[0].get("id")
            add_log(f"Found Deezer ID: {deezer_id}")
            is_valid_deezer_id = True
        else:
            add_log(f"No Deezer ID found for {search_query}", "WARNING")

    if tid in CANCELLED:
        return False, "cancelled"

    if is_valid_deezer_id and deezer_id:
        add_log(f"Trying Deezer (streamrip) for track {deezer_id}")
        if await run_streamrip(str(deezer_id), out_dir, tid=tid):
            add_log(f"✓ Deezer download succeeded: {title}")
            return True, "deezer"

    if tid in CANCELLED:
        return False, "cancelled"

    # Step 2: Fallback to yt-dlp (YouTube)
    # If Deezer doesn't have the track (e.g. an unreleased leak or underground mix), or
    # if streamrip fails due to regional blocks, we gracefully fallback to ripping the audio from YouTube.
    add_log(f"Falling back to yt-dlp for: {artist} - {title}", "INFO")
    query = f"{artist} {title} official audio" if artist else title
    if await run_ytdlp(
        query, out_dir, f"{artist} - {title}" if artist else title,
        metadata_artist=artist, metadata_title=title, metadata_album=album, tid=tid,
    ):
        add_log(f"✓ YouTube fallback succeeded: {title}")
        return True, "youtube"

    return False, "unknown"


async def process_download(item: dict):
    tid = item["id"]
    artist = item.get("artist", "")
    title = item.get("title", "")
    CANCELLED.discard(tid)

    # Determine output directory
    playlist_name = item.get("playlist_name")
    label = sanitize_filename(f"{artist} - {title}" if artist else title)
    if playlist_name:
        out_dir = PLAYLISTS_DIR / sanitize_filename(str(playlist_name)) / label
    else:
        out_dir = SINGLES_DIR / label

    success, method, error = False, "unknown", ""

    # Everything below runs under try/finally: if any step raises, the item MUST
    # still leave `active`, or it permanently occupies one of the three
    # concurrency slots and eventually deadlocks the queue.
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        add_log(f"Starting download: {artist} - {title}")
        await broadcast()
        success, method = await _attempt_download(item, out_dir)
    except Exception as e:
        logger.exception(f"Download crashed for {artist} - {title}")
        success, method, error = False, "error", str(e)
        add_log(f"✗ Download crashed: {artist} - {title} ({e})", "ERROR")
    finally:
        try:
            await _finalize_download(item, out_dir, success, method, error)
        except Exception:
            logger.exception("Failed to finalize download")


async def _finalize_download(item: dict, out_dir: Path, success: bool, method: str, error: str):
    tid = item["id"]
    artist = item.get("artist", "")
    title = item.get("title", "")
    cancelled = tid in CANCELLED
    CANCELLED.discard(tid)

    async with DOWNLOAD_LOCK:
        was_active = any(x.get("id") == tid for x in DOWNLOAD_STATUS["active"])
        DOWNLOAD_STATUS["active"] = [x for x in DOWNLOAD_STATUS["active"] if x.get("id") != tid]
        # Only count it if it was still part of the batch — a stopped or removed
        # download has already had the batch reset out from under it.
        if was_active:
            DOWNLOAD_STATUS["batch_completed"] += 1

        # If queue and active are now empty, the batch is done
        if not DOWNLOAD_STATUS["queue"] and not DOWNLOAD_STATUS["active"]:
            DOWNLOAD_STATUS["last_batch_finished_at"] = time.time()

        item["status"] = "completed" if success else "failed"
        item["method"] = method
        item["finished_at"] = time.time()
        item["path"] = str(out_dir)
        if error:
            item["error"] = error

        if success:
            DOWNLOAD_STATUS["completed"].append(item)
            DOWNLOAD_STATUS["completed_total"] += 1
            add_log(f"✓ Completed [{method}]: {artist} - {title}")
        elif cancelled:
            add_log(f"■ Cancelled: {artist} - {title}", "WARNING")
        elif was_active:
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
            is_paused = DOWNLOAD_STATUS.get("is_paused", False)
            if queue and len(active) < 3 and not is_paused:
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
    # deezer_search is synchronous `requests`; on the event loop it stalled every
    # connected client for up to the full 8s timeout on each keystroke
    tracks = await asyncio.to_thread(deezer_search, q, 15)
    results = []
    for t in tracks:
        results.append({
            "id": t.get("id"),
            "title": clean_track_title(t.get("title", "")),
            "artist": t.get("artist", {}).get("name", ""),
            "album": t.get("album", {}).get("title", ""),
            "duration": t.get("duration", 0),
            "cover": t.get("album", {}).get("cover_medium", ""),
            "preview": t.get("preview", ""),
        })
    return web.json_response(results)


async def search_playlist(request):
    """
    Main endpoint for parsing playlist URLs (YouTube, Spotify, Soundcloud, etc.)
    It extracts basic metadata for all tracks and returns them to the staging area.
    """
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

        # Handle Spotify specifically (playlists and albums share the same embed shape)
        if "spotify.com" in url and re.search(r'(playlist|album)/[a-zA-Z0-9]+', url):
            return await handle_spotify_playlist(url)

        # For YouTube and others, get playlist title first.
        # We use --flat-playlist to rapidly get metadata without downloading.
        title_cmd = ["yt-dlp", "--flat-playlist", "--print", "%(playlist_title)s", "--playlist-items", "1", url]
        playlist_title = "Unknown Playlist"
        is_single = False
        try:
            # _run_tracked kills the process on timeout instead of leaking it
            returncode, stdout, _ = await _run_tracked("", title_cmd, timeout=10)
            if returncode == 0:
                raw_title = stdout.decode(errors="replace").strip()
                # yt-dlp returns "NA" for the playlist title if the URL is a single video
                if raw_title == "NA":
                    is_single = True
                elif raw_title:
                    playlist_title = raw_title
        except Exception: pass

        cmd = [
            "yt-dlp",
            "--flat-playlist",
            "--dump-json",
            "--quiet",
            url
        ]
        
        returncode, stdout, stderr = await _run_tracked("", cmd, timeout=60)

        if returncode != 0:
            err = stderr.decode().strip()
            logger.error(f"yt-dlp playlist error: {err}")
            return web.json_response({"error": f"Failed to fetch playlist: {err[:100]}"}, status=500)

        tracks = []
        for line in stdout.decode().splitlines():
            if not line: continue
            try:
                data = json.loads(line)
                title = data.get("title", "Unknown")
                channel = data.get("channel") or data.get("uploader", "")
                artist = data.get("artist") or channel or "Unknown Artist"
                
                # Prioritize extracting artist from title format if yt-dlp artist is missing or equal to channel
                if not data.get("artist") or data.get("artist") == "Unknown Artist" or artist == channel:
                    if " - " in title:
                        parts = title.split(" - ", 1)
                        artist = parts[0].strip()
                        title = parts[1].strip()
                    else:
                        # Match: Artist "Title" (including smart quotes) + whatever fluff comes after
                        match = re.search(r'^(.*?)\s+["“”\'](.*?)["“”\'](.*)$', title)
                        if match:
                            artist = match.group(1).strip()
                            # Recombine title without quotes, leaving the fluff at the end for the cleaner
                            title = match.group(2).strip() + " " + match.group(3).strip()

                title = clean_track_title(title)
                artist, title = normalize_features(artist, title, channel)

                tracks.append({
                    "title": title,
                    "artist": artist,
                    "album": data.get("album", ""),
                    "duration": data.get("duration", 0),
                    "cover": data.get("thumbnail", ""),
                    "id": data.get("id")
                })
            except Exception: continue
            
        return web.json_response({"tracks": tracks, "title": playlist_title, "is_single": is_single})
    except Exception as e:
        logger.exception("Playlist search failed")
        return web.json_response({"error": str(e)}, status=500)


async def handle_spotify_playlist(url):
    try:
        kind, playlist_id = re.search(r'(playlist|album)/([a-zA-Z0-9]+)', url).groups()

        # Optimization: Parse the public playlist embed first.
        # This allows us to fetch track metadata for small playlists (<= 99 tracks)
        # without requiring any Spotify API authentication or user login.
        embed_url = f"https://open.spotify.com/embed/{kind}/{playlist_id}"
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
        sources = (entity.get('coverArt') or {}).get('sources', [])
        if sources:
            playlist_cover = sources[0].get('url', '')
        else:
            # Albums carry no coverArt — the artwork lives under visualIdentity
            images = (entity.get('visualIdentity') or {}).get('image', [])
            if images:
                playlist_cover = max(images, key=lambda i: i.get('maxHeight', 0)).get('url', '')

        tracks = []
        for t in track_list:
            c_sources = t.get("coverArt", {}).get("sources", [])
            t_cover = c_sources[0].get("url", playlist_cover) if c_sources else playlist_cover

            tracks.append({
                "title": clean_track_title(t.get("title", "Unknown")),
                "artist": t.get("subtitle", "Unknown Artist"),
                "album": playlist_name if kind == "album" else "",
                "duration": t.get("duration", 0) / 1000,
                "cover": t_cover,
                "id": t.get("uri", "").split(":")[-1]
            })

        # If the playlist is small, return the scraped tracks immediately.
        # Albums always take this path — the API fallback below is playlist-only.
        if len(tracks) <= 99 or kind == "album":
            return web.json_response({"tracks": tracks, "title": playlist_name})

        # If the playlist is large (100+ tracks), we MUST use the Spotify Web API 
        # to fetch the full list, as the embed is limited to 100 items.
        content = read_config_safe()
        client_id = get_val_from_content(content, "client_id", "spotify")
        client_secret = get_val_from_content(content, "client_secret", "spotify")
        user_access_token = get_val_from_content(content, "access_token", "spotify")
        user_refresh_token = get_val_from_content(content, "refresh_token", "spotify")

        if client_id and client_secret:
            token_to_use = None
            if user_access_token:
                # Use the authenticated user token to bypass 100-track limits
                token_to_use = user_access_token
                add_log(f"Fetching playlist {playlist_id} using Spotify User Auth...")

            return await handle_spotify_playlist_api(playlist_id, client_id, client_secret, token_to_use, user_refresh_token)

        # Fallback: if no credentials exist, we just return what we scraped from the embed
        add_log(f"No Spotify API credentials, using scraped tracks for {playlist_id}...")
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
                                    "title": clean_track_title(t.get("name", "Unknown")),
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


def is_already_downloaded(artist: str, title: str, playlist_name: Optional[str]) -> bool:
    """Return True if the expected output directory already contains an audio file."""
    label = sanitize_filename(f"{artist} - {title}" if artist else title)
    if playlist_name:
        out_dir = PLAYLISTS_DIR / sanitize_filename(str(playlist_name)) / label
    else:
        out_dir = SINGLES_DIR / label
    try:
        if not out_dir.exists():
            return False
        return any(f.suffix.lower() in AUDIO_EXTS for f in out_dir.iterdir() if f.is_file())
    except OSError:
        return False


def _downloaded_flags(tracks: list) -> list:
    """Disk check for a batch of tracks. Runs in a thread — a large playlist
    import is one iterdir() per track and used to block the event loop."""
    return [
        is_already_downloaded(t.get("artist", ""), t.get("title", ""), t.get("playlist_name"))
        for t in tracks
    ]


def _track_key(t: dict) -> tuple:
    """Identity used to detect a track that is already queued."""
    return (
        (t.get("artist") or "").strip().lower(),
        (t.get("title") or "").strip().lower(),
        (t.get("playlist_name") or None),
    )


async def check_downloaded(request):
    body = await request.json()
    tracks = body.get("tracks", [])
    flags = await asyncio.to_thread(_downloaded_flags, tracks)
    results = [
        {"title": t.get("title"), "artist": t.get("artist"),
         "playlist_name": t.get("playlist_name"), "downloaded": already}
        for t, already in zip(tracks, flags)
    ]
    return web.json_response(results)


async def download_playlist(request):
    body = await request.json()
    tracks = body.get("tracks", [])
    ids = []
    skipped = 0
    # Disk check before taking the lock so it never blocks the event loop
    flags = await asyncio.to_thread(_downloaded_flags, tracks)

    async with DOWNLOAD_LOCK:
        # Skip anything already on disk *or* already waiting in the queue —
        # without the latter, re-clicking Sync queued everything a second time
        queued_keys = {
            _track_key(x) for x in DOWNLOAD_STATUS["queue"] + DOWNLOAD_STATUS["active"]
        }
        to_queue = []
        for t, already in zip(tracks, flags):
            key = _track_key(t)
            if already or key in queued_keys:
                skipped += 1
                continue
            queued_keys.add(key)
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


async def get_staging(request):
    return web.json_response(STAGING)


async def set_staging(request):
    global STAGING
    try:
        body = await request.json()
        tracks = body.get("tracks")
        if not isinstance(tracks, list):
            return web.json_response({"error": "tracks must be a list"}, status=400)
        STAGING = tracks
        save_staging()
        return web.json_response({"ok": True, "count": len(STAGING)})
    except Exception as e:
        logger.exception("Failed to save staging")
        return web.json_response({"error": str(e)}, status=400)


async def clear_queue(request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    clear_all = body.get("all", False)
    failed_only = body.get("failed", False)
    async with DOWNLOAD_LOCK:
        # The failed list is managed independently of the queue, so clearing it
        # must not discard pending tracks or reset batch progress
        if failed_only:
            DOWNLOAD_STATUS["failed"] = []
            save_status()
            await broadcast()
            return web.json_response({"ok": True})

        DOWNLOAD_STATUS["queue"] = []
        DOWNLOAD_STATUS["batch_total"] = 0
        DOWNLOAD_STATUS["batch_completed"] = 0
        if clear_all:
            DOWNLOAD_STATUS["completed"] = []
            DOWNLOAD_STATUS["failed"] = []
            DOWNLOAD_STATUS["completed_total"] = 0
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
            DOWNLOAD_STATUS["queue"] = [x for x in DOWNLOAD_STATUS["queue"] if x.get("id") != tid]
            removed = len(DOWNLOAD_STATUS["queue"]) < original_len

            # If it's mid-download, actually kill the subprocess rather than
            # just hiding it from the UI while it keeps writing to disk
            was_active = any(x.get("id") == tid for x in DOWNLOAD_STATUS["active"])
            if was_active:
                DOWNLOAD_STATUS["active"] = [x for x in DOWNLOAD_STATUS["active"] if x.get("id") != tid]
                DOWNLOAD_STATUS["batch_total"] = max(0, DOWNLOAD_STATUS["batch_total"] - 1)
                kill_download(tid)
                removed = True

            # Also dismiss individual entries from the failed list
            original_failed_len = len(DOWNLOAD_STATUS["failed"])
            DOWNLOAD_STATUS["failed"] = [x for x in DOWNLOAD_STATUS["failed"] if x.get("id") != tid]
            removed = removed or (len(DOWNLOAD_STATUS["failed"]) < original_failed_len)

            if removed:
                save_status()

        await broadcast()
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def stop_downloads(request):
    async with DOWNLOAD_LOCK:
        # Kill the running subprocesses — clearing the list alone left yt-dlp and
        # streamrip running and still writing files after "Stop"
        for item in DOWNLOAD_STATUS["active"]:
            kill_download(item.get("id", ""))
        stopped = len(DOWNLOAD_STATUS["active"])
        DOWNLOAD_STATUS["queue"] = []
        DOWNLOAD_STATUS["active"] = []
        DOWNLOAD_STATUS["batch_total"] = 0
        DOWNLOAD_STATUS["batch_completed"] = 0
        DOWNLOAD_STATUS["is_paused"] = False
        if stopped:
            add_log(f"Stopped {stopped} in-flight download(s)", "WARNING")
        save_status()
    await broadcast()
    return web.json_response({"ok": True})


async def toggle_pause(request):
    async with DOWNLOAD_LOCK:
        DOWNLOAD_STATUS["is_paused"] = not DOWNLOAD_STATUS.get("is_paused", False)
        state = "paused" if DOWNLOAD_STATUS["is_paused"] else "resumed"
        add_log(f"Queue {state}")
        save_status()
    await broadcast()
    return web.json_response({"ok": True, "is_paused": DOWNLOAD_STATUS["is_paused"]})


async def retry_track(request):
    """Requeue one failed track, a list of them, or all of them.

    Bulk retry used to be N separate requests from the browser, each triggering
    its own status write and full broadcast.
    """
    try:
        body = await request.json()
        tid = body.get("id")
        ids = body.get("ids")
        retry_all = bool(body.get("all"))
        if not (tid or ids or retry_all):
            return web.json_response({"error": "No ID provided"}, status=400)

        async with DOWNLOAD_LOCK:
            if retry_all:
                wanted = {x.get("id") for x in DOWNLOAD_STATUS["failed"]}
            else:
                wanted = set(ids or []) | ({tid} if tid else set())

            requeued = [x for x in DOWNLOAD_STATUS["failed"] if x.get("id") in wanted]
            if requeued:
                DOWNLOAD_STATUS["failed"] = [
                    x for x in DOWNLOAD_STATUS["failed"] if x.get("id") not in wanted
                ]
                for item in requeued:
                    item["status"] = "pending"
                    item["queued_at"] = time.time()
                    # Clean up state from the previous attempt
                    item.pop("finished_at", None)
                    item.pop("started_at", None)
                    item.pop("error", None)
                    DOWNLOAD_STATUS["queue"].append(item)
                # Retries are part of the batch too — without this the progress
                # bar counted completions against a total that never grew
                DOWNLOAD_STATUS["batch_total"] += len(requeued)
                save_status()

        await broadcast()
        return web.json_response({"ok": True, "count": len(requeued)})
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
        found_local = False
        for name in ["cover.jpg", "cover.png", "folder.jpg", "folder.png"]:
            p = base / name
            if p.exists():
                return web.FileResponse(p)
        
        # If folder was specified but no local cover found, return 404 
        # so frontend can fall back to generic icon
        return web.Response(status=404)

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
    total = 0
    for f in path.rglob('*'):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue  # file vanished mid-scan (an active download, a delete)
    return total


# A full rglob over the library is expensive and was being run per directory
# listing and once per completed download. Cache it.
_LIB_SIZE = {"value": 0, "ts": 0.0}
_LIB_SIZE_TTL = 60


async def get_library_size(force: bool = False) -> int:
    now = time.time()
    if not force and (now - _LIB_SIZE["ts"]) < _LIB_SIZE_TTL:
        return int(_LIB_SIZE["value"])
    size = await asyncio.to_thread(get_folder_size, DOWNLOADS_DIR)
    _LIB_SIZE.update(value=size, ts=now)
    return size


async def refresh_library_size():
    """Recompute DOWNLOADS_DIR size (rate-limited) and push it via broadcast."""
    before = DOWNLOAD_STATUS.get("library_size")
    DOWNLOAD_STATUS["library_size"] = await get_library_size()
    if DOWNLOAD_STATUS["library_size"] != before:
        await broadcast()


def _scan_dir(target: Path) -> list:
    items = []
    try:
        # Use target.iterdir() but ensure paths are relative to DOWNLOADS_DIR for frontend
        for entry in sorted(target.iterdir(), key=lambda e: (e.is_file(), e.name.lower())):
            try:
                stat = entry.stat()
            except OSError:
                continue
            rel_path = str(entry.relative_to(DOWNLOADS_DIR))

            has_cover = False
            if entry.is_dir():
                for name in ["cover.jpg", "cover.png", "folder.jpg", "folder.png"]:
                    if (entry / name).exists():
                        has_cover = True
                        break

            items.append({
                "name": entry.name,
                "type": "file" if entry.is_file() else "dir",
                "size": stat.st_size if entry.is_file() else 0,
                "modified": stat.st_mtime,
                "ext": entry.suffix.lower() if entry.is_file() else "",
                "path": rel_path,
                "has_cover": has_cover,
            })
    except PermissionError:
        pass
    return items


async def list_files(request):
    path_param = request.rel_url.query.get("path", "")
    base = DOWNLOADS_DIR
    target = (base / path_param).resolve()
    if not is_safe_path(target, base):
        return web.json_response({"error": "forbidden"}, status=403)
    if not target.exists():
        return web.json_response({"error": "not found"}, status=404)

    # Directory scan and disk stat both hit the filesystem — keep them off the loop
    items = await asyncio.to_thread(_scan_dir, target)
    total, used, free = await asyncio.to_thread(shutil.disk_usage, str(base))
    folder_size = await get_library_size()

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
    if not src.exists():
        return web.json_response({"error": "not found"}, status=404)
    dst = src.parent / new_name
    # sanitize_filename strips separators, but re-check: the destination must
    # still land inside the library and must not clobber an existing entry
    if not is_safe_path(dst, DOWNLOADS_DIR):
        return web.json_response({"error": "forbidden"}, status=403)
    if dst.exists():
        return web.json_response({"error": "a file with that name already exists"}, status=409)
    try:
        await asyncio.to_thread(src.rename, dst)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"ok": True})


async def delete_file(request):
    body = await request.json()
    rel = body.get("path", "")
    target = (DOWNLOADS_DIR / rel).resolve()
    if not is_safe_path(target, DOWNLOADS_DIR):
        return web.json_response({"error": "forbidden"}, status=403)
    if target == DOWNLOADS_DIR.resolve():
        return web.json_response({"error": "refusing to delete the library root"}, status=400)
    if not target.exists():
        return web.json_response({"error": "not found"}, status=404)
    try:
        if target.is_dir():
            await asyncio.to_thread(shutil.rmtree, target)
        else:
            await asyncio.to_thread(target.unlink)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"ok": True})


def _write_zip(zip_path: Path, folder: Path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in folder.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(folder.parent))


async def zip_folder(request):
    body = await request.json()
    rel = body.get("path", "")
    folder = (DOWNLOADS_DIR / rel).resolve()
    if not is_safe_path(folder, DOWNLOADS_DIR) or not folder.is_dir():
        return web.json_response({"error": "invalid"}, status=400)

    zip_path = folder.parent / f"{folder.name}.zip"
    # Zipping a large folder on the event loop froze every client until it finished
    await asyncio.to_thread(_write_zip, zip_path, folder)

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
    quality = body.get("quality", "FLAC")
    spotify_id = body.get("spotify_id", "")
    spotify_redirect = body.get("spotify_redirect", "")

    # Refuse to write over a config we couldn't read — doing so would silently
    # destroy the stored Spotify tokens and ARL
    try:
        existing_content = read_config_raw()
    except ConfigUnreadableError as e:
        return web.json_response({"error": str(e)}, status=409)

    # Keep existing tokens if not provided in the save (don't overwrite with empty)
    spotify_access_token = get_val_from_content(existing_content, "access_token", "spotify")
    spotify_refresh_token = get_val_from_content(existing_content, "refresh_token", "spotify")

    # Secrets come back from the UI masked when unchanged — keep what we have
    arl = body.get("arl", "")
    if arl == SECRET_MASK:
        arl = get_val_from_content(existing_content, "arl", "deezer")
    spotify_secret = body.get("spotify_secret", "")
    if spotify_secret == SECRET_MASK:
        spotify_secret = get_val_from_content(existing_content, "client_secret", "spotify")

    toml_content = f"""[deezer]
arl = "{toml_escape(arl)}"

[spotify]
client_id = "{toml_escape(spotify_id)}"
client_secret = "{toml_escape(spotify_secret)}"
redirect_uri = "{toml_escape(spotify_redirect)}"
access_token = "{toml_escape(spotify_access_token)}"
refresh_token = "{toml_escape(spotify_refresh_token)}"

[downloads]
folder = "{toml_escape(str(DOWNLOADS_DIR))}"
quality = "{toml_escape(quality)}"
"""
    write_config_raw(toml_content)
    DEEZER_MAX_QUALITY = None  # re-probe the account's quality ceiling
    return web.json_response({"ok": True})


def set_val_in_content(content: str, key: str, section: str, value: str) -> str:
    """Replace (or insert) a key in a TOML section without regex-escape hazards.

    The value is passed through a callable replacement so backslashes in a token
    are never interpreted as re.sub group references.
    """
    line = f'{key} = "{toml_escape(value)}"'
    pattern = re.compile(
        r'^(?P<indent>[ \t]*)' + re.escape(key) + r'\s*=\s*.*$', re.MULTILINE
    )
    if pattern.search(content):
        return pattern.sub(lambda m: m.group("indent") + line, content, count=1)
    header = f"[{section}]"
    if header in content:
        return content.replace(header, f"{header}\n{line}", 1)
    return content.rstrip() + f"\n\n{header}\n{line}\n"


def get_val_from_content(content, key, section):
    if not content: return ""
    # Anchor the section end to a line-start bracket so a "[" inside a value
    # (e.g. a folder path) doesn't truncate the section
    s_match = re.search(
        r'^\[' + re.escape(section) + r'\](.*?)(?=^\[|\Z)',
        content, re.DOTALL | re.MULTILINE,
    )
    if not s_match:
        return ""
    section_text = s_match.group(1)
    # Double-quoted, honouring backslash escapes written by toml_escape
    m = re.search(re.escape(key) + r'\s*=\s*"((?:[^"\\]|\\.)*)"', section_text)
    if m:
        return toml_unescape(m.group(1)).strip()
    m = re.search(re.escape(key) + r"\s*=\s*'([^']*)'", section_text)
    if m:
        return m.group(1).strip()
    m = re.search(re.escape(key) + r'\s*=\s*([^\s,]+)', section_text)
    return m.group(1).strip() if m else ""

async def spotify_login(request):
    import urllib.parse
    content = read_config_safe()
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
    state = new_oauth_state()

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
    # The state we sent in /api/spotify/login must come back, or this callback
    # wasn't started by us
    if not consume_oauth_state(request.query.get("state", "")):
        logger.warning("Spotify callback rejected: unknown or expired state")
        return web.Response(
            text="Invalid or expired OAuth state. Start the login again from Settings.",
            status=400,
        )

    try:
        content = read_config_raw()
    except ConfigUnreadableError as e:
        return web.Response(text=f"Cannot store tokens: {e}", status=409)
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
            # Never log token material, even partially — these lines also reach
            # the in-app log panel
            logger.info(
                f"spotify_callback: token exchange ok (refresh token issued: {bool(refresh_token)})"
            )
            if not access_token:
                return web.Response(text="Spotify did not return an access token", status=400)

            # Surgical update to just the tokens
            new_cfg = set_val_in_content(content, "access_token", "spotify", access_token)
            # Spotify only issues a refresh token on the first consent — keep the
            # existing one rather than writing the literal string "None"
            if refresh_token:
                new_cfg = set_val_in_content(new_cfg, "refresh_token", "spotify", refresh_token)

            write_config_raw(new_cfg)
            logger.info("spotify_callback: Config updated with new tokens")

    return web.Response(text="Successfully logged into Spotify! You can close this window and try your import again.", content_type="text/html")

async def refresh_spotify_token(client_id, client_secret, refresh_token):
    logger.info("Refreshing Spotify access token")
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
                if not new_access:
                    logger.error("Spotify refresh returned no access token")
                    return None
                logger.info("Spotify token refresh successful")
                # Save new token
                try:
                    cfg_text = read_config_raw()
                except ConfigUnreadableError as e:
                    logger.error(f"Refreshed token could not be persisted: {e}")
                    return new_access
                write_config_raw(set_val_in_content(cfg_text, "access_token", "spotify", new_access))
                return new_access
            else:
                err = await resp.text()
                logger.error(f"Spotify token refresh failed ({resp.status}): {err}")
    return None

async def get_config(request):
    cfg = {"arl": "", "quality": "FLAC", "spotify_id": "", "spotify_secret": "", "deps": {}}
    try:
        content = read_config_raw()
        cfg["config_readable"] = True
    except ConfigUnreadableError as e:
        content = ""
        cfg["config_readable"] = False
        cfg["config_error"] = str(e)

    if content:
        # Credentials are never returned in the clear: this endpoint is
        # unauthenticated by default and the ARL is a full Deezer account
        # credential. The UI posts the mask back when the value is unchanged.
        arl = get_val_from_content(content, "arl", "deezer")
        secret = get_val_from_content(content, "client_secret", "spotify")
        cfg["arl"] = SECRET_MASK if arl else ""
        cfg["arl_set"] = bool(arl)
        cfg["spotify_secret"] = SECRET_MASK if secret else ""
        cfg["spotify_secret_set"] = bool(secret)
        cfg["spotify_id"] = get_val_from_content(content, "client_id", "spotify")
        cfg["spotify_redirect"] = get_val_from_content(content, "redirect_uri", "spotify")
        cfg["spotify_logged_in"] = bool(get_val_from_content(content, "refresh_token", "spotify"))

        val_q = get_val_from_content(content, "quality", "downloads")
        if val_q: cfg["quality"] = val_q

    cfg["deps"]["streamrip"] = find_rip() is not None
    cfg["deps"]["ytdlp"] = shutil.which("yt-dlp") is not None
    cfg["deps"]["encryption"] = "fernet" if HAVE_FERNET else "xor-obfuscation"
    cfg["auth_enabled"] = bool(AUTH_TOKEN)
    cfg["download_path"] = str(DOWNLOADS_DIR)

    return web.json_response(cfg)


async def get_logs(request):
    return web.json_response(DOWNLOAD_STATUS.get("logs", []))


async def serve_index(request):
    return web.FileResponse(FRONTEND_DIR / "index.html")


# ── App setup ──────────────────────────────────────────────────────────────────

async def on_startup(app):
    load_status()
    load_staging()
    chmod_private(KEY_FILE)
    chmod_private(CONFIG_FILE)
    # Legacy shared config held the ARL in plaintext at a predictable path
    legacy_sr = CONFIG_DIR / "streamrip_config.toml"
    if legacy_sr.exists():
        try:
            legacy_sr.unlink()
        except OSError:
            pass
    if not HAVE_FERNET:
        logger.warning(
            "`cryptography` is not installed — config is only XOR-obfuscated, not "
            "encrypted. Install it (see requirements.txt) for real encryption at rest."
        )
    if not AUTH_TOKEN:
        logger.warning(
            "MV_AUTH_TOKEN is not set — the API is unauthenticated. Anyone who can "
            "reach this port can browse and delete the library."
        )
    app["queue_worker"] = asyncio.create_task(queue_worker())
    add_log("Music Vault server started")
    DOWNLOAD_STATUS["library_size"] = await get_library_size(force=True)
    await broadcast()


async def on_shutdown(app):
    # Don't leave yt-dlp / streamrip subprocesses running after we exit
    for tid in list(ACTIVE_PROCS):
        kill_download(tid)
    task = app.get("queue_worker")
    if task:
        task.cancel()
    for ws in list(WS_CLIENTS):
        try:
            await ws.close()
        except Exception:
            pass


def create_app():
    app = web.Application(middlewares=[auth_middleware])
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

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
    app.router.add_get("/api/staging", get_staging)
    app.router.add_post("/api/staging", set_staging)
    app.router.add_post("/api/download/stop", stop_downloads)
    app.router.add_post("/api/download/pause", toggle_pause)
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
    # 0.0.0.0 by default because the container needs it; set MV_HOST=127.0.0.1
    # to keep the server local-only when running directly on a host.
    web.run_app(
        app,
        host=os.environ.get("MV_HOST", "0.0.0.0"),
        port=int(os.environ.get("MV_PORT", 8081)),
    )