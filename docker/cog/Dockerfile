# The TV's home-screen browser (WPE WebKit "cog"): a separate container so the main image stays small.
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends cog libwpewebkit-2.0-1 libwpebackend-fdo-1.0-1 \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav \
    libgl1-mesa-dri libgles2 libegl-mesa0 libgbm1 fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY docker/cog/run.sh /run.sh
RUN chmod +x /run.sh
CMD ["/run.sh"]
