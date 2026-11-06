"""Remote-control keys for the launcher, from whatever input devices the Pi has: an IR receiver (rc-core), a USB IR dongle that acts as a keyboard
(FLIRC), the HDMI-CEC adapters (if the TV forwards its remote), a plain keyboard. Reads the kernel's /dev/input/event* devices directly (no extra
packages): each key press becomes one of the launcher's names (see launcher.KEYMAP) and is handed to a callback.
The parsing and the code table are plain functions so they can be tested without a device."""
import glob
import os
import struct
import threading
import time

EVENT = struct.Struct("llHHi")  # struct input_event on a 64-bit kernel: seconds, microseconds, type, code, value
EV_KEY = 1
KEY_NAMES = {  # Linux key codes (linux/input-event-codes.h) -> the names launcher.KEYMAP knows
    103: "KEY_UP", 108: "KEY_DOWN", 105: "KEY_LEFT", 106: "KEY_RIGHT",
    28: "KEY_ENTER", 96: "KEY_KPENTER", 352: "KEY_OK", 353: "KEY_SELECT", 57: "KEY_SPACE",
    158: "KEY_BACK", 174: "KEY_EXIT", 1: "KEY_ESC", 14: "KEY_BACKSPACE",
    102: "KEY_HOME", 139: "KEY_MENU", 172: "KEY_HOMEPAGE",
    164: "KEY_PLAYPAUSE", 207: "KEY_PLAY", 119: "KEY_PAUSE", 166: "KEY_INFO",
}


def parse_events(data):
    """[(code, value)] for the key events in a chunk read from an input device (value 1 = pressed, 0 = released, 2 = held and repeating)."""
    out = []
    for i in range(0, len(data) - EVENT.size + 1, EVENT.size):
        _s, _u, typ, code, value = EVENT.unpack_from(data, i)
        if typ == EV_KEY:
            out.append((code, value))
    return out


def device_name(event_path):
    """The kernel's name for /dev/input/eventN (read from /sys), or ''."""
    try:
        with open(f"/sys/class/input/{os.path.basename(event_path)}/device/name") as f:
            return f.read().strip()
    except OSError:
        return ""


def watch(on_key, devices=None, stop=None, repeat=True):
    """Start one thread per input device; each press calls on_key("KEY_UP"...) for keys in KEY_NAMES. `repeat`: a held key repeats (arrows
    scroll; the launcher wants that). Returns the threads. A device that disappears just ends its thread."""
    stop = stop or threading.Event()
    paths = devices if devices is not None else sorted(glob.glob("/dev/input/event*"))

    def run(path):
        try:
            with open(path, "rb", buffering=0) as f:
                while not stop.is_set():
                    data = f.read(EVENT.size * 8)
                    if not data:
                        time.sleep(0.2)
                        continue
                    for code, value in parse_events(data):
                        if value in (1, 2 if repeat else 1) and code in KEY_NAMES:
                            on_key(KEY_NAMES[code])
        except OSError:
            return
    threads = [threading.Thread(target=run, args=(p,), daemon=True) for p in paths]
    for t in threads:
        t.start()
    return threads
