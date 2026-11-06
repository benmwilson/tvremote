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
