# Ben's requests, 2026-11-06 (this long session): what was asked and where it stands

Status: DONE = built and deployed, CODE = committed but not deployed yet, OPEN = not finished, ASK = waiting on Ben.

## Security and storage
- Hash the guest-link tokens on disk: DONE. Then "show guest QR codes again": DONE (token derived from a seed + a key file, never saved; `TVREMOTE_INVITE_KEY` keeps the key out of /data).
- SQLite or JSON: answered (JSON via store.py for now; SQLite when released publicly).

## Launcher (the TV home screen)
- Renderer spike: DONE (cog in its own container, `docker/cog`; no live video in the browser: software decode).
- Data (thumbnail, viewers, avatar), two-minute refresh while showing: DONE.
- New web launcher at /tv with cards, profile pictures (Kick official API, Twitch lookup), six stream thumbnails: DONE.
- Group cards with member faces and live dots above the faces: DONE.
- Focus lag, no scale/shadow, cards updated in place, no moving glow: DONE.
- Cursor hidden over the browser (labwc window rule `com.igalia.Cog` + transparent theme): DONE on the Pi; Ben to confirm it is gone.
- Header: logo, clock, date, weather (Open-Meteo), other TVs' open apps: DONE.
- Weather settings: city box fixed, city search with suggestions (admin Settings): DONE (suggestions not yet clicked through in a browser).
- Old mpv launcher tiles retired; phone pad removed; TV Navigation arrows and OK drive the launcher: DONE.
- Live hover preview: OPEN (browser can do one capped preview; not built). Loading cover while the browser starts: OPEN.

## Remote UI
- Living room card: shows the TV's own app and position ("On the TV: YouTube 9:12"), title and channel for admins on the bedroom TV: DONE. Living room title listener is off (`NOWPLAYING_IPS`): ASK before turning on.
- Titles for Plex / Prime / Netflix: OPEN (the Roku only reports the app and position; Plex needs the server address and token).
- Desktop TV cards same height and aligned: DONE.
- "TV is off" panel restyled: DONE. Chat card avatars inline, ring not clipped: DONE. Group card info line below the chips: DONE. Suggested group card ("Save as group") restyled: CODE.
- Favourite taps broken by the spinner code, spinner only on the tapped section: DONE.
- Mute button instant and ordered: DONE. Watch on phone full screen, "Watch on this computer" with bundled hls.js, closes when the stream ends: DONE.
- Removed "It costs nothing to speak of" text: DONE.

## Playback on the Pi
- Streams silent (PipeWire remembered a mute): DONE.
- Multistream with chat, windows in mixed layouts (race): DONE.
- mpv's own on-screen controller (seek bar, address as title) over the picture: CODE (`osc=no` in config/mpv.conf; needs a rebuild).
- "Switching stream" card when a multistream collapses to one: CODE (quiet path).
- Warnings about the Pi/TV as a whole (heat) across the whole screen, not in one tile: CODE (`tv_banner`, window `tvr-banner`). NEEDS the new labwc rule on the Pi: copy `config/labwc-rc.xml` to `~/.config/labwc/rc.xml` and `kill -HUP $(pgrep -x labwc)` (ASK Ben first). Temperature warning threshold is 78 C: ASK whether to lower it.

## Still open
- Multistream sheet in the first browser tab: "Live now, suggested" adds did not seem to work. The add itself works (the API accepted it); the open tab's view was stale after a restart. Re-test after the next deploy; if it still fails, find why the sheet does not refresh.
- Offered, not asked for: always show which HDMI input a Roku TV is on, even while its own app runs.
- Parked from earlier: IR receiver, YouTube everywhere, app.py split.
