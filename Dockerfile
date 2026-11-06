FROM debian:trixie-slim

# Raspberry Pi repo: its ffmpeg (+rpt) has the v4l2m2m hwdec patches that plain Debian ffmpeg lacks
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
# Same keyring + deb822 entry as the host (copied from its /usr/share/keyrings)
COPY docker/raspberrypi-archive-keyring.pgp /usr/share/keyrings/raspberrypi-archive-keyring.pgp
COPY docker/raspi.sources /etc/apt/sources.list.d/raspi.sources
COPY docker/raspi.pref /etc/apt/preferences.d/raspi

RUN apt-get update && apt-get install -y --no-install-recommends \
      mpv cec-utils v4l-utils wlr-randr python3 python3-venv pipx procps grim openssl \
      libpipewire-0.3-0 pipewire-bin \
    && rm -rf /var/lib/apt/lists/* \
    && apt-cache policy libavcodec61 \
    && dpkg -s libavcodec61 | grep -q '^Version:.*rpt'

# Every package, and everything they pull in, is pinned in requirements.lock (what runs on the Pi, 2026-11-06), so a rebuild changes nothing by surprise.
# streamlink and yt-dlp break when sites change: bump them with
#   docker compose build --build-arg YTDLP_VERSION=<new> --build-arg STREAMLINK_VERSION=<new>
# and, once the new ones work, put the same versions in requirements.lock. (apt's yt-dlp comes in via mpv but is too old; the venv comes first on PATH.)
ARG STREAMLINK_VERSION=
ARG YTDLP_VERSION=
COPY requirements.lock /tmp/requirements.lock
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.lock \
    && if [ -n "$STREAMLINK_VERSION" ]; then /opt/venv/bin/pip install --no-cache-dir "streamlink==$STREAMLINK_VERSION"; fi \
    && if [ -n "$YTDLP_VERSION" ]; then /opt/venv/bin/pip install --no-cache-dir "yt-dlp[default,curl-cffi]==$YTDLP_VERSION"; fi
ENV PATH=/opt/venv/bin:$PATH

# uid/gid 1000 so the host's Wayland and PipeWire sockets accept us
RUN groupadd -g 1000 app && useradd -m -u 1000 -g 1000 -d /home/app app \
    && mkdir -p /data /home/app/.config/mpv && chown -R 1000:1000 /data /home/app

COPY --chown=1000:1000 config/mpv.conf /home/app/.config/mpv/mpv.conf
COPY app/ /opt/tvremote/
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER 1000:1000
ENV HOME=/home/app
EXPOSE 8765
ENTRYPOINT ["/entrypoint.sh"]
