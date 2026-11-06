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

# Latest streamlink / yt-dlp (apt's yt-dlp comes in via mpv but is too old; venv comes first on PATH)
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir flask waitress streamlink "yt-dlp[default,curl-cffi]" deno pychromecast segno
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
