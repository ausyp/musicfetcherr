# Musicfetcherr — one container: downloader API + Telegram bot + exit-node watchdog.
FROM node:22-bookworm-slim AS node
FROM tailscale/tailscale:latest AS tailscale

FROM python:3.12-slim-bookworm

# ffmpeg/ffprobe: cover conversion + delivered-format report.
# node: yt-dlp's JavaScript runtime for YouTube signature challenges.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates libstdc++6 tini \
 && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
# tailscale CLI for the exit-node watchdog (static Go binary)
COPY --from=tailscale /usr/local/bin/tailscale /usr/local/bin/tailscale

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "yt-dlp[default]"

COPY . .

# /data = staging + caches (volume). Writable by whatever PUID the stack uses.
RUN mkdir -p /data/staging /data/cache /music && chmod -R 0777 /data /music

ENV PYTHONUNBUFFERED=1 \
    HOME=/data \
    XDG_CACHE_HOME=/data/cache \
    MUSICFETCHERR_CONFIG=/tmp/musicfetcherr/config.yaml \
    STAGING_DIR=/data/staging \
    MUSIC_DIR=/music

ENTRYPOINT ["tini", "--", "python", "/app/docker/entrypoint.py"]
