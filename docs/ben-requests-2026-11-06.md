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
- "TV is off" panel restyled: DONE. Chat card avatars inline, ring not clipped: DONE. Group card info line below the chips: DONE. Suggested group card ("Save as group") restyled: DONE.
- Favourite taps broken by the spinner code, spinner only on the tapped section: DONE.
- Mute button instant and ordered: DONE. Watch on phone full screen, "Watch on this computer" with bundled hls.js, closes when the stream ends: DONE.
- Removed "It costs nothing to speak of" text: DONE.

## Playback on the Pi
- Streams silent (PipeWire remembered a mute): DONE.
- Multistream with chat, windows in mixed layouts (race): DONE.
- mpv's own on-screen controller (seek bar, address as title) over the picture: DONE (`osc=no`).
- "Switching stream" card when a multistream collapses to one: DONE (quiet path).
- Warnings about the Pi/TV as a whole (heat) across the whole screen, not in one tile: DONE (`tv_banner`, window `tvr-banner`; labwc rules updated on the Pi, backups `rc.xml.bak-cursor` and `rc.xml.bak-banner`). Temperature warning threshold is 78 C: ASK whether to lower it.

- Volume / mute / paused notices on the same strip for one full-screen stream, with a Settings choice (strip or classic text) and a test-warning button: DONE. Several streams keep text in the stream's own window.
- People options (make admin, reset password, sign out, remove) as one compact row of buttons: DONE.
- Launcher shows one tile per streamer (xqc / buddha were doubled): DONE.
- Open pages reload themselves after the app restarts (a stale tab ran an old script): DONE (takes effect from the next restart after this one).

## Still open
- Multistream sheet "Live now, suggested" adds: re-tested on a fresh page, four streams added quickly, all worked. The failure was a stale tab after a restart (see the auto-reload above).
- Live hover preview in the launcher: needs a new multi-session HLS relay (playlist + segments through the app, so the TV's browser never talks to the internet); design in this log's history: one preview after 1.5 s on a live card, 320x184, hls.js (bundled). NOT built.
- Offered, not asked for: always show which HDMI input a Roku TV is on, even while its own app runs.
- Parked from earlier: IR receiver, YouTube everywhere, app.py split.
