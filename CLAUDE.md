# tvremote

Play Kick / Twitch / YouTube / any link on the living-room TV from a Raspberry Pi, controlled from an iPhone.
Runs as a single Docker container on the Pi.

## Target flow
1. On iPhone: copy a link (or Share from Safari) → run the "Play on Pi" Shortcut.
2. Shortcut opens `http://<pi-ip>:8765/play?u=<url-encoded link>` in Safari.
3. Pi starts playback on the TV, switches the TV input to the Pi (CEC), then redirects to the web remote.
4. Web remote shows what's playing and has controls: pause, volume, mute, seek, quality, stop, favourites.

The Shortcut needs no SSH: Receive any input (fallback: Get Clipboard) → URL Encode → Text `http://<pi-ip>:8765/play?u=<encoded>` → Open URLs.

## Supported sources
Route by URL host. Keep the routing in one table in app.py so new sites are one line.

| Source | Player path | Notes |
|---|---|---|
| Kick (live) | streamlink → mpv | `1080p60,best`. |
| Twitch (live + VODs) | streamlink → mpv | `1080p60,best`, add `--twitch-disable-ads`. Channel names like `twitch.tv/xqc`. |
| YouTube (videos) | mpv + yt-dlp | Force H.264 (`vcodec^=avc1`, max 1080p). Pi 4 can't hardware-decode VP9/AV1. |
| YouTube (live) | streamlink → mpv | HLS is H.264 already. Detect `/live`, `/@channel/live` or `is_live` via yt-dlp. |
| Everything else | mpv + yt-dlp | Whatever yt-dlp supports (Reddit, X, Vimeo, direct .mp4/.m3u8…). Same H.264 preference. |

- Shared text can arrive as "Watch X on Kick https://…", so always extract the first URL.
- A bare word typed in the UI = Kick channel by default, with a site toggle (Kick / Twitch / YouTube search).
- YouTube search from the UI: `yt-dlp "ytsearch10:<query>" --flat-playlist -J`, show results as tappable cards.
- Quality picker: streamlink sources list `streamlink --json <url>` streams; yt-dlp sources offer 1080/720/480.
- Show the source (Kick/Twitch/YouTube/other) as a badge on the now-playing card.
- Favourites store `{name, url, source}` and work for any source.

## Repo layout (to build)
```
app/app.py              Flask + waitress backend (draft exists, review and finish)
app/index.html          single-file phone-first remote UI
app/cast.py             Chromecast target (pychromecast: discovery, play, controls, status)
app/hlsproxy.py         optional Pi-side HLS relay for the Chromecast (token-guarded)
app/roku.py             Roku ECP control (power, volume, input, keys)
app/chat.py             Twitch chat: the TV overlay (off by default) and the feed for the phone's chat view
app/logo.svg, icon-*.png the project mark (tools/make_icons.py draws the PNGs)
app/targets.py          cached TV cards (never waits on a slow TV) and playback health
app/shell.py            the TV's home screen: a fullscreen mpv window behind the streams (idle / loading / not live / ended / error)
app/multi.py           several streams on the Pi's screen (up to 4, grid or focus): layout table, one mpv window per stream; config/labwc-rc.xml (made by tools/make_labwc_rules.py) places the windows, copied once to ~/.config/labwc/rc.xml on the Pi
app/mpvipc.py           one command over an mpv IPC socket (shared by the player and the home screen)
app/accounts.py, access.py, activity.py   accounts and roles, access requests and guest links, last-action per person
app/sessions.py, throttle.py   login tokens (hashed on disk) and failed-login counting: the security-sensitive state, kept out of app.py
app/store.py            the one safe JSON writer (unique temp file, then rename); every data file goes through it
app/kick.py, tlscallback.py   Kick API (live status, link, chat) and the self-signed https redirect listener
app/discover.py, tvconfig.py  finding Rokus/Chromecasts/the Pi screen, and the saved TV settings (/data/targets.json)
tests/                  unittest suite (python -m unittest discover -s tests) and a stub API server for checking the UI
config/mpv.conf         baked into the image at /home/app/.config/mpv/mpv.conf
docker/entrypoint.sh    set display to 1080p60, register CEC name, start the app
Dockerfile
compose.yaml            Portainer-compatible stack
.env.example            PI_UID/PI_GID, WAYLAND_DISPLAY, CEC_PHYS, PORT
README.md               setup, iPhone Shortcut steps, troubleshooting
```

## Docker design
mpv runs inside the container but draws on the Pi's host Wayland desktop and plays through the host's PipeWire,
using the Pi's hardware decoder. So the container needs the host's sockets and devices passed through.

- Base: `debian:trixie-slim` (arm64), matching the host OS so mpv/libdrm/v4l2 versions behave the same as on the host.
- apt: `mpv cec-utils v4l-utils wlr-randr python3 python3-venv pipx ca-certificates curl libpipewire-0.3-0 pipewire-bin`.
- Python venv: `flask waitress`. pipx (or venv): `streamlink yt-dlp`, latest. Rebuilding the image updates them.
- Run as uid/gid 1000 (ben) so the host's Wayland and PipeWire sockets accept the connection.
- compose.yaml essentials:
  ```yaml
  services:
    tvremote:
      build: .
      image: tvremote:latest
      container_name: tvremote
      restart: unless-stopped
      network_mode: host            # port 8765 on the LAN, no NAT
      user: "1000:1000"
      group_add: ["video", "render"] # use numeric GIDs from the host if names don't resolve
      devices:
        - /dev/dri:/dev/dri
        - /dev/cec0:/dev/cec0
        - /dev/video10:/dev/video10   # H.264 decoder (v4l2m2m); add 11-19 if mpv probes them
        - /dev/video11:/dev/video11
        - /dev/video12:/dev/video12
      volumes:
        - /run/user/1000:/run/user/1000   # Wayland + PipeWire sockets
        - tvremote-data:/data             # favourites.json, player.log
      environment:
        - XDG_RUNTIME_DIR=/run/user/1000
        - WAYLAND_DISPLAY=wayland-0
        - PIPEWIRE_RUNTIME_DIR=/run/user/1000
        - TVREMOTE_PORT=8765
        - TVREMOTE_CEC_PHYS=1.0.0.0
        - TVREMOTE_CEC_NAME=HunnidPi
  volumes:
    tvremote-data:
  ```
- `entrypoint.sh`: wait for `$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY` to exist (the desktop may still be starting after boot),
  run `wlr-randr --output HDMI-A-1 --mode 1920x1080@60`, run `cec-ctl -d /dev/cec0 --playback -o HunnidPi`, then exec the app.
- Healthcheck: `curl -fs http://127.0.0.1:8765/api/status`.
- Watchtower runs on the homelab server, not the Pi, and this image is built locally anyway. Update by rebuilding.
- If something can't work in the container (most likely hardware decode or CEC permissions), find out from the logs,
  try the fixes (numeric GIDs, more /dev/video nodes, `privileged: true` as a last-resort test only), and tell Ben what
  you found before falling back to a native install.

## Dev workflow
- Code is edited on Ben's Windows PC and pushed to GitHub.
- SSH from Claude Code is allowed since 2026-11-05 with a dedicated key (`~/.ssh/tvremote_claude`, comment `claude-code-tvremote`, address in `CLAUDE.local.md`):
  `ssh -i ~/.ssh/tvremote_claude -o BatchMode=yes ben@<pi-ip> "<command>"`. Reading is free (logs, `docker ps`, status, files).
  Ask Ben first before anything that stops, restarts, rebuilds or changes what is playing on a TV, or edits the Pi's system settings;
  never touch what a TV is showing without asking. If SSH fails, fall back to giving Ben one copy-paste block for Termius.
  To revoke: delete the `claude-code-tvremote` line from `~/.ssh/authorized_keys` on the Pi.
- Deploy: `cd ~/tvremote && git pull && docker compose up -d --build` (Ben runs it, or Claude after asking, when nobody is watching the TV)
- Test from the PC over HTTP (no SSH needed): `curl http://<pi-ip>:8765/api/status`
- Logs (Ben runs on the Pi): `docker logs --tail 50 tvremote`

## Hardware
- Raspberry Pi 4 (address, hostname and user in the untracked `CLAUDE.local.md`), uid 1000, Debian Trixie (Raspberry Pi OS), labwc Wayland desktop, wired Ethernet, Docker installed, ben in the docker group
- HDMI-A-1 → the living-room Roku TV; the Pi appears in TV CEC as "HDMI 1: <CEC name>", physical address 1.0.0.0

## Verified working on the host (keep these in the container)
- `mpv.conf`: `vo=dmabuf-wayland`, `hwdec=v4l2m2m`, `fs`
  - The only smooth combo (1–2 dropped frames/min at 1080p60).
  - `vo=gpu` (Vulkan or OpenGL) drops 40+ frames/sec. Vulkan also fails swapchain under labwc.
- Display must be 1920x1080@60. The 4K@30 default caused drops. dmabuf-wayland does not scale,
  so stream resolution should match the display (720p sits small and off-centre at 1080p display).
- Kick: `streamlink --player mpv <url> 1080p60,best` (the Kick plugin works in streamlink 8.6.3).
- Other sites: mpv + yt-dlp with `--ytdl-format="bv*[vcodec^=avc1][height<=1080]+ba/b[height<=1080]/b"`.
- CEC input switch: `cec-ctl -d /dev/cec0 --playback -o HunnidPi`, then `--to 0 --image-view-on` and
  `--active-source phys-addr=1.0.0.0`.

## Constraints
- The TV remote can't control the Pi (the Roku TV forwards no CEC key presses). The phone is the remote.
- The Kick iOS app hides Shortcuts from its share sheet, hence Copy → run Shortcut (Back Tap).

## Setup instructions for Claude Code
Work through these in order. Use the TaskCreate list to track them. Stop and ask Ben before anything destructive on the Pi.

### 0. Preflight
- On the PC: `gh auth status` must be logged in. If the GitHub remote doesn't exist yet: `gh repo create tvremote --public --source . --push`
  (public so the Pi can clone without credentials. Ask Ben first; if he wants private, he runs `gh auth login` on the Pi.)
- Ask Ben to run this on the Pi and paste the output:
  ```
  docker --version; docker compose version; id; getent group video render
  ls -l /dev/cec0 /dev/video1* /dev/dri; ls /run/user/1000 | grep -E 'wayland|pipewire'
  ```
  Use the real GIDs from this output in compose.yaml.

### 1. Build the repo
- Create every file in "Repo layout". Review the draft `app/app.py` against this document and fix anything wrong.
  Store favourites and the player log under `/data`.
- `app.py` endpoints: `GET /` (UI), `GET /play?u=&q=` (start + 303 redirect to `/`), `POST /api/play`, `POST /api/cmd`
  (pause, mute, volume, volume_delta, seek, stop, restart with quality), `GET /api/status`, `GET /api/qualities`,
  `GET /api/search?q=` (YouTube), `GET/POST/DELETE /api/favourites`.
  mpv IPC socket at `/tmp/tvremote-mpv.sock` inside the container. Kill any existing player before starting a new one.
  CEC input switch in a background thread so `/play` returns fast.
- `index.html`: now-playing card (title, source badge, live/position), big play/pause, volume slider + mute,
  seek ±10s/±60s (hidden for live), quality picker, stop, paste-a-link box with Kick/Twitch/YouTube toggle,
  YouTube search results, favourites grid with add/remove. Poll `/api/status` every 2s.
  Add apple-mobile-web-app meta tags and a manifest so it works from the Home Screen.
- `.gitignore`: `.env`, `__pycache__/`.
- Commit in logical chunks and push.

### 2. Deploy
Ben runs on the Pi (give him the exact block with the real repo URL filled in):
```
pkill -x mpv; pkill -f streamlink
git clone https://github.com/<user>/tvremote ~/tvremote || (cd ~/tvremote && git pull)
cd ~/tvremote && docker compose up -d --build
docker logs --tail 30 tvremote
```
The first build on a Pi 4 takes several minutes. He pastes the output back. Fix any failures in the repo, push, and have him
run `git pull && docker compose up -d --build` again.

### 3. Test (all must pass)
- `curl -s http://<pi-ip>:8765/api/status` returns JSON.
- For each source, start via `curl "http://<pi-ip>:8765/play?u=<encoded>"`, wait 10s, then check `/api/status`
  shows running with a title. If something fails, ask Ben for `docker logs --tail 50 tvremote`:
  - Kick: `https://kick.com/capped` (or any live channel)
  - Twitch: a live channel from `https://www.twitch.tv/directory`
  - YouTube video: `https://www.youtube.com/watch?v=aqz-KE-bpKQ`
  - YouTube live: any `youtube.com/@<channel>/live` that's live
  - Other: a Reddit or Vimeo video link
- Status must show hardware decoding is active (expose mpv's `hwdec-current` in `/api/status`; it should be `v4l2m2m`).
- Controls: pause, volume 50, mute, seek +10 on the YouTube video, quality switch on Kick, stop. Check that each changes `/api/status`.
- Dropped frames: `frame-drop-count` should stay under ~5/min at 1080p60.
- Reboot test: ask Ben to run `sudo reboot` on the Pi, then confirm `/api/status` responds and he sees 1080p on the TV.
- Ask Ben to confirm the picture and sound on the TV for at least Kick and YouTube. You can't see the screen.

### 4. iPhone Shortcut (give Ben these steps; you can't do this part)
"Play on Pi":
1. Receive **Any** input from **Share Sheet**; if there's no input: **Get Clipboard**.
2. **URL Encode** → Shortcut Input.
3. **Text**: `http://<pi-ip>:8765/play?u=` + URL Encoded Text.
4. **Open URLs** → Text.
5. Details: Show in Share Sheet on, Add to Home Screen. Optional: Settings → Accessibility → Touch → Back Tap → Double Tap → Play on Pi.
Replace the old SSH-based "Play on Pi" action with these steps.
Also: in Safari, open `http://<pi-ip>:8765` → Share → Add to Home Screen ("TV Remote").

### 5. Done when
- All tests in step 3 pass, Ben confirms picture and sound on the TV, README matches what was built, everything is pushed.

## Two TVs (targets)
- Living room: Roku TV, Pi 4 on HDMI → mpv in the container (target type `pi`).
- Bedroom: Roku TV with a Chromecast plugged in (target type `chromecast`; model unknown, detect Google TV vs older dongle from the mDNS model name).

### Targets
- `pi` = mpv on the living-room TV. `chromecast` = discovered with pychromecast via mDNS (container is `network_mode: host`).
- Names: "Living room" (pi) and "Bedroom" (the Chromecast's friendly name; renamable in `/data/targets.json`).
- UI: opens on All TVs (a card per TV); tap one for its remote. `/share?u=<link>` is the picker the iPhone Shortcut opens. All controls and status act on the selected target; an unknown `?target=` is refused, never sent to the Pi.
- `/play` accepts `&target=<living|bedroom>` (default living). The Shortcut can have a second version "Play in Bedroom" that adds `&target=bedroom`.

### Chromecast playback
- YouTube (video and live) → the Chromecast's YouTube receiver (pychromecast quick_play "youtube").
- Kick / Twitch / other → resolve on the Pi (`streamlink --stream-url <url> <quality>` or `yt-dlp -g` with a single progressive H.264+AAC MP4 ≤1080p), then cast via the Default Media Receiver.
- If the Chromecast can't fetch the URL (CORS / 403 / expiring tokens), fall back to proxying through the Pi: `streamlink --player-external-http` on a fixed port, cast that URL.
- Pick the quality on the Pi before casting; don't rely on the Chromecast's own switching.

### Controls and status
- play/pause, seek, volume, mute, stop → MediaController / set_volume / quit_app.
- Status from media_status in the same `/api/status` shape (title, position, duration, live, paused, volume, error) so the UI doesn't care which target it is.
- Error text as on the Pi side (e.g. "Chromecast couldn't load the stream").

### Tests (bedroom TV on the Chromecast input)
Kick, Twitch, YouTube video, YouTube live, direct mp4 to the Chromecast; every control on each; switching targets mid-session doesn't break the other TV; the Chromecast is found again after a Pi reboot and a Chromecast reboot. Tell Ben what to look for on the bedroom TV for each.

## Roadmap: all-in-one system (Ben's direction)
Goal: anyone can run the container on a Pi and get a remote that finds and controls their TVs. The Pi's own HDMI output is optional.
- **Targets become a discovered list**, not hard-coded "living"/"bedroom": the Pi's HDMI screen (when a display is present), Chromecasts (mDNS), Roku TVs (SSDP/ECP), maybe AirPlay (both Rokus advertise it). Each target has a user-chosen name, saved in `/data`.
- **Setup wizard** in the web UI: scan the network, show what was found, name each device, pick the HDMI input to switch to, test it (play a short clip / press a key), save. Roku needs "Control by mobile apps → Permissive" and the wizard should say so and detect the 403.
- **Headless Pi** (no display): skip the mpv target; Chromecast and Roku targets still work.
- **No addresses in the repo**: discovery and `/data` config replace the `.env` entries (`ROKU_*`, `INPUT_*`).
- Before this: settle Kick-on-Chromecast quality (soak-test direct vs relay), finish the per-source bedroom tests, then refactor `tgt()` and the picker around a target registry.
- Open idea: chat overlay for the Chromecast via DashCast or a custom receiver (the default receiver can't draw over video).

## Todo
- **Done 2026-11-06 (live):** admin TV cards in Settings (rename, Roku address, HDMI inputs, remove), "Set up automatically" (scan; saves only when every guess is clear, else opens the wizard; the scan on the real network matches the saved setup), the Pi-moved banner for admins (`compute_moved`, checked every minute, compares the saved HDMI port with the Pi's real CEC address), Chromecast and phone-watch lookups in-process. Still open from the list: pairing codes (AirPlay, parked), Twitch profile pictures (needs a developer app), `app.py` is about 3500 lines (split it), idle/polling is fine on the Pi.
- **Transition and status screens (2026-11-06, live):** a stream or quality change in place first puts a card over the picture (`shell.transition_screen`, window `tvr-transition` in `chatwin.py`, full screen above the stream, same shared background picture), then does the change behind it and says "Quality: 720p" after. The home screen's loading, quality, refresh, offline, ended and error screens use the same layout (channel large, status under it). Multi-view tiles keep the small on-screen wheel. Seen on the Pi via `/api/screen.png`; Ben has not yet confirmed how it looks and feels on the TV.
- **Lesson (in-process libraries):** import yt-dlp BEFORE streamlink (`inproc._load`). The other order makes streamlink's own requests fail ("'Urllib3UtilUrlPercentReOverride' object has no attribute 'sub'") and every stream silently falls back to the slow command path; watch the log for "No direct stream address". With the fix, tap to first picture is about 3.2 s (8.6 s at the start of the audit).
- **Multi-stream groups (2026-11-06, done and live):** saved sets of 2-4 streams that start together in multi-view (`app/groups.py`, `/api/groups*` in app.py, per-account `groups.json`). Created from the Multi-view sheet ("Save these as a group") or suggested after the same set was on screen together (a minute or more) in two sessions (`_groups_loop` records the owner's history; dismissible). Shown with stacked avatars (`avatarGroup`) in quick play (groups with 2+ live), a Groups section above favourites, and the Multi-view sheet. Tapping replaces what is on the TV (confirm if something plays). Guests have no groups. Not yet seen: a suggestion appearing on a real phone (needs two sessions of watching); renaming from the UI (the endpoint exists); groups on a Chromecast TV (they play on the Pi's screen only).
- **Performance audit (2026-11-06):** streamlink and yt-dlp run inside the app (`app/inproc.py`, command fallback), play taps answer at once (`play_async`), the remote page is gzip + ETag. Measured on the Pi: tap to answer 6.4 s -> 0.01 s, tap to first picture 8.6 s -> ~4.5 s, detect 11 s -> 0.14 s, search 7.6 s -> 1.7 s. Still open: idle CPU and polling rate, the connection time to Kick's CDN, `cast.py` still spawns the commands, `yt-dlp -g` for watch-on-phone and the Chromecast.
- **Pi network drops (2026-11-06):** NetworkManager reported a false IP conflict (host 00:00:00:00:00:00) about 58 minutes after each boot and dropped the Pi's own LAN route. Fixed with `ipv4.dad-timeout=0` (`/etc/NetworkManager/conf.d/99-no-dad.conf`, applied from boot), a router DHCP reservation for the Wi-Fi MAC, and a watchdog that fully reconnects (and reboots after 5 minutes) when wlan0 has no local route. Check around an hour after the next boot that it did not recur; if it does, give the Pi a static address.
- **Chat on the TV (2026-11-06): done for one stream and for multi-view, Twitch and Kick** (picture top-left, chat window on the right, now-playing bar under the picture, one shared background picture; windows in `app/chatwin.py`, pictures from `tools/make_chat_bg.py`). Next: transition screen when a stream swaps and nicer idle/loading screens on the new background (small windows, not OSD), then the smaller todos below. See `docs/chat-on-tv.md`.
- **Not yet checked on real devices:** Watch on phone playback on Ben's iPhone (Chrome desktop can't play HLS), scanning the idle-screen QR code from the couch, the "Switching to 720p" wheel on the TV (`osd_swap`), the new chat header buttons and the Appearance toggles on the phone.
- **Done 2026-11-05 (all live on the Pi):** TV registry (TVs first, Pi and Chromecast per TV, TV-only cards, one card with a Pi | Chromecast switch for a shared TV, "Continue on / Move to" button, setup screen with Cancel and Use HDMI N), Watch on phone (inline sticky player, stream picker, closes when the stream ends), Auto search (Kick and Twitch, asks which, YouTube fallback), quick-play tiles or cards when idle, Appearance settings, security pass (headers, 1 MB limit, admin-only home screen switch, guests limited to Kick/Twitch/YouTube links), fixes for a quick second add to multi-view, dropped frames after a multi-view collapses in place, stale quality picker, favourite titles.
- **Lesson:** a page script syntax error blanks the whole remote and no Python test sees it: `tests/test_ui_syntax.py` now runs `node --check` on `app/index.html`. Run the full suite before every deploy.
- **Chromecast is found by its device id** (done 2026-11-06): `tvs.json` keeps `uuid` next to `device`; the id wins over the name, and a name match with no id saved learns it (`remember_cast_uuid`). Not yet seen with a real rename in Google Home.
- **Pairing codes in the wizard** if a TV needs one (AirPlay on a Roku set to "require code"): start pairing from the TV's card, type the code the TV shows, keep the credentials in `/data`.
- **Dependencies are locked** in `requirements.lock` (every package and sub-dependency, frozen from the Pi's container 2026-11-06) and the `Dockerfile` installs from it. To bump yt-dlp or streamlink: `docker compose build --build-arg YTDLP_VERSION=<new> --build-arg STREAMLINK_VERSION=<new>`, and once they work put the same versions in the lock file.
- **Roku input names can be stale** (2026-11-06): cards now show an HDMI port as "HDMI 2" instead of the TV's own label. The wizard still does not warn when a label looks wrong.
- **AirPlay as a third way to play** (Ben can AirPlay Kick from his iPhone to the living-room Roku): a TV card gets an "AirPlay (built into this TV)" switch next to the Pi and the Chromecast, the Living room card then plays instead of "TV controls only", and the Continue button becomes a "Continue on..." list of every way to play. Unknown: whether a Pi can push to a Roku's AirPlay 2 receiver (try `pyatv` `stream.play_url` in the running container first; the living-room TV is the roommate's, so ask before the spike).
- Look at the new help section, QR code and login eye icon on the Pi / in Chrome (not seen on screen yet).

- **Watch on the phone** (keep watching when leaving the room): a "Watch on phone" button on the now-playing card opens a bottom sheet (`dialog.sheet`) with a `<video playsinline controls>`. First version: the Pi hands the phone the stream's own address (`stream_url()`, already cached 5 min) and iOS plays HLS natively. Works for Kick, YouTube live and other HLS; YouTube videos need a progressive MP4 from yt-dlp; Twitch would show ads (its ad filter lives inside streamlink's pipe) so it needs the relay. If a CDN refuses the phone (headers, tokens), extend `hlsproxy.py` to several token-guarded sessions served over the LAN. Notes: the phone sits some seconds behind the TV; quality should drop to 720p; guests may use it only if Ben says so; check it doesn't change what the TV plays.

- **Pi health and temperature protection** (Ben, 2026-11-06): the Pi hit 79.8-80.8 °C with two 1080p60 tiles (throttles at 80 °C). Add a "Pi health" section to the admin or Debug page: CPU use (total and per mpv window), temperature, `vcgencmd get_throttled` decoded (under-voltage, throttled now / since boot), memory and swap, Wi-Fi signal, uptime. Then protection: above ~78 °C show an admin banner and a "Pi running hot" note on the TV, above ~82 °C drop multi-view tiles to 480p (or refuse more streams) until it cools, and say so on the phone. Keep sampling cheap (read `/sys/class/thermal`, `/proc`, once every few seconds only while the page is open). Check the case, heatsink and fan too.
- **Multi-view at 1080p (measured 2026-11-06):** two 1080p60 tiles drop about 30 frames a second each and the sync drifts to 30 s+; the decoder itself drops nothing, the loss is in showing a 1080p picture scaled into a half-screen window. Idea: Auto never above 720p for a tile that shares the screen; keep the manual 1080 pick, with a "may drop frames" note.

### Test on the Pi (nothing from the last multi-view round has run there; checklist A-G in `docs/multi-view.md`)
Rebuild when nobody is watching the TV. The Pi is now on the bedroom TV (Wi-Fi), so "living" in the notes means the Pi's own screen.
- **A. Single stream first** (the core path changed): Kick at 1080p60 via direct mpv, then quality change, Refresh and a different channel each loading in place with no flash; an offline channel; a Twitch stream; a YouTube video. If one misbehaves: `TVREMOTE_HOT=0` in the compose environment and rebuild, and report what happened.
- **B. Multi-view basics:** add up to four streams, Grid/Focus toggle, sound per stream, tap a picture to choose which stream the top card controls.
- **C. Swapping:** Enlarge in Focus swaps in place; tap Enlarge quickly several times, no stream duplicated or lost.
- **D. Removing and replacing:** remove the first stream, Replace, Full screen from a tile (watch for a flash), a stream going offline ("Went offline", Try again).
- **E. Sync streams button:** with 2-4 streams of one event, does it line the pictures up? Does it say "no timing to compare" when Kick's playlists carry no `PROGRAM-DATE-TIME`? Unproven: its backwards seek on live HLS.
- **F. Phone UI from the last round:** the bottom sheet, platform cards with logos, styled selects, confirm sheets (move TV, revoke/remove/clear guest links), Stop vs Remove in multi-view, recents with LIVE, chat tabs, the All TVs card listing every stream.
- **G. Odd cases:** TV off while multi-view runs then on (should start a single stream), Roku volume bubble, power button states.
- Also unproven there: a tile window going full screen in place; the volume bar in the smallest focus tiles; the Pi stream surviving a container restart (it can't today).
- When something fails send `docker logs --tail 80 tvremote`, a photo of the TV, and the Debug page's Multi-view section.

### Multi-view and streaming ideas
- **Multi-view on the bedroom Chromecast** (Ben wants it). A custom receiver page is a likely dead end (the plain dongle has one decoder, and it needs Cast developer registration). Realistic: the Pi makes one picture and the Chromecast plays it, either by combining the streams on the Pi or by capturing the Pi's own multi-view screen, hardware-encoded (`h264_v4l2m2m`). Expect 8-15 s delay and about 720p30, and the Pi can't also run the wall at full quality. First step: measure CPU and encode fps with the wall running. Needs the Chromecast's exact model from its mDNS name.
- Auto-remove a stream a few minutes after it goes offline (restarts the other windows once).
- Sync using the platform's own clock if Kick's playlists have none.
- The same in-place loading for the Chromecast.
- Twitch account link and real Twitch profile pictures (needs a Twitch developer app).
- Kick embedded chat can't log in inside a frame (our send box is hidden behind `KICK_OWN_SEND=false`).

### Housekeeping and security
- `app.py` is about 2500 lines: split it.
- Security review leftovers: refuse private addresses in `/play` for guests, `MAX_CONTENT_LENGTH`, security headers, pin and audit dependencies.
- Router: DHCP reservation for the Pi (`192.168.1.149`, MAC `D8:3A:DD:3E:A9:76`), and for the Rokus and Chromecast.
- Rotate the Kick client secret (it was pasted in chat once).
- Optional: let Claude Code SSH to the Pi with a dedicated key, then change the "no SSH" rule above.

## Conventions
- Canadian spelling in UI text (favourites, colour).
- Keep the UI a single dependency-free HTML file, dark, large touch targets, works as an iOS home-screen web app.
- No secrets in the repo.
