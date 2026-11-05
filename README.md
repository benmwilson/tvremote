# tvremote

Play Kick / Twitch / YouTube / any link on the living-room TV from a Raspberry Pi 4, controlled from an iPhone.
One Docker container: mpv draws on the Pi's Wayland desktop, plays through the host's PipeWire, and uses the Pi's hardware H.264 decoder. A small web remote (port 8765) controls it.

## Setup (on the Pi)

Needs Docker, a running labwc/Wayland desktop for uid 1000, and the TV on HDMI-A-1.

```
git clone https://github.com/benmwilson/tvremote ~/tvremote
cd ~/tvremote
getent group video render        # put the numbers in .env (see below)
docker compose up -d --build     # first build takes several minutes
docker logs --tail 30 tvremote
```

Optional `.env` (copy `.env.example`): set `VIDEO_GID` / `RENDER_GID` to the numbers from `getent group video render`.
`compose.yaml` passes `/dev/dri`, `/dev/cec0` and `/dev/video10-16,18,19`; remove any that don't exist on your Pi.

Update: `cd ~/tvremote && git pull && docker compose up -d --build` (rebuilding also pulls the latest streamlink and yt-dlp).

Check: `curl http://192.168.1.138:8765/api/status`

## iPhone Shortcut ("Play on Pi")

1. Receive **Any** input from **Share Sheet**; if there's no input: **Get Clipboard**.
2. **URL Encode** the Shortcut Input.
3. **Text**: `http://192.168.1.138:8765/play?u=` followed by the URL Encoded Text.
4. **Open URLs** with that Text.
5. Details: Show in Share Sheet on, Add to Home Screen. Optional: Settings → Accessibility → Touch → Back Tap → Double Tap → Play on Pi.

The Kick app doesn't list Shortcuts in its share sheet: copy the link, then run the Shortcut.

Remote on the Home Screen: open `http://192.168.1.138:8765` in Safari → Share → Add to Home Screen.

## Sources

| Source | Player |
|---|---|
| Kick, Twitch | streamlink → mpv (`1080p60,best`; Twitch ads disabled) |
| YouTube live (`/live` or `is_live`) | streamlink → mpv |
| YouTube video, everything else | mpv + yt-dlp, H.264 only, max 1080p |

Routing lives in the `SOURCES` table at the top of `app/app.py`; a new streamlink site is one line.

## API

`GET /play?u=&q=` · `POST /api/play` · `POST /api/cmd` (pause, mute, volume, volume_delta, seek, stop, restart) · `GET /api/status` · `GET /api/qualities` · `GET /api/search?q=` · `GET/POST/DELETE /api/favourites`

`/api/status` includes `hwdec` (should be `v4l2m2m`) and `drops`.

## Troubleshooting

- **Black screen / no picture:** `docker logs --tail 50 tvremote`; the player output is also in `/data/player.log` inside the container. Check the Wayland socket exists: `ls /run/user/1000`.
- **Dropped frames:** `hwdec` must be `v4l2m2m` and the display 1920x1080@60. The entrypoint sets this with `wlr-randr`; dmabuf-wayland doesn't scale, so stream resolution should match the display.
- **hwdec empty or `no`:** a decoder node isn't passed through or GIDs are wrong. Use numeric GIDs in `.env`, make sure `/dev/video10-19` exist.
- **TV doesn't switch input:** the entrypoint runs `cec-ctl --playback -o HunnidPi`; check the log for `cec-ctl failed` and that `/dev/cec0` is passed through.
- **Vimeo:** needs a logged-in session (cookies); not supported out of the box.
- **Playback stops immediately:** the reason (e.g. `HTTP error 404`, `login required`) shows on the remote and as `error` in `/api/status`.
- **YouTube fails:** yt-dlp needs a JS runtime (deno is in the image). Rebuild to get the latest yt-dlp.
- **Permission denied on sockets:** the container runs as uid/gid 1000 to match the host user.
- The TV remote can't control the Pi (the Roku TV forwards no CEC key presses); the phone is the remote.
- `/play` is a plain GET with no auth, so keep the port on your LAN.
