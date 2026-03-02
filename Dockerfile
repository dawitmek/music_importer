FROM python:3.12-slim

WORKDIR /app

# System dependencies
# streamrip often needs libsndfile1 and build tools for some dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    git \
    libsndfile1 \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip
RUN pip install --no-cache-dir --upgrade pip

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# yt-dlp
RUN pip install --no-cache-dir yt-dlp

# streamrip
# We remove || true to ensure we know if it fails during build
RUN pip install --no-cache-dir streamrip

# Copy app files
COPY server.py .
COPY index.html .
COPY static/ ./static/

# Create directories
RUN mkdir -p downloads/singles downloads/playlists data/logs config

EXPOSE 8080

CMD ["python", "server.py"]
