"""Named accounts beside the owner (whose password is TVREMOTE_PASSWORD). Each account has its own favourites and its own
Kick link; everything else (what is playing, recents, settings) is shared, since there is one TV.
Passwords are stored as salted scrypt hashes in /data/users.json.
Two roles: "admin" (manages accounts and settings) and "user". The owner (the .env password) is always an admin, so
nobody can be locked out by demoting accounts."""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading

OWNER = "owner"
ROLES = ("user", "admin")
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


def role(name):
    if name == OWNER:
        return "admin"
    return _load().get(name, {}).get("role", "user") if name in _load() else "user"


def listing():
    return [{"name": n, "role": role(n)} for n in names()]


def set_role(name, new_role):
    if new_role not in ROLES:
        raise ValueError("Role must be user or admin.")
    with _lock:
        d = _load()
        if name not in d:
            raise ValueError("No such account.")
        d[name]["role"] = new_role
        _write(d)


def valid_name(name):
    return bool(re.fullmatch(r"[a-z0-9_-]{2,20}", name or "")) and name != OWNER


def _write(d):
    tmp = _file() + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # hashes are for this app only
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)
    os.replace(tmp, _file())


def add(name, password, new_role=None):
    """Create an account (or change its password, keeping its role unless one is given). Raises ValueError for a bad name,
    a short password or an unknown role."""
    name = (name or "").strip().lower()
    if not valid_name(name):
        raise ValueError("Names are 2 to 20 letters, digits, - or _ (and not 'owner').")
    if len(password or "") < 6:
        raise ValueError("Use a password of at least 6 characters.")
    if new_role not in (None, *ROLES):
        raise ValueError("Role must be user or admin.")
    salt = secrets.token_hex(16)
    with _lock:
        d = _load()
        d[name] = {"salt": salt, "hash": _hash(password, salt), "role": new_role or d.get(name, {}).get("role", "user")}
        _write(d)
    return name


def remove(name):
    with _lock:
        d = _load()
        if d.pop(name, None) is None:
            return False
        _write(d)
    return True


def verify(name, password):
    u = _load().get((name or "").strip().lower())
    if not u:
        _hash(password or "", "00" * 16)  # similar work either way, so timing doesn't reveal which names exist
        return False
    return hmac.compare_digest(_hash(password or "", u["salt"]), u["hash"])
