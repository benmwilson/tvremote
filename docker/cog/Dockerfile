# The TV's home-screen browser (WPE WebKit "cog"): a separate container so the main image stays small.
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends cog libwpewebkit-2.0-1 libwpebackend-fdo-1.0-1 \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav \
    libgl1-mesa-dri libgles2 libegl-mesa0 libgbm1 fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# A fully transparent mouse cursor theme: cog never hides the pointer itself, so without this the compositor's arrow sits on the TV.
COPY docker/cog/blank-cursor/left_ptr /usr/share/icons/blank/cursors/left_ptr
RUN cd /usr/share/icons/blank/cursors && for n in default arrow top_left_arrow pointer hand1 hand2 text xterm ibeam watch wait; do ln -sf left_ptr $n; done     && echo "[Icon Theme]" > /usr/share/icons/blank/index.theme && echo "Name=blank" >> /usr/share/icons/blank/index.theme
ENV XCURSOR_THEME=blank XCURSOR_PATH=/usr/share/icons XCURSOR_SIZE=24
COPY docker/cog/run.sh /run.sh
RUN chmod +x /run.sh
CMD ["/run.sh"]
