"""Which TVs there are and how each is reached, saved in /data/targets.json by the setup wizard (so nothing about your TVs lives in .env).
Per TV: a friendly name, the Roku's address, the HDMI input to switch to, whether it is in use, and for the Chromecast TV which Chromecast.
shortcut: two fixed slots, "living" (the Pi's own screen) and "bedroom" (a Chromecast); more TVs need the target registry refactor."""
import ipaddress
import json
import os
import re

IDS = ("living", "bedroom")
INPUTS = ("", "HDMI1", "HDMI2", "HDMI3", "HDMI4")
DEFAULT_NAMES = {"living": "Living room", "bedroom": "Bedroom"}


def _path(data_dir):
    return os.path.join(data_dir, "targets.json")


def load(data_dir):
    try:
        with open(_path(data_dir)) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def lan_address(text):
    """The address if it is a private IPv4 address, else None: a TV is never somewhere out on the internet, and this keeps the
    admin-only "talk to this address" calls on the home network."""
    try:
        a = ipaddress.ip_address(str(text or "").strip())
    except ValueError:
        return None
    return str(a) if a.version == 4 and a.is_private and not a.is_loopback and not a.is_link_local else None


def clean(raw):
    """Check what the wizard sent and return the config to keep. Raises ValueError with a sentence for the person."""
    out = {}
    for tid in IDS:
        c = (raw or {}).get(tid) or {}
        ip = str(c.get("roku") or "").strip()
        if ip and not lan_address(ip):
            raise ValueError("A TV address must be on your home network (like 192.168.1.20).")
        inp = str(c.get("input") or "")
        if inp not in INPUTS:
            raise ValueError("Pick HDMI 1 to 4, or leave it as not switching.")
        item = {"name": re.sub(r"\s+", " ", str(c.get("name") or "")).strip()[:24] or DEFAULT_NAMES[tid],
                "roku": ip, "input": inp, "enabled": bool(c.get("enabled", True))}
        if tid == "bedroom":
            item["device"] = str(c.get("device") or "").strip()[:60] or None
        out[tid] = item
    return out


def save(data_dir, cfg):
    """Write the config, keeping any other keys already in the file (like a Chromecast's `via`)."""
    current = load(data_dir)
    for tid, item in cfg.items():
        current[tid] = {**(current.get(tid) or {}), **item}
    tmp = _path(data_dir) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(current, f, indent=2)
    os.replace(tmp, _path(data_dir))
    return current
