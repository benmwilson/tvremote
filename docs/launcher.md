# TV launcher (home hub) with remote-control input

Goal: the Pi's idle screen becomes a launcher you can move around and start things from: who is live, favourites, groups, recents. Ideally with the
TV's own remote (the Roku remote), not only the phone.

## Built so far
- `app/launcher.py`: the model. Rows of tiles built from the favourites, recents and groups (`build_rows`), and a `Launcher` that moves a focus around
  them with keys (`key()`), remembering the column per row, and answers with what to do (`redraw`, `play`, `group`, `back`, `home`).
  Every key source maps into one set of names (`KEYMAP`: Linux input names from CEC / IR / a keyboard, and the Roku names the phone's pad sends).
- `tests/test_launcher.py`: the rows and the moves.

Nothing draws it or feeds it keys yet.

## Where keys can come from (what the Pi has, checked 2026-11-06 on the bedroom TV)
| Source | State |
|---|---|
| Phone pad | Works today for the Roku itself; needs a second target ("drive the launcher") when the launcher is the screen. |
| HDMI-CEC remote passthrough | The Pi's kernel exposes the CEC adapters as input devices: `vc4-hdmi-0` = `/dev/input/event0`, `vc4-hdmi-1` = `event2` (ben is in the `input` group). If the TV forwards its remote's keys over CEC they arrive there as `KEY_*` events. Earlier notes say this Roku TV forwards none: **to be confirmed by pressing the TV remote while reading `event0`**. |
| IR receiver | No `/dev/lirc*` and nothing on USB: it needs a small hardware receiver (e.g. FLIRC USB, or a GPIO IR module with `gpio-ir`). The Roku TV's own remote is IR, so a receiver would see its keys; the Wi-Fi Direct (voice button) remote cannot be heard that way. |
| Roku ECP | Commands only (the Pi sends keys to the Roku); no stream of the remote's own key presses is known. |
| USB / Bluetooth keyboard or air mouse | Works through the same input events. |

## Next steps
1. **Test CEC passthrough**: with the Pi's input selected on the TV, press the TV remote's arrows/OK/Back while reading `/dev/input/event0`. If keys appear, build `launcherinput.py` (evdev reader, `KEYMAP`) and pass `/dev/input` through in `compose.yaml`.
2. If not: choose the IR receiver (or accept the phone as the remote and add the pad).
3. **Draw the launcher** in `shell.py` as one more screen of the home window (ASS: a row of tiles with the focus ring), redrawn on `redraw`.
4. **Wire the actions**: `play` -> the normal play path, `group` -> `playGroup`; `back`/`home` from a playing stream stops it and returns to the launcher.
5. **Settings**: launcher on/off, which rows, tile size; admin only.
