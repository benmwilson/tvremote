"""Roku TV control over ECP (port 8060). Needs 'Control by mobile apps' set to Permissive for keypresses."""
import html
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
            "power": _tag(xml, "power-mode"), "app": html.unescape(app.group(2)).strip() if app else None,  # the TV sends "&amp;" for "&"
            "app_id": app_id.group(1) if app_id else None}


def media(ip):
    """What the TV's own player says it is doing (read-only): {state, duration}. state is play, pause, buffer, startup, close...
    Apps report little: there is no title, so the app's name is all the page can show."""
    xml = _call(ip, "/query/media-player")
    state = re.search(r'<player\b[^>]*\bstate="([^"]*)"', xml)
    dur = re.match(r"(\d+)\s*ms", _tag(xml, "duration") or "")
    return {"state": state.group(1) if state else None, "duration": int(dur.group(1)) / 1000 if dur else None}


def apps(ip):
    """The apps installed on the TV: [{"id", "name"}], in the TV's own order. Inputs and menus are left out."""
    xml = _call(ip, "/query/apps")
    return [{"id": m.group(1), "name": html.unescape(m.group(3)).strip()}
            for m in re.finditer(r'<app\b[^>]*?\bid="(\d+)"[^>]*?\btype="(\w+)"[^>]*>([^<]*)</app>', xml) if m.group(2) == "appl"]


def launch(ip, app_id):
    if not re.fullmatch(r"\d{1,12}", str(app_id)):
        raise RokuError("Unknown app")
    _call(ip, f"/launch/{app_id}", "POST")


def icon(ip, app_id):
    """(bytes, content type) of an app's icon, or raises RokuError. Small: a TV icon is tens of kilobytes."""
    if not re.fullmatch(r"\d{1,12}", str(app_id)):
        raise RokuError("Unknown app")
    try:
        with urllib.request.urlopen(f"http://{ip}:8060/query/icon/{app_id}", timeout=4) as r:
            return r.read(400_000), r.headers.get_content_type()
    except (urllib.error.URLError, OSError) as e:
        raise RokuError(f"No icon ({e})")


def key(ip, name):
    if name not in KEYS:
        raise RokuError("Unknown key")
    _call(ip, f"/keypress/{name}", "POST")
