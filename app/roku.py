"""Roku TV control over ECP (port 8060). Needs 'Control by mobile apps' set to Permissive for keypresses."""
import re
import urllib.error
import urllib.request

# Only these keys can be sent (the endpoint is reachable from the LAN).
KEYS = {"Home", "Back", "Up", "Down", "Left", "Right", "Select", "Info", "Play", "Rev", "Fwd",
        "PowerOn", "PowerOff", "VolumeUp", "VolumeDown", "VolumeMute",
        "InputHDMI1", "InputHDMI2", "InputHDMI3", "InputHDMI4", "InputTuner", "InputAV1"}


class RokuError(Exception):
    pass


def _call(ip, path, method="GET", timeout=4):
    req = urllib.request.Request(f"http://{ip}:8060{path}", method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 403:
            raise RokuError("TV refused: set Settings > System > Advanced system settings > "
                            "Control by mobile apps > Network access to Permissive")
        raise RokuError(f"TV returned HTTP {e.code}")
    except OSError as e:
        raise RokuError(f"TV unreachable ({e})")


def _tag(xml, name):
    m = re.search(rf"<{name}>(.*?)</{name}>", xml, re.S)
    return m.group(1).strip() if m else None


def info(ip):
    xml = _call(ip, "/query/device-info")
    app = re.search(r"<app\b([^>]*)>([^<]+)<", _call(ip, "/query/active-app"))
    app_id = re.search(r'id="([^"]*)"', app.group(1)) if app else None  # e.g. tvinput.hdmi1 for an HDMI input
    size = _tag(xml, "screen-size")
    return {"name": _tag(xml, "user-device-name"), "brand": _tag(xml, "vendor-name"),
            "model": _tag(xml, "model-name"), "size": int(size) if size and size.isdigit() else None,
            "power": _tag(xml, "power-mode"), "app": app.group(2) if app else None,
            "app_id": app_id.group(1) if app_id else None}


def key(ip, name):
    if name not in KEYS:
        raise RokuError("Unknown key")
    _call(ip, f"/keypress/{name}", "POST")
