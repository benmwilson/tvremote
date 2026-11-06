# Chat on the TV: what is known (2026-11-05 spike)

Goal (Ben): show stream chat on the TV itself, with the stream (or the multi-view tiles) shifting over to make room, like a column on the right.

## What already exists
- `app/chat.py` draws Twitch chat as text over the stream with mpv `osd-overlay` (off by default, Twitch only, emotes as text). Nothing for Kick.
- Kick will not give its chat to anything but a real browser (kick.com refuses non-browser clients). The phone embeds Kick's popout chat page for that reason.

## What the spike proved (bedroom TV, omie 1080p60, Pi 4)
- **Shifting the stream works with no extra window.** `set_property video-margin-ratio-right 0.25` on the main mpv (socket `/tmp/tvremote-mpv.sock`, `vo=dmabuf-wayland`) scaled the picture to a 1440x810 box, vertically centred, leaving the right 480 px empty. Measured on a `grim` screenshot: picture spans x 0-1432, y 136-944. Set it back to 0 to restore. The other margins (`-left`, `-top`, `-bottom`) should work the same: untested. Multi-view tiles are separate windows placed by labwc rules (`config/labwc-rc.xml`), so a chat column there means shrinking the layout table in `app/multi.py` and regenerating the rules with `tools/make_labwc_rules.py`.
- **A Chromium chat window is too heavy on a Pi 4.** Chromium 156 (apt, in the running container, `--no-sandbox --ozone-platform=wayland --class=tvr-chat --app=https://kick.com/popout/omie/chat`) used about 37% of a core and 255 MB for the chat page alone; the stream then lagged and buffered (Ben saw it) and the drop counter reached 736. So: do not run a browser on the Pi next to a 1080p60 hardware-decoded stream.
- A labwc `windowRule` with `identifier="tvr-chat"` and MoveTo/ResizeTo placed the window correctly in the right column (rule was removed again; rc.xml restored from `rc.xml.bak-before-chat-spike`).

## Options for next time (cheapest first)
1. **No browser: draw the chat with mpv itself** in the margin that `video-margin-ratio-right` frees: `osd-overlay` ASS text on the main player (chat.py already does this for Twitch, but over the picture; point it at the right column instead). Needs a chat feed the Pi can read:
   - Twitch: already have it (anonymous IRC).
   - Kick: try Kick's public Pusher websocket (`chatrooms.<id>.v2`; the chatroom id from the official API in `app/kick.py`, or from the channel lookup). Unofficial and untested: spike it first (a 50-line script on the Pi that prints messages for omie). If Kick blocks it, fall back to option 3.
2. **Render the chat page somewhere else and send the picture**: e.g. a small always-on box (hunnidlab 192.168.1.119?) runs headless Chromium, screenshots the chat column at ~2 fps, and the Pi shows that image with mpv `overlay-add` or as an ASS image. Heavier to build; keeps the Pi light.
3. **Chat stays on the phone** (already works, with the chat tabs). Make it nicer instead: bigger tap target, "chat only" full-screen mode.
Whichever is chosen, wire it into the quick toggles (Appearance) and keep it off by default.

## Cleanup done after the spike
Chromium process killed, `video-margin-ratio-right` back to 0, `~/.config/labwc/rc.xml` restored. Chromium is still installed inside the *running* container (a rebuild removes it); nothing in the repo or Dockerfile changed.

## Update 2026-11-06: Twitch chat as a column (built)
- Single Twitch stream, chat switched on: `video-margin-ratio-right 0.25` shifts the picture and `chat.ass_column` draws the chat in the strip (header, newest messages growing up from the bottom, wrapped to 30 characters). Set in `chat_send` (app.py); `chat_sync` resets the flag for a new player. Multi-view keeps the old short-lived lines over the picture (no room for a column).
- Ben saw it on the TV: the shifted layout looked right, but every overlay redraw flashes on `vo=dmabuf-wayland` (the same reason the volume bar uses `show-text`). The column now redraws every 6 s (`COL_REDRAW_S`). Ben still has to say whether that flash is gone or tolerable.
- `/api/screen.png` (grim) does not capture mpv's OSD on this video output, so overlay text can only be judged on the TV itself, not from screenshots.
- **Kick chat cannot be read without a browser (checked 2026-11-06).** Kick left Pusher (its old key is refused on every cluster); its page now talks to `wss://websockets.kick.com/viewer/v1/connect`, which answers 403 outside a logged-in browser and looks like the viewer-tracking socket, not chat. Kick's site started answering 429 after a few bundle downloads. So Kick chat stays on the phone.
- If the flashing stays: the likely proper fix is to stop drawing the column in the player and give the chat its own surface (the shell mpv behind the stream already draws text reliably; the stream window would have to be narrower than the screen instead of using a margin).

## Update 2026-11-06 (later): chat in windows of its own (built, live, Ben confirmed it looks good)
- Drawing the chat inside the stream's own mpv window flashed and could not be sized; the final design uses **two small mpv windows** (`app/chatwin.py`, class `Panel`, `vo=wlshm`, a plain colour plus a subtitle file like the home screen): a 480x1080 chat window on the right and a 1440x270 "now playing" bar under the picture. The stream is shrunk with `video-margin-ratio-right 0.25` and `video-align-y -1` (picture at the top). Both are placed by labwc window rules (`tvr-chat`, `tvr-chatbar` in `config/labwc-rc.xml`, made by `tools/make_labwc_rules.py`; copy to `~/.config/labwc/rc.xml` on the Pi and `kill -HUP $(pgrep -x labwc)`, backup at `rc.xml.bak-before-chatwin`) with `ToggleAlwaysOnTop` so a restarted stream window can't cover them.
- One background for both sites: two soft glows laid out over the whole screen, each window draws them offset by its own position (`chat.background(ox, oy)`), so the windows join up. The site's colour shows only in the dot and rule (Kick green, Twitch purple).
- Mpv's `background=none` does not make the window see-through on `dmabuf-wayland`, so black bars could not be filled from behind; separate windows were the way.
- Kick chat on the TV works (`chat.KickChat`): polls `https://kick.com/api/v2/channels/<slug>` once for the channel id, then `.../<id>/messages` every 5 s; unofficial, may change. Twitch uses anonymous IRC. Multi-view still uses the old short-lived lines over the picture; chat in multi-view is the next step.
- `/api/screen.png` (grim) does capture these windows (they are real windows), unlike mpv's own OSD.
