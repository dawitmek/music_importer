FROM python:3.11-slim

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# yt-dlp
RUN pip install --no-cache-dir yt-dlp

# streamrip (optional, may need Deezer ARL)
RUN pip install --no-cache-dir streamrip || true

# Copy app
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Create directories
RUN mkdir -p downloads/singles downloads/playlists data/logs config

EXPOSE 8080

CMD ["python", "backend/server.py"]
