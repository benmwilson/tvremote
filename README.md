# tvremote

Play Kick / Twitch / YouTube / any link on your TVs from a Raspberry Pi 4, controlled from an iPhone.
One Docker container with a small web remote (port 8765). Two kinds of TV:

- **Pi on HDMI** (living room): mpv draws on the Pi's Wayland desktop, plays through the host's PipeWire and uses the Pi's hardware H.264 decoder.
- **Chromecast** (bedroom): found on the network and cast to. YouTube uses the Chromecast's own YouTube app; Kick, Twitch and other links are resolved on the Pi and cast.

The remote opens on **All TVs**: a card per TV showing its name, brand and size, what it's playing and a small health dot (amber "Buffering" if the stream stalls). Tap a TV to control it: pause, volume, mute, seek, quality, stop and favourites act on that TV. Sharing a link from your iPhone opens a picker so you choose the TV.

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

## iPhone Shortcut ("Play on TV")

One action opens a picker, so the Shortcut needs no list of TVs and nothing to edit when you add one:

1. Receive **Any** input from **Share Sheet**; if there's no input: **Get Clipboard**.
2. **URL Encode** the Shortcut Input.
3. **Text**: `http://<pi-ip>:8765/share?u=` followed by the URL Encoded Text.
4. **Open URLs** with that Text.
5. Details: Show in Share Sheet on, Add to Home Screen. Optional: Settings → Accessibility → Touch → Back Tap → Double Tap → Play on TV.

The picker shows one card per TV, with the TV you used last first and outlined. Tap a card and it plays there, then opens that TV's remote. Each card shows the TV's name, brand, size and model, what it's playing, and a small dot that turns amber with "Buffering" if the stream stalls.

For a one-tap Shortcut that skips the picker, point it at `/play?u=<encoded>&target=bedroom` instead (use `living` for the living room).

The Kick app doesn't list Shortcuts in its share sheet: copy the link, then run the Shortcut.

Remote on the Home Screen: open `http://<pi-ip>:8765` in Safari → Share → Add to Home Screen. It opens on the All TVs view; tap a TV to control it.

## Password

Set `PASSWORD=<something>` in `~/tvremote/.env` on the Pi, then `docker compose up -d`. The remote then shows a password-only login page (no username) and remembers you for 180 days. The logout icon is at the top right. Each login is a random token kept (hashed) in `/data/sessions.json`; logging out ends it on the Pi, and changing the password logs every phone out. `curl -u :<password>` still works for scripts. A `/play` Shortcut that skips the picker can't show the login page, so end its URL with `&t=<password>`: `http://<pi-ip>:8765/play?u=<encoded>&target=bedroom&t=<password>`. Plain HTTP on your LAN, so it keeps casual users out, not a determined sniffer.

## Roku TVs, Chromecast and input switching

- Set `ROKU_LIVING` / `ROKU_BEDROOM` (TV addresses) in `.env` to get a TV card with power, volume, input and navigation buttons. On each TV: Settings → System → Advanced system settings → Control by mobile apps → Network access → Permissive.
- Set `INPUT_LIVING` / `INPUT_BEDROOM` (e.g. `HDMI1`) and the TV wakes and switches to that input whenever you play on it.
- The bedroom target is a Chromecast found by mDNS (name configurable in `/data/targets.json`). Pick the TV at the top of the remote, or add `&target=bedroom` to the Shortcut URL (a second Shortcut, "Play in Bedroom").
- `.env` (not committed) holds your TV addresses and inputs. Keep real addresses out of the repo.
- The Roku remote's own buttons (pause etc.) don't reach the Pi or the Chromecast: the TV doesn't forward them over CEC. The phone is the remote.

### How Kick/Twitch reach the Chromecast

By default the Chromecast fetches the stream itself (`direct`). `&via=proxy` on `/play` (or `TVREMOTE_CAST_VIA=proxy` in the container environment) makes the Pi fetch the HLS segments over its wired link and relay them to the Chromecast (`app/hlsproxy.py`). The relay path is token-guarded and only fetches public http(s) hosts. In testing, both modes buffer only while starting (about 10–17 s) on a strong Wi-Fi link.
## The TV's home screen

Between streams the living-room TV shows the app's own screens instead of the Pi desktop: **Ready** (with the address of the remote), an animated **Loading**, **Not live right now** when a channel is offline, **Stream ended**, and **Couldn't play** with the reason. It is a fullscreen window kept behind the streams; while a stream plays it is paused and draws nothing.

- **Turn it off or on** from the living-room TV's remote: the "Home screen on this TV" switch. Off shows the Pi desktop again. The app and the remote keep running either way. Turning it on restarts anything playing, so the stream window opens on top.
- Starts off with `TVREMOTE_SHELL=0` in the container environment; the switch's setting is saved in `/data/settings.json`.
- Playing no longer re-selects an input the TV is already on (that relaunched the input and flashed the screen): the app asks the Roku what it is showing first. This needs `ROKU_LIVING` and `INPUT_LIVING` set.
- The screens are written as a subtitle file timed ahead (one event per second for the clock, whose colon blinks), so nothing redraws; redrawing flashed the whole screen, and osd-overlay text never stayed on that window. The logo and text are centred as one group. If the home screen won't open: `docker exec tvremote cat /tmp/tvremote-shell.log`. It tries a plain software window first (`wlshm`), then `gpu`; on this Pi `SHELL_VO=gpu,wlshm` in `.env` is what is used. The Debug drawer shows nothing about it yet.

## On the remote

- **Colours:** while something plays, the remote takes the site's colour: Kick green, Twitch purple, YouTube red.
- **Refresh** restarts the current stream (the TV shows a Refreshing screen with a wheel). Changing quality shows its own screen. Starting a new source cancels any older one still loading: the newest tap wins.
- **Favourites** show a picture (Kick, fetched by your phone), a link to the channel, whether it is live, for how long, and the game. Live ones sort first; offline ones are dimmed. **Reorder** lets you drag a row by its handle.
- **One favourite per streamer:** saving the same channel name on Kick and on Twitch makes one favourite with a chip per site (a dot marks the site they are live on). Tap the row to watch where they are live, or a chip to pick the site. The "Saved" button unlinks just the site you are watching; the X removes the whole favourite. Matching is by channel name, so two different people with the same name on both sites would be merged.
- **Recent** shows the last few links you played, with a star to save them.
- **Seek bar** (videos): drag to any position. Live streams have no seek bar.
- **Sleep timer:** pick 15, 30, 60 or 90 minutes. When it runs out the app stops playback and sends the Roku a power-off (Roku has no way for an app to set its own sleep timer). Cancel from the same menu.
- **Stop** cancels a start that is still loading, and the TV goes straight to Ready.
- **Chat** (read-only): under the player while a Twitch or Kick channel plays. Twitch is read by the Pi and shown as a list; Kick embeds Kick's own popout chat page. The link icon opens the chat on the site.
- **TV off:** if the TV is in standby or not answering, the player shows "The TV is off" with a Turn on button. Switching the TV off stops a live stream on the Pi and pauses a video or the Chromecast, whether you used the app or the TV's own remote.
- **Debug drawer:** a "Stats on the TV" switch draws dropped frames, buffer and fps on the TV. It costs frames, so keep it off for normal watching.
- **Volume** on the TV shows as a block bar for 1.5 s; mute shows "Muted". The Pi remembers the volume you chose across restarts. The TV screens (Loading, Refreshing) take the site's colour too.

## Sources

| Source | Player |
|---|---|
| Kick, Twitch | streamlink → mpv (`1080p60,best`; Twitch ads disabled) |
| YouTube live (`/live` or `is_live`) | streamlink → mpv |
| YouTube video, everything else | mpv + yt-dlp, H.264 only, max 1080p |

Routing lives in the `SOURCES` table at the top of `app/app.py`; a new streamlink site is one line.

## API

`GET /play?u=&q=&target=&via=` · `POST /api/play` · `POST /api/cmd` (pause, mute, volume, volume_delta, seek, seek_to, stop, restart) · `GET/POST /api/sleep` · `GET /api/recents` · `GET /api/chat/feed` · `GET /api/status` · `GET /api/qualities` · `GET /api/search?q=` · `GET/POST/DELETE /api/favourites` · `GET /api/targets` · `GET /api/tv` · `GET /api/roku` · `POST /api/roku/key` · `GET /api/debug`

Add `?target=bedroom` to the API calls to act on the Chromecast. `/api/status` includes `hwdec` (should be `v4l2m2m` on the Pi) and `drops`. The **Debug** button on the remote opens a drawer with player state, mpv properties, versions, the command line and the log.

Changing the volume from the remote shows a short volume bar on the Pi's screen.

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
- **Volume bar or stats text not showing:** only the Pi's screen can show them; the Chromecast can't.
- **"osd-overlay: required argument data not set" in the log:** an old build; update (clearing an overlay needs an empty data argument).
- `/play` is a plain GET; set `PASSWORD` (above) if the network isn't trusted.
