"""Which TVs there are and how each is reached, saved in /data/targets.json by the setup wizard (so nothing about your TVs lives in .env).
Per TV: a friendly name, the Roku's address, the HDMI input to switch to, whether it is in use, and for the Chromecast TV which Chromecast.
shortcut: two fixed slots, "living" (the Pi's own screen) and "bedroom" (a Chromecast); more TVs need the target registry refactor."""
import ipaddress
import json
import os
import re

import store

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


# ---- the TV list (tvs.json): a TV is the saved thing, with what is plugged into it ----
MAX_TVS = 12


def _tvs_path(data_dir):
    return os.path.join(data_dir, "tvs.json")


def _name(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()[:24]


def migrate(old):
    """TVs from the old two-slot config: the Pi slot becomes a TV holding the Pi, the Chromecast slot a TV holding the Chromecast.
    Both on the same Roku make one TV. A switched-off slot adds nothing."""
    def slot(tid):
        c = (old or {}).get(tid) or {}
        return c if c and c.get("enabled", True) is not False else None
    live, bed = slot("living"), slot("bedroom")
    tvs = []
    if live:
        tvs.append({"id": "tv1", "name": _name(live.get("name")) or DEFAULT_NAMES["living"], "roku": live.get("roku") or "",
                    "pi": {"input": live.get("input") or ""}, "chromecast": None})
    if bed:
        cc = {"device": bed.get("device") or "", "input": bed.get("input") or ""}
        if tvs and tvs[0]["roku"] and tvs[0]["roku"] == (bed.get("roku") or ""):
            tvs[0]["chromecast"] = cc
        else:
            tvs.append({"id": f"tv{len(tvs) + 1}", "name": _name(bed.get("name")) or DEFAULT_NAMES["bedroom"],
                        "roku": bed.get("roku") or "", "pi": None, "chromecast": cc})
    return tvs


def load_tvs(data_dir):
    """The saved TVs; when tvs.json doesn't exist yet, the old targets.json converted (nothing is written)."""
    try:
        with open(_tvs_path(data_dir)) as f:
            j = json.load(f)
        if isinstance(j, dict) and isinstance(j.get("tvs"), list):
            return j["tvs"]
    except (OSError, ValueError):
        pass
    return migrate(load(data_dir))


def _input(value):
    value = str(value or "")
    if value not in INPUTS:
        raise ValueError("Pick HDMI 1 to 4, or leave it as not switching.")
    return value


def clean_tvs(raw):
    """Check the list the wizard sent and return the TVs to keep. Raises ValueError with a sentence for the person."""
    items = (raw or {}).get("tvs") if isinstance(raw, dict) else None
    if not isinstance(items, list) or len(items) > MAX_TVS:
        raise ValueError("That list of TVs isn't right.")
    out, names, ids = [], set(), {str(t.get("id")) for t in items if isinstance(t, dict)}
    for c in items:
        if not isinstance(c, dict):
            raise ValueError("That list of TVs isn't right.")
        name = _name(c.get("name"))
        if not name:
            raise ValueError("Give every TV a name.")
        if name.lower() in names:
            raise ValueError("Two TVs have the same name. Give each its own.")
        names.add(name.lower())
        ip = str(c.get("roku") or "").strip()
        if ip and not lan_address(ip):
            raise ValueError("A TV address must be on your home network (like 192.168.1.20).")
        pi, cc = c.get("pi"), c.get("chromecast")
        tid = str(c.get("id") or "")
        if not re.fullmatch(r"tv\d+", tid) or any(o["id"] == tid for o in out):
            n = 1
            while f"tv{n}" in ids or any(o["id"] == f"tv{n}" for o in out):
                n += 1
            tid = f"tv{n}"
        out.append({"id": tid, "name": name, "roku": ip,
                    "pi": {"input": _input((pi or {}).get("input"))} if isinstance(pi, dict) else None,
                    "chromecast": {"device": str(cc.get("device") or "").strip()[:60], "input": _input(cc.get("input")),
                                   **({"uuid": str(cc["uuid"]).strip()[:40]} if cc.get("uuid") else {})} if isinstance(cc, dict) else None})
    if sum(1 for t in out if t["pi"]) > 1:
        raise ValueError("Only one TV can have the Pi plugged in.")
    if sum(1 for t in out if t["chromecast"]) > 1:
        raise ValueError("Only one TV can have a Chromecast.")
    return out


def save_tvs(data_dir, tvs):
    store.write_json(_tvs_path(data_dir), {"version": 1, "tvs": tvs}, indent=2)
    return tvs


def endpoints(tvs):
    """What the app's two playback endpoints need, worked out from the TVs: "living" is the Pi's screen, "bedroom" the Chromecast.
    An endpoint with no TV is off. When one TV has both, each label says which it is."""
    pi = next((t for t in tvs if t.get("pi")), None)
    cc = next((t for t in tvs if t.get("chromecast")), None)
    both = bool(pi and cc and pi["id"] == cc["id"])
    out = {}
    for eid, t, key, suffix in (("living", pi, "pi", " · Pi"), ("bedroom", cc, "chromecast", " · Chromecast")):
        out[eid] = {"enabled": bool(t), "roku": (t or {}).get("roku") or "", "input": ((t or {}).get(key) or {}).get("input") or "",
                    "label": (t["name"] + (suffix if both else "")) if t else DEFAULT_NAMES[eid]}
    out["bedroom"]["device"] = ((cc or {}).get("chromecast") or {}).get("device") or None
    out["bedroom"]["uuid"] = ((cc or {}).get("chromecast") or {}).get("uuid") or None  # the Chromecast's own id: survives a rename in Google Home
    return out
