"""Named accounts. Each has its own favourites and its own Kick link; everything else (what is playing, recents, settings) is shared,
since there is one TV. Passwords are stored as salted scrypt hashes in /data/users.json.
Two roles: "admin" (manages accounts and settings) and "user". The first admin made by setup is the founder (the owner): only they can
change other admins. "owner" itself is just the stand-in used on a fresh install, before anyone can log in."""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

OWNER = "owner"
GUEST = "guest"  # the pseudo-account behind a one-time link: controls the TVs, nothing else
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


def initialised():
    """True once accounts have ever been set up: the file exists, even if it can't be read now. Login stays required then, so a
    damaged or unreadable file can't turn the install back into an open one."""
    return os.path.exists(_file())


def names():
    return sorted(_load())


def exists(name):
    return name in _load()


def role(name):
    if name == OWNER:
        return "admin"
    if name == GUEST:
        return "guest"
    return _load().get(name, {}).get("role", "user") if name in _load() else "user"


def is_owner(name):
    """The owner: the founder (the first admin, made by setup), or the stand-in used while nobody can log in yet (a fresh install)."""
    return name == OWNER or bool(_load().get(name, {}).get("founder"))


def founder_name():
    for n, u in _load().items():
        if u.get("founder"):
            return n
    return None


def claim_founder(name):
    """Make `name` (an admin) the owner, if nobody is. Only called after the operator has proved they can read the Pi's log
    (the setup code), never automatically. True when it happened."""
    with _lock:
        d = _load()
        if any(u.get("founder") for u in d.values()) or d.get(name, {}).get("role") != "admin":
            return False
        d[name]["founder"] = True
        _write(d)
        return True


def listing():
    d = _load()
    return [{"name": n, "role": role(n), "created": d[n].get("created"), "last_login": d[n].get("last_login"),
             "founder": bool(d[n].get("founder"))} for n in sorted(d)]


def touch(name):
    """Remember when an account last logged in."""
    with _lock:
        d = _load()
        if name in d:
            d[name]["last_login"] = time.time()
            _write(d)


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
    return bool(re.fullmatch(r"[a-z0-9_-]{2,20}", name or "")) and name not in (OWNER, GUEST)


def _write(d):
    tmp = _file() + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # hashes are for this app only
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)
    os.replace(tmp, _file())


def add(name, password, new_role=None, founder=False):
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
        d[name] = {"salt": salt, "hash": _hash(password, salt), "role": new_role or d.get(name, {}).get("role", "user"),
                   "created": d.get(name, {}).get("created") or time.time(), "last_login": d.get(name, {}).get("last_login"),
                   "founder": bool(founder) or bool(d.get(name, {}).get("founder"))}
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


def change_password(name, current, new):
    """An account changes its own password. Raises ValueError when the current one is wrong or the new one is too short."""
    if not verify(name, current):
        raise ValueError("The current password is wrong.")
    add(name, new)
