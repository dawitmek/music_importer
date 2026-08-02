#!/usr/bin/env bash
# yt-dlp is pinned at image build time, but YouTube changes often enough that a
# months-old image starts failing every fallback download. Refresh it on start
# unless disabled. Failure here is non-fatal — we may simply be offline.
set -u

if [[ "${MV_UPDATE_YTDLP:-1}" == "1" ]]; then
  echo "[i] Updating yt-dlp…"
  if pip install --no-cache-dir --upgrade --quiet yt-dlp 2>/dev/null; then
    echo "[✓] yt-dlp: $(yt-dlp --version 2>/dev/null || echo unknown)"
  else
    echo "[!] yt-dlp update failed (offline?) — continuing with the bundled version"
  fi
fi

exec "$@"
