"""Letting people in without handing out the password: requests for access (an admin approves one into an account) and
one-time guest links (opening one starts a timed session that can control the TVs and nothing else).
Invite tokens are never saved. Each one is derived (HMAC) from a random seed saved with the invite and a secret key kept in a separate file, and only
the token's hash is the lookup key, so invites.json alone can't be used to get in. An admin can still show an unopened link's QR code again: the app
derives the token again from the seed and the key. The key is a file in /data unless TVREMOTE_INVITE_KEY (32+ characters) is set: then a backup of /data
can't rebuild an unopened link. Links last 48 hours and a few uses. Files live in /data and are readable only by the app."""
import base64
import hashlib
import hmac
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


def _key():
    """The secret the invite tokens are derived with (its own file, so a copy of invites.json is not enough to rebuild a link)."""
    env = os.environ.get("TVREMOTE_INVITE_KEY", "")
    if len(env) >= 32:
        return env.encode()  # kept outside /data (an env var or a secrets mount): a backup of /data then can't rebuild a link
    path = _path("invite.key")
    try:
        with open(path, "rb") as f:
            k = f.read()
        if len(k) >= 32:
            return k
    except OSError:
        pass
    k = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(k)
    return k


def _token_for(seed):
    return base64.urlsafe_b64encode(hmac.new(_key(), b"invite|" + seed.encode(), hashlib.sha256).digest()[:24]).decode().rstrip("=")


MAX_DEVICES = 5


def create_invite(hours, label, max_uses=1, now=None):
    """A new link token. `hours` is how long each guest's access lasts once the link is opened; `max_uses` is how many devices may open it
    (each one counts once, and the link stops working after that many)."""
    now = now or time.time()
    hours = max(1, min(int(hours), 24 * 30))
    max_uses = max(1, min(int(max_uses or 1), MAX_DEVICES))
    seed = secrets.token_hex(8)
    token = _token_for(seed)
    with _lock:
        d = {k: {f: x for f, x in v.items() if f != "token"} for k, v in _read("invites.json", {}).items() if v["redeem_by"] > now}  # also scrubs tokens saved by older versions
        d[_h(token)] = {"created": now, "redeem_by": now + REDEEM_WINDOW, "hours": hours, "label": (label or "").strip()[:30],
                        "max_uses": max_uses, "uses": 0, "seed": seed}  # the token is not saved: its hash is the key, and the seed alone can't rebuild it
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
    return [{"id": k[:8], **{f: x for f, x in v.items() if f not in ("token", "seed")}, "can_qr": bool(v.get("seed"))} for k, v in _read("invites.json", {}).items() if v["redeem_by"] > now]  # never the token or its seed: only invite_token() hands a link out, one at a time


def revoke_invite(iid):
    with _lock:
        d = _read("invites.json", {})
        gone = [k for k in d if k.startswith(iid) and len(iid) >= 8]
        for k in gone:
            del d[k]
        if gone:
            _write("invites.json", d)
        return bool(gone)


def invite_id_for_url(url):
    """The list id of the invite a /join/<token> link belongs to (None when it is not one, or is gone): which guest QR is on the TV."""
    token = (url or "").rsplit("/join/", 1)[-1] if "/join/" in (url or "") else ""
    return k[:8] if token and (k := _h(token)) in _read("invites.json", {}) else None


def invite_token(iid):
    """The token of an unopened invite (by the id the list shows), for showing its QR code again. None when it is gone, expired, or was made before
    links could be rebuilt."""
    if len(iid or "") < 8:
        return None
    for k, v in _read("invites.json", {}).items():
        if k.startswith(iid) and v.get("seed") and v["redeem_by"] > time.time():
            return _token_for(v["seed"])
    return None
