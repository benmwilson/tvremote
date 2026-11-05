"""Login sessions: a random token in the cookie, and only its keyed hash saved in /data/sessions.json (so the file can't be used to log in).
A session says who it belongs to ("owner", an account name, or "guest") and when it ends."""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

import accounts
import activity
import store

SESSION_DAYS = 180
lock = threading.Lock()
revoked = set()  # logged-out tokens, kept in memory too so a failed disk write still ends the session
key_cache = {"v": None}


def _dir():
    return os.environ.get("TVREMOTE_DATA", "/data")


def file():
    return os.path.join(_dir(), "sessions.json")


def load():
    try:
        with open(file()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(d):
    try:
        store.write_json(file(), d, 0o600)  # only hashes, but nobody else needs to read them
    except OSError:
        pass  # sessions then last until the next restart


def _key():
    if key_cache["v"] is None:  # a random key kept in /data
        path = os.path.join(_dir(), "session.key")
        try:
            with open(path, "rb") as f:
                key_cache["v"] = f.read()
        except OSError:
            key_cache["v"] = secrets.token_bytes(32)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(key_cache["v"])
            except OSError:
                pass  # sessions then last until the next restart
    return key_cache["v"]


def token_hash(tok):
    # keyed: only hashes are stored (the file can't be used as a cookie)
    return hmac.new(_key(), tok.encode(), hashlib.sha256).hexdigest()


def new_session(user=accounts.OWNER, seconds=None, extra=None):
    """A random token for one login; the server remembers it (hashed) until it expires or the user logs out."""
    tok = secrets.token_urlsafe(32)
    with lock:
        d = {k: v for k, v in load().items() if _exp(v) > time.time()}
        exp = time.time() + (seconds or SESSION_DAYS * 86400)
        d[token_hash(tok)] = {"exp": exp, "user": user, **(extra or {})}
        save(d)
    return tok


def _exp(v):
    return v["exp"] if isinstance(v, dict) else v


def session_user(tok):
    """Who a login cookie belongs to ("owner" or an account name), or None when it is not a live session."""
    if not tok or token_hash(tok) in revoked:
        return None
    v = load().get(token_hash(tok))
    if v is None or _exp(v) <= time.time():
        return None
    if not isinstance(v, dict):
        return None  # an old-style session (from when the .env password existed) is not a login any more
    return v["user"] if v["user"] == accounts.GUEST or accounts.exists(v["user"]) else None


def guest_sessions():
    return [{"id": k[:8], "label": v.get("label", ""), "exp": v["exp"], "since": v.get("since"), "last": activity.last("guest:" + k[:8])}
            for k, v in load().items()
            if isinstance(v, dict) and v.get("user") == accounts.GUEST and v["exp"] > time.time() and k not in revoked]


def drop_guest(gid):
    activity.forget("guest:" + gid)
    with lock:
        d = load()
        gone = [k for k, v in d.items() if len(gid) >= 8 and k.startswith(gid) and isinstance(v, dict) and v.get("user") == accounts.GUEST]
        for k in gone:
            del d[k]
            revoked.add(k)
        if gone:
            save(d)
        return bool(gone)


def drop_user_sessions(name):
    """End every login of an account (it was removed or its password changed)."""
    with lock:
        d = load()
        gone = [k for k, v in d.items() if isinstance(v, dict) and v.get("user") == name]
        for k in gone:
            del d[k]
            revoked.add(k)
        if gone:
            save(d)


def drop_session(tok):
    with lock:
        d = load()
        if d.pop(token_hash(tok), None) is not None:  # only real sessions are remembered: /logout needs no login, so junk must not pile up
            revoked.add(token_hash(tok))
            save(d)
