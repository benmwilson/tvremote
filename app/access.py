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
    tmp = _path(name) + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, _path(name))


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


def create_invite(hours, label, now=None):
    """A new link token. `hours` is how long the guest's access lasts once the link is opened."""
    now = now or time.time()
    hours = max(1, min(int(hours), 24 * 30))
    token = secrets.token_urlsafe(24)
    with _lock:
        d = {k: v for k, v in _read("invites.json", {}).items() if v["redeem_by"] > now}
        d[_h(token)] = {"created": now, "redeem_by": now + REDEEM_WINDOW, "hours": hours, "label": (label or "").strip()[:30]}
        _write("invites.json", d)
    return token


def peek_invite(token, now=None):
    v = _read("invites.json", {}).get(_h(token or ""))
    return v if v and v["redeem_by"] > (now or time.time()) else None


def redeem_invite(token, now=None):
    """Use up a link: returns its details once, then never again."""
    with _lock:
        d = _read("invites.json", {})
        v = d.pop(_h(token or ""), None)
        if v is None or v["redeem_by"] <= (now or time.time()):
            return None
        _write("invites.json", d)
        used = (_read("invites_used.json", []) + [{"label": v["label"], "hours": v["hours"], "opened": now or time.time()}])[-10:]
        _write("invites_used.json", used)  # kept short: just enough to see that a link was used, and when
        return v


def invites_used():
    """The last few links that were opened, newest first: {label, hours, opened}."""
    return list(reversed(_read("invites_used.json", [])))


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
