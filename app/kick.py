"""Kick's official API (api.kick.com) with an app token: is a channel live, its title, category and uptime.
The Pi can't read kick.com itself (it refuses non-browser clients), but the sanctioned API accepts a bearer token.
Needs TVREMOTE_KICK_CLIENT_ID and TVREMOTE_KICK_CLIENT_SECRET; without them enabled() is False and callers fall back.
shortcut: app token only (public data), no user login; add the authorization-code flow when accounts can link Kick."""
import base64
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ID = os.environ.get("TVREMOTE_KICK_CLIENT_ID", "")
SECRET = os.environ.get("TVREMOTE_KICK_CLIENT_SECRET", "")
_token = {"value": None, "exp": 0.0}


def enabled():
    return bool(ID and SECRET)


def _open(req, timeout=10):  # one place to stub in tests
    req.add_header("User-Agent", "tvremote/1.0")  # urllib's default is often blocked by bot filters
    return urlopen(req, timeout=timeout).read()


def describe(e):
    """One line for the log: the error, and the start of Kick's answer when it sent one."""
    body = ""
    if hasattr(e, "read"):
        try:
            body = " " + e.read(300).decode(errors="replace")
        except Exception:
            pass
    return f"{type(e).__name__}: {e}{body}"


def _app_token():
    if _token["value"] and time.time() < _token["exp"] - 60:
        return _token["value"]
    body = urlencode({"grant_type": "client_credentials", "client_id": ID, "client_secret": SECRET}).encode()
    j = json.loads(_open(Request("https://id.kick.com/oauth/token", body)))
    _token["value"], _token["exp"] = j["access_token"], time.time() + float(j.get("expires_in") or 3600)
    return _token["value"]


REDIRECT = os.environ.get("TVREMOTE_KICK_REDIRECT", "")  # the https URL registered on the Kick app
LINK = os.path.join(os.environ.get("TVREMOTE_DATA", "/data"), "kick_link.json")
_pending = {}  # state -> (PKCE verifier, expiry): one login in flight is enough, kept in memory
# shortcut: the refresh token is stored but never used, so the link works until the access token expires; add a refresh when a feature needs the user token.


def can_link():
    return enabled() and bool(REDIRECT)


def _form(url, **fields):
    return json.loads(_open(Request(url, urlencode(fields).encode())))


def link_url():
    """The Kick page to send the browser to. PKCE (S256) plus a random state tie the answer to this request."""
    verifier, state, now = secrets.token_urlsafe(48), secrets.token_urlsafe(16), time.time()
    for s in [s for s, (_, exp) in _pending.items() if exp < now]:
        del _pending[s]
    _pending[state] = (verifier, now + 600)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return "https://id.kick.com/oauth/authorize?" + urlencode({
        "response_type": "code", "client_id": ID, "redirect_uri": REDIRECT, "scope": "user:read channel:read",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state})


def link_finish(code, state):
    """Trade the code for tokens and remember who linked. Raises ValueError for an unknown or stale state."""
    verifier, exp = _pending.pop(state, (None, 0))
    if not verifier or exp < time.time() or not code:
        raise ValueError("unknown or expired login")
    tok = _form("https://id.kick.com/oauth/token", grant_type="authorization_code", client_id=ID, client_secret=SECRET,
                redirect_uri=REDIRECT, code_verifier=verifier, code=code)
    me = json.loads(_open(Request("https://api.kick.com/public/v1/users",
                                  headers={"Authorization": "Bearer " + tok["access_token"]})))["data"][0]
    fd = os.open(LINK, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"access": tok["access_token"], "refresh": tok.get("refresh_token"),
                   "exp": time.time() + float(tok.get("expires_in") or 0),
                   "user_id": me.get("user_id"), "name": me.get("name")}, f)


def linked():
    """The linked Kick user's name, or None."""
    try:
        with open(LINK) as f:
            return json.load(f).get("name")
    except (OSError, ValueError):
        return None


def unlink():
    try:
        os.remove(LINK)
    except OSError:
        pass


def check(slug):
    """{"live", "game", "title", "uptime_s", "viewers"} for a channel, {"live": False} when offline,
    None when it could not be checked (no creds, network, unknown channel)."""
    if not enabled():
        return None
    try:
        req = Request("https://api.kick.com/public/v1/channels?" + urlencode({"slug": slug}),
                      headers={"Authorization": "Bearer " + _app_token()})
        data = json.loads(_open(req)).get("data") or []
    except Exception as e:
        print("Kick API:", describe(e), flush=True)
        _token["value"] = None  # an expired or revoked token gets a fresh one next time
        return None
    if not data:
        return None
    ch, st = data[0], data[0].get("stream") or {}
    if not st.get("is_live"):
        return {"live": False}
    up = None
    try:
        up = time.time() - datetime.fromisoformat(st["start_time"].replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except (KeyError, ValueError, AttributeError):
        pass
    return {"live": True, "game": (ch.get("category") or {}).get("name"), "title": ch.get("stream_title"),
            "uptime_s": up, "viewers": st.get("viewer_count")}
