"""Named accounts beside the owner (whose password is TVREMOTE_PASSWORD). Each account has its own favourites and its own
Kick link; everything else (what is playing, recents, settings) is shared, since there is one TV.
Passwords are stored as salted scrypt hashes in /data/users.json."""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading

OWNER = "owner"
_lock = threading.Lock()


def _file():
    return os.path.join(os.environ.get("TVREMOTE_DATA", "/data"), "users.json")


def _load():
    try:
        with open(_file()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1).hex()


def names():
    return sorted(_load())


def exists(name):
    return name in _load()


def valid_name(name):
    return bool(re.fullmatch(r"[a-z0-9_-]{2,20}", name or "")) and name != OWNER


def add(name, password):
    """Create an account (or change its password). Raises ValueError for a bad name or a short password."""
    name = (name or "").strip().lower()
    if not valid_name(name):
        raise ValueError("Names are 2 to 20 letters, digits, - or _ (and not 'owner').")
    if len(password or "") < 6:
        raise ValueError("Use a password of at least 6 characters.")
    salt = secrets.token_hex(16)
    with _lock:
        d = _load()
        d[name] = {"salt": salt, "hash": _hash(password, salt)}
        tmp = _file() + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(d, f)
        os.replace(tmp, _file())
    return name


def remove(name):
    with _lock:
        d = _load()
        if d.pop(name, None) is None:
            return False
        with open(_file() + ".tmp", "w") as f:
            json.dump(d, f)
        os.replace(_file() + ".tmp", _file())
    return True


def verify(name, password):
    u = _load().get((name or "").strip().lower())
    if not u:
        _hash(password or "", "00" * 16)  # similar work either way, so timing doesn't reveal which names exist
        return False
    return hmac.compare_digest(_hash(password or "", u["salt"]), u["hash"])
