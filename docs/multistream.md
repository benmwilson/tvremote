# Multistream: several streams on the Pi's screen

Up to four streams at once on the living-room TV (the Pi's HDMI output), controlled from the phone. This note says how it
works, what is not yet proven on the real Pi, and a checklist to test it.

## How it works

- **One mpv window per stream.** The desktop (labwc) places a window only when it first opens, by its window name
  (`tvr-grid4-2`, `tvr-focus3-1`, ...). `config/labwc-rc.xml` (made by `tools/make_labwc_rules.py` from the layout table in
  `app/multi.py`) holds a rule for every layout, count and tile. Copy it once on the Pi:
  `cp ~/tvremote/config/labwc-rc.xml ~/.config/labwc/rc.xml && kill -HUP $(pgrep -x labwc)`.
  (`labwc --reconfigure` does not work over SSH: it needs `LABWC_PID`.)
- **Layouts:** `grid` (2-4 equal tiles) and `focus` (one big tile on top, the rest in a row below). Adding or removing a stream, or
  changing the layout, restarts all the windows (they can't be moved while playing). Swapping, Replace, quality and Refresh do not.
- **Decoder budget:** the Pi 4 decodes about one 1080p60 stream in total. Each window gets only the quality its size needs
  (Auto: 720p60 for the big focus tile, 480p for the rest). Four at 480p with hardware decoding was tested: 0 dropped frames.
  Per-stream quality picks are Auto, 1080p, 720p, 480p, 360p.
- **The Wall (`app/multi.py`)** owns the list, the layout, which stream the main window should play (`main_url`), sound, the
  selection, qualities. Tile 0 is the main player owned by `app.py` (`state`, `SOCK`); the others are started by the Wall with their
  own sockets (`/tmp/tvremote-mpv-t{i}.sock`).
- **Hot loading.** Where a window plays a stream's own address (`streamlink --stream-url`, played by mpv directly), changing
  its stream is one `loadfile` command, so nothing closes or opens. Used for: swap (Enlarge), Replace, per-window quality, a new link,
  a quality change and Refresh on a single stream, and ending multistream (the window makes itself full screen and reloads at full
  quality). Twitch is excluded (its ad filter lives inside streamlink's own pipe), so it restarts the window. Stream addresses are
  remembered for 5 minutes and the other quality is worked out ahead of time (`warm_addresses`), so a swap is quick.
  `reconcile_job` makes every window match the wall; queued changes converge, so quick taps can't duplicate or lose a stream.
  Set `TVREMOTE_HOT=0` to turn all of this off (every change then restarts a window, as before).
- **Three separate ideas in the UI:** *Controlling* (the stream the top card's title, pause, favourite, quality and the volume
  slider are for; chosen by tapping a picture, no window changes), *Big* (focus layout only; Enlarge swaps windows), *Sound*
  (any number of streams can be heard; only the first by default; each has its own volume).
- **Offline:** a window that ends by itself keeps its slot with a "Went offline" panel, an Offline tag and Try again. Taking it off
  is the X (it moves the others).
- **Sync:** reads each stream's playlist (`#EXT-X-PROGRAM-DATE-TIME`) and the player's position/cache, works out when each picture
  really happened and winds the leading windows back to the most delayed one (`seek -d relative+exact`, plays from its buffer).
- **The TV's home screen** (`app/shell.py`, a fullscreen mpv behind the windows) shows a background with soft glows, a name strip
  under every tile, and a "Loading" or "Went offline" panel in each slot.
- **Phone:** the Multistream card opens a bottom sheet (a native `<dialog class="sheet">`). The All TVs card lists every stream.
  Chat has a tab per stream (one Twitch feed per channel; Kick uses its own embedded chat).

## Not proven on the real Pi (checked only by unit tests and a stub UI)

- Single-stream Kick through direct mpv at 1080p60 (the direct path was tested at 480p and 720p only).
- `loadfile` swaps with real streams at different sizes, and how long they take.
- Sync's backwards seek on a live HLS stream, and whether Kick's playlists carry `PROGRAM-DATE-TIME`.
- A window making itself full screen after being a tile (and what its picture does).
- The volume bar scaled in the smallest focus tiles (480x270).
- Everything visual on the phone from the last round: the sheet, picture row, selects, platform cards, confirm sheets.

## Test checklist (after `git pull && docker compose up -d --build`)

Rebuilding stops what is playing on the Pi's screen. It does not switch the TV's input or turn it on. Do it when nobody is
watching the living-room TV.

**A. Single stream (the path that changed most)**
1. Start a live Kick channel from favourites. Smooth? Debug shows `v4l2m2m` and few dropped frames?
2. Change quality on the top card: does it change in place with no flash? Tap Refresh: same?
3. Start a different channel while one plays: does it load in place? Start an offline channel: do you get the usual "not live"?
4. Start a Twitch channel and a YouTube video: do they still play as before?
5. If anything is wrong, set `TVREMOTE_HOT=0` in the compose environment, rebuild, and tell me what happened.

**B. Multistream basics**
1. Add a second, third and fourth stream: windows in the right places, background names under each, nothing overlapping?
2. Grid and Focus toggle. Windows restart together and come back in the new layout.
3. Sound: only the first has sound. Switch on a second (speaker on its picture or its Sound button): both play. The volume
   slider and mute button act on the controlled stream; the volume bar draws in that window.
4. Tap each picture: "Controlling <name>" changes; title, pause, Favourite and quality follow it.

**C. Swapping and speed**
1. Focus layout: Enlarge a small stream. It should swap in place (no windows closing) in about a second or two.
2. Tap Enlarge quickly several times, on different streams: no stream may be duplicated or lost. The sheet shows a spinner and
   locks the buttons while it works.

**D. Removing, replacing, ending**
1. Remove the first stream (X): the next one takes its place. Remove down to one: it goes back to full screen.
2. Replace a stream with another: loaded in place (the Replace button, then pick or type a name).
3. Full screen on a stream: multistream ends and that stream is full screen at full quality (watch for a flash).
4. A stream going offline: its slot says "Went offline"; Try again works when it is back.

**E. Sync**
1. With two to four streams from the same event, tap Sync streams and read the note. Do the pictures line up by eye?
2. Note what it says if Kick has no timing tags ("no timing to compare").

**F. Phone UI**
1. Platform cards when a name is on both Kick and Twitch (with logos, live, viewers). Suggestions never show a playing streamer
   and show only the channel name.
2. Quality dropdowns and all other selects look like the buttons (chevron, rounded).
3. Confirm sheets: Move to the other TV; Revoke, Remove and Clear finished on guest links.
4. In multistream: Stop is replaced by Remove, with Stop all streams below. Enlarge appears in Focus when the controlled stream is
   not the big one. Sleep timer is in the TV card.
5. Recents show LIVE chips and what is being played. Chat tabs, and the "Open on the site" link inside the open chat.
6. All TVs card lists every stream, left-aligned, with game and live time.

**G. Odd cases**
1. Turn the TV off while multistream runs, turn it on, start something: it should be a single stream, not the old wall.
2. Roku volume slider shows its number bubble and the ruler while dragging.
3. Power button shows On or Off from the Roku's own report; hold to flip it by hand; Debug has the same switch.

When something fails, send: `docker logs --tail 80 tvremote`, a photo of the TV, and Debug (Multistream section) if you can open it.

## Open ideas

- Auto-remove a stream a few minutes after it goes offline (it would restart the other windows once).
- Sync using the platform's own clock if Kick's playlists have none.
- **Multistream on the bedroom Chromecast** (Ben wants this). Three routes, none quick:
  1. *A custom Cast receiver page* with several `<video>` players. Needs a Google Cast developer registration and a publicly
     reachable HTTPS page for the receiver. The plain Chromecast dongle has one hardware decoder, so more than two small streams
     will not play well. Likely a dead end on this hardware.
  2. *The Pi composes one picture and the Chromecast plays that as a single HLS stream* (decode, tile with ffmpeg, hardware
     encode with `h264_v4l2m2m`). Works with any Chromecast, but costs the Pi's decoder and CPU (can't also run the living-room
     wall at full tilt), adds 8-15 s of delay, and a composite of 720p30 is realistic.
  3. *Capture the Pi's own multistream screen and send that.* The existing wall already does the compositing; `wf-recorder` or
     `grim` plus ffmpeg hardware-encodes the output and the Chromecast plays it. Needs the Pi's output on (or a headless output).
  First step: measure. While the wall runs, encode a capture with `h264_v4l2m2m` and look at CPU, dropped frames and encode fps.
  Also needed: the dongle's exact model (generation) from its mDNS name.
- The same in-place idea for the Chromecast (it already keeps what it plays across a Pi restart).
- Twitch account link (needs a Twitch developer app) and real Twitch profile pictures.
