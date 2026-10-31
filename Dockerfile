# HardlyWatching in a container.
#
# The app is a stdlib-Python server that serves its own web UI; the container is
# the same server the desktop bundles ship, plus the two tools it shells out to.
# There is no desktop shell here on purpose: you open the UI in your browser.
#
#   docker build -t hardlywatching .
#   docker run -d -p 8765:8765 \
#     -v "$PWD/data:/data" -v "$PWD/archive:/archive" hardlywatching
#
# Two volumes, and nothing else persists:
#   /data     state.json, the two SQLite files, thumbnails, avatars, sessions
#   /archive  the media, one folder per profile (the app's downloads root)

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    INSTA_DATA=/data \
    INSTA_OUTPUT=/archive \
    INSTA_HOST=0.0.0.0 \
    INSTA_PORT=8765 \
    INSTA_NO_BROWSER=1 \
    INSTA_LAN=1

# ffmpeg: TikTok and reel downloads, thumbnails and the hover previews.
# curl is not used by the app; it is here so `docker exec` debugging is possible
# without installing anything.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# The optional dependencies are the features, not decoration: pillow-heif makes
# HEIC thumbnails, and onnxruntime+numpy are the "who is in this picture" pass.
# Without them those two features silently turn themselves off.
RUN pip install --no-cache-dir \
      instaloader \
      yt-dlp \
      "pillow>=10.4,<12" \
      pillow-heif \
      numpy \
      onnxruntime

WORKDIR /app
COPY . /app

# Run as a normal user, not root: every file the app writes lands in /data or
# /archive, and on a bind mount that would otherwise be owned by root.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin hw \
 && mkdir -p /data /archive \
 && chown -R hw:hw /data /archive /app

# The image is labelled with the release it was cut from; CI passes this in
# (`--build-arg VERSION=$(cat VERSION)`) so the label cannot drift from the tag.
ARG VERSION=dev
LABEL org.opencontainers.image.title="HardlyWatching" \
      org.opencontainers.image.description="Self-hosted download queue and viewer for Instagram and TikTok" \
      org.opencontainers.image.source="https://github.com/hardlysleeping/HardlyWatching" \
      org.opencontainers.image.documentation="https://github.com/hardlysleeping/HardlyWatching/blob/master/docs/docker.md" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      org.opencontainers.image.version="${VERSION}"

USER hw
VOLUME ["/data", "/archive"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=4).status==200 else 1)"

CMD ["python3", "server.py"]
