# 🎵 Music Vault

A visually stunning, full-stack music downloader and library manager.

## Features

- **Deezer Search** — Real-time track search via the Deezer API
- **Smart Download Chain** — Streamrip (Deezer FLAC/MP3) → yt-dlp (YouTube) → Failed
- **Staging Queue** — Stage multiple tracks, then batch-sync
- **File Manager** — Browse, play, rename, delete, zip-download your library
- **Audio Player** — Persistent footer player supporting MP3, FLAC, M4A, and more
- **WebSocket Sync** — Real-time download status across all browser tabs
- **System Logs** — Live backend logs streamed to the UI
- **Smart Cover Art** — Checks local folder → Deezer API → SVG placeholder

---

## Quick Start

### Option A: Docker (Recommended)

```bash
# Build and run
docker-compose up --build

# With Deezer ARL
DEEZER_ARL=your_arl_here docker-compose up --build
```

### Option B: Local Python

```bash
# Install system deps
sudo apt install ffmpeg   # Linux
brew install ffmpeg        # macOS

# Run startup script
chmod +x start.sh
./start.sh

# Custom port
./start.sh --port 9000
```

Open **http://localhost:8080**

---

## Configuration

### Deezer ARL (for high-quality downloads)

1. Log in to [deezer.com](https://deezer.com)
2. Open DevTools → Application → Cookies → `arl` value
3. Paste in **Music Vault → Config → Deezer ARL**

Without an ARL, all downloads use the YouTube fallback (MP3).

---

## Directory Structure

```
music_vault/
├── backend/
│   └── server.py          # aiohttp backend + WebSocket
├── frontend/
│   └── index.html         # Single-page app
├── downloads/
│   ├── singles/           # Individual track downloads
│   └── playlists/         # Playlist downloads
├── data/
│   ├── status.json        # Persisted queue state
│   └── logs/              # Server logs
├── config/
│   └── config.toml        # Deezer ARL + quality settings
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── start.sh               # Dev startup orchestrator
```

---

## API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/api/search/suggestions?q=` | GET | Deezer track search |
| `/api/download/single` | POST | Queue single track |
| `/api/download/playlist` | POST | Queue multiple tracks |
| `/api/download/clear` | POST | Clear pending queue |
| `/api/track-cover` | GET | Smart cover art (local → Deezer → SVG) |
| `/api/files` | GET | List files with disk usage |
| `/api/files/rename` | POST | Rename file or folder |
| `/api/files/delete` | POST | Delete file or folder |
| `/api/files/zip` | POST | Zip folder for download |
| `/api/status` | GET | Full download status JSON |
| `/api/config` | GET/POST | Read/write config |
| `/api/logs` | GET | Recent log entries |
| `/ws/status` | WS | Real-time status broadcast |
| `/files/{path}` | GET | Serve downloaded files |

---

## Download Fallback Chain

```
User requests track
       │
       ▼
[1] Streamrip + Deezer ARL
   → FLAC or MP3_320
   → Success? ✓ Done
       │ Fail
       ▼
[2] yt-dlp → YouTube search
   → MP3 best quality
   → Success? ✓ Done
       │ Fail
       ▼
[3] Mark as FAILED
   → Logged for retry
```

---

## Tech Stack

- **Backend**: Python 3.11, aiohttp, asyncio
- **Frontend**: Vanilla JS, CSS3, Web Audio API
- **Downloads**: streamrip, yt-dlp, ffmpeg
- **Infrastructure**: Docker, Docker Compose, Bash
- **Fonts**: Bebas Neue + DM Mono + DM Sans

---

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `DEEZER_ARL` | Deezer ARL cookie | (empty) |
| `VV_PORT` | Server port | `8080` |
# music_vault_v2
