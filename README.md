# tvremote

Play Kick / Twitch / YouTube / any link on your TVs from a Raspberry Pi 4, controlled from an iPhone.
One Docker container with a small web remote (port 8765). Two kinds of TV:

- **Pi on HDMI** (living room): mpv draws on the Pi's Wayland desktop, plays through the host's PipeWire and uses the Pi's hardware H.264 decoder.
- **Chromecast** (bedroom): found on the network and cast to. YouTube uses the Chromecast's own YouTube app; Kick, Twitch and other links are resolved on the Pi and cast.

The remote has a TV picker at the top (Living room / Bedroom). Pause, volume, mute, seek, quality, stop and favourites act on the selected TV.

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

Check: `curl http://<pi-ip>:8765/api/status`

## iPhone Shortcut ("Play on Pi")

1. Receive **Any** input from **Share Sheet**; if there's no input: **Get Clipboard**.
2. **URL Encode** the Shortcut Input.
3. **Text**: `http://<pi-ip>:8765/play?u=` followed by the URL Encoded Text.
4. **Open URLs** with that Text.
5. Details: Show in Share Sheet on, Add to Home Screen. Optional: Settings → Accessibility → Touch → Back Tap → Double Tap → Play on Pi.

The Kick app doesn't list Shortcuts in its share sheet: copy the link, then run the Shortcut.

Remote on the Home Screen: open `http://<pi-ip>:8765` in Safari → Share → Add to Home Screen.

## Password

Set `PASSWORD=<something>` in `~/tvremote/.env` on the Pi, then `docker compose up -d`. The remote asks for it (any username; Safari can save it). In the Shortcut, end the URL with `&t=<password>` after the encoded link: `http://<pi-ip>:8765/play?u=<encoded>&t=<password>`. Plain HTTP on your LAN, so it keeps casual users out, not a determined sniffer.

## Roku TVs, Chromecast and input switching

- Set `ROKU_LIVING` / `ROKU_BEDROOM` (TV addresses) in `.env` to get a TV card with power, volume, input and navigation buttons. On each TV: Settings → System → Advanced system settings → Control by mobile apps → Network access → Permissive.
- Set `INPUT_LIVING` / `INPUT_BEDROOM` (e.g. `HDMI1`) and the TV wakes and switches to that input whenever you play on it.
- The bedroom target is a Chromecast found by mDNS (name configurable in `/data/targets.json`). Pick the TV at the top of the remote, or add `&target=bedroom` to the Shortcut URL (a second Shortcut, "Play in Bedroom").
- `.env` (not committed) holds your TV addresses and inputs. Keep real addresses out of the repo.
- The Roku remote's own buttons (pause etc.) don't reach the Pi or the Chromecast: the TV doesn't forward them over CEC. The phone is the remote.

### How Kick/Twitch reach the Chromecast

By default the Chromecast fetches the stream itself (`direct`). `&via=proxy` on `/play` (or `TVREMOTE_CAST_VIA=proxy` in the container environment) makes the Pi fetch the HLS segments over its wired link and relay them to the Chromecast (`app/hlsproxy.py`). The relay path is token-guarded and only fetches public http(s) hosts. In testing, both modes buffer only while starting (about 10–17 s) on a strong Wi-Fi link.
## Sources

| Source | Player |
|---|---|
| Kick, Twitch | streamlink → mpv (`1080p60,best`; Twitch ads disabled) |
| YouTube live (`/live` or `is_live`) | streamlink → mpv |
| YouTube video, everything else | mpv + yt-dlp, H.264 only, max 1080p |

Routing lives in the `SOURCES` table at the top of `app/app.py`; a new streamlink site is one line.

## API

`GET /play?u=&q=&target=&via=` · `POST /api/play` · `POST /api/cmd` (pause, mute, volume, volume_delta, seek, stop, restart) · `GET /api/status` · `GET /api/qualities` · `GET /api/search?q=` · `GET/POST/DELETE /api/favourites` · `GET /api/targets` · `GET /api/tv` · `GET /api/roku` · `POST /api/roku/key` · `GET /api/debug`

Add `?target=bedroom` to the API calls to act on the Chromecast. `/api/status` includes `hwdec` (should be `v4l2m2m` on the Pi) and `drops`. The **Debug** button on the remote opens a drawer with player state, mpv properties, versions, the command line and the log.

Changing the volume from the remote shows a short "Volume N%" message on the Pi's screen.

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
- **Bedroom missing from the picker or "not found on the network":** the Chromecast must be on the same network as the Pi (no guest network or client isolation). The container uses host networking so mDNS works.
- **Volume overlay or chat text not showing:** only the Pi's screen can show overlays; the Chromecast can't.
- `/play` is a plain GET; set `PASSWORD` (above) if the network isn't trusted.
