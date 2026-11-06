"""Letting people in without handing out the password: requests for access (an admin approves one into an account) and
one-time guest links (opening one starts a timed session that can control the TVs and nothing else).
Invite tokens are stored hashed, so the file can't be used to get in. Files live in /data and are readable only by the app."""
import hashlib
import json
import os
import re
import secrets
import threading
import time

import store

REDEEM_WINDOW = 48 * 3600  # an unopened link stops working after this
MAX_PENDING = 20
COOLDOWN = 30  # seconds between requests from one address
_lock = threading.Lock()
_last_by_ip = {}


def _path(name):
    return os.path.join(os.environ.get("TVREMOTE_DATA", "/data"), name)


def _read(name, default):
    try:
        with open(_path(name)) as f:
            d = json.load(f)
        return d if isinstance(d, type(default)) else default
    except (OSError, ValueError):
        return default


def _write(name, data):
    store.write_json(_path(name), data, 0o600)


# ---- requests for access ----

def add_request(name, note, ip, now=None):
    """Queue a request. Raises ValueError with a sentence for the person asking."""
    now = now or time.time()
    name = re.sub(r"\s+", " ", (name or "").strip())[:30]
    note = re.sub(r"\s+", " ", (note or "").strip())[:140]
    if not name:
        raise ValueError("Tell us who you are.")
    with _lock:
        if now - _last_by_ip.get(ip, 0) < COOLDOWN:
            raise ValueError("Please wait a moment before asking again.")
        pending = _read("requests.json", [])
        if len(pending) >= MAX_PENDING:
            raise ValueError("There are too many waiting requests right now. Ask the owner directly.")
        _last_by_ip[ip] = now
        for k in [k for k, t in _last_by_ip.items() if now - t > 3600]:
            del _last_by_ip[k]
        rid = secrets.token_hex(4)
        pending.append({"id": rid, "name": name, "note": note, "ts": now})
        _write("requests.json", pending)
    return rid


def requests_pending():
    return _read("requests.json", [])


def pop_request(rid):
    """Remove and return a request (approved or denied), or None."""
    with _lock:
        pending = _read("requests.json", [])
        hit = next((r for r in pending if r["id"] == rid), None)
        if hit:
            _write("requests.json", [r for r in pending if r["id"] != rid])
        return hit


# ---- one-time guest links ----

def _h(token):
    return hashlib.sha256(token.encode()).hexdigest()


MAX_DEVICES = 5


def create_invite(hours, label, max_uses=1, now=None):
    """A new link token. `hours` is how long each guest's access lasts once the link is opened; `max_uses` is how many devices may open it
    (each one counts once, and the link stops working after that many)."""
    now = now or time.time()
    hours = max(1, min(int(hours), 24 * 30))
    max_uses = max(1, min(int(max_uses or 1), MAX_DEVICES))
    token = secrets.token_urlsafe(24)
    with _lock:
        d = {k: v for k, v in _read("invites.json", {}).items() if v["redeem_by"] > now}
        d[_h(token)] = {"created": now, "redeem_by": now + REDEEM_WINDOW, "hours": hours, "label": (label or "").strip()[:30],
                        "max_uses": max_uses, "uses": 0, "token": token}  # kept so an admin can show its QR code again (an unopened link only; gone once used up or expired)
        _write("invites.json", d)
    return token


def peek_invite(token, now=None):
    v = _read("invites.json", {}).get(_h(token or ""))
    return v if v and v["redeem_by"] > (now or time.time()) else None


def redeem_invite(token, now=None):
    """One device opens the link: returns the link's details, and counts it. After its last permitted use the link is gone."""
    with _lock:
        d = _read("invites.json", {})
        key = _h(token or "")
        v = d.get(key)
        if v is None or v["redeem_by"] <= (now or time.time()):
            return None
        v["uses"] = v.get("uses", 0) + 1
        if v["uses"] >= v.get("max_uses", 1):
            del d[key]
        _write("invites.json", d)
        opened = now or time.time()
        entry = secrets.token_hex(4)
        used = (_read("invites_used.json", []) + [{"id": entry, "label": v["label"], "hours": v["hours"], "opened": opened}])[-10:]
        _write("invites_used.json", used)  # kept short: just enough to see that a link was used, and when
        return {**v, "entry": entry}  # the guest's session carries this, so the list can tell which opening is still in use


def invites_used():
    """The last few times a link was opened, newest first: {id, label, hours, opened}. (Entries saved before ids existed get one from their time.)"""
    return [{"id": f"t{int(e['opened'])}", **e} for e in reversed(_read("invites_used.json", []))]


def clear_used(uid=None):
    """Remove one entry of that history (or all of it when no id is given). Returns how many were removed."""
    with _lock:
        keep = [e for e in _read("invites_used.json", []) if uid is not None and e.get("id", f"t{int(e['opened'])}") != uid]
        removed = len(_read("invites_used.json", [])) - len(keep)
        if removed:
            _write("invites_used.json", keep)
        return removed


def invites_pending(now=None):
    now = now or time.time()
    return [{"id": k[:8], **v} for k, v in _read("invites.json", {}).items() if v["redeem_by"] > now]


def revoke_invite(iid):
    with _lock:
        d = _read("invites.json", {})
        gone = [k for k in d if k.startswith(iid) and len(iid) >= 8]
        for k in gone:
            del d[k]
        if gone:
            _write("invites.json", d)
        return bool(gone)


def invite_token(iid):
    """The token of an unopened invite (by the id the list shows), for showing its QR code again. None when it is gone."""
    if len(iid or "") < 8:
        return None
    for k, v in _read("invites.json", {}).items():
        if k.startswith(iid) and v.get("token") and v["redeem_by"] > time.time():
            return v["token"]
    return None
