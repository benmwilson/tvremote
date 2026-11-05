"""Counting failed logins so guessing can't go on forever. Everything is in memory (a restart clears it, which is fine for a lockout).
Failed tries (the form, a script, the setup code), newest last. Keys: "<address>" (everything from one address, IPv6 by /64),
"<address>|<name>" (one account from one address) and "setup" (every guess at the setup code, from anywhere)."""
import ipaddress
import time

fails = {}
WINDOW = 600  # seconds a failure counts for


def bucket(ip):
    try:
        a = ipaddress.ip_address(ip or "")
        return str(ipaddress.ip_network(f"{a}/64", strict=False)) if a.version == 6 else str(a)
    except ValueError:
        return ip or "?"


def recent(key, now=None):
    now = now or time.time()
    r = [t for t in fails.get(key, []) if now - t < WINDOW]
    if r:
        fails[key] = r
    else:
        fails.pop(key, None)  # no entry for anyone with no recent failures
    return r


def _prune(now):
    if len(fails) > 5000:  # only ever drops what has expired, then the oldest: a flood of addresses can't reset anyone's count
        for k in list(fails):
            recent(k, now)
        for k in sorted(fails, key=lambda k: fails[k][-1])[:max(0, len(fails) - 4000)]:
            del fails[k]


def blocked(ip, who=""):
    now = time.time()
    _prune(now)
    ip = bucket(ip)
    return len(recent(ip, now)) >= 30 or bool(who) and len(recent(f"{ip}|{who}", now)) >= 10


def fail(ip, who=""):
    ip, now = bucket(ip), time.time()
    for k in (ip, f"{ip}|{who}" if who else None):
        if k:
            fails.setdefault(k, []).append(now)


def ok(ip, who):
    fails.pop(f"{bucket(ip)}|{who}", None)  # only this account's count: a login that works can't wipe the failures against others


def setup_blocked(ip):
    """Setup-code guessing: per address, and 10 wrong guesses from anywhere stop everyone for a while."""
    return blocked(ip, "setup") or len(recent("setup")) >= 10


def setup_fail(ip):
    fail(ip, "setup")
    fails.setdefault("setup", []).append(time.time())
