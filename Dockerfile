FROM python:3.12-slim

WORKDIR /app

# System dependencies
# streamrip often needs libsndfile1 and build tools for some dependencies
# locales are often needed for python packages that handle metadata
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    git \
    libsndfile1 \
    build-essential \
    locales \
    pipx \
    && rm -rf /var/lib/apt/lists/*

# Set locale
RUN sed -i -e 's/# en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen && \
    locale-gen
ENV LANG en_US.UTF-8
ENV LANGUAGE en_US:en
ENV LC_ALL en_US.UTF-8
ENV PATH="/root/.local/bin:${PATH}"

# Upgrade pip
RUN pip install --no-cache-dir --upgrade pip

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# yt-dlp
RUN pip install --no-cache-dir yt-dlp

# streamrip via pipx for better isolation on Linux
RUN pipx install streamrip

# Copy app files
COPY server.py .
COPY index.html .
COPY static/ ./static/

# Create directories
RUN mkdir -p downloads/singles downloads/playlists data/logs config

EXPOSE 8080

CMD ["python", "server.py"]
