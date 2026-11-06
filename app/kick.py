"""Kick's official API (api.kick.com) with an app token: is a channel live, its title, category and uptime.
The Pi can't read kick.com itself (it refuses non-browser clients), but the sanctioned API accepts a bearer token.
Needs TVREMOTE_KICK_CLIENT_ID and TVREMOTE_KICK_CLIENT_SECRET; without them enabled() is False and callers fall back.
Accounts can also link their own Kick login (authorization code + PKCE) to send chat messages as themselves."""
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
DATA = os.environ.get("TVREMOTE_DATA", "/data")
SCOPES = "user:read channel:read chat:write"
_pending = {}  # state -> (PKCE verifier, expiry, account): one login in flight is enough, kept in memory
_ids = {}  # channel slug -> broadcaster user id


def can_link():
    return enabled() and bool(REDIRECT)


def _path(user):
    return os.path.join(DATA, "kick_link.json" if user == "owner" else f"kick_link.{user}.json")  # the owner's file predates accounts


def _form(url, **fields):
    return json.loads(_open(Request(url, urlencode(fields).encode())))


def link_url(user="owner"):
    """The Kick page to send the browser to. PKCE (S256) plus a random state tie the answer to this request and account."""
    verifier, state, now = secrets.token_urlsafe(48), secrets.token_urlsafe(16), time.time()
    for s in [s for s, v in _pending.items() if v[1] < now]:
        del _pending[s]
    _pending[state] = (verifier, now + 600, user)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return "https://id.kick.com/oauth/authorize?" + urlencode({
        "response_type": "code", "client_id": ID, "redirect_uri": REDIRECT, "scope": SCOPES,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state})


def _save(user, d):
    fd = os.open(_path(user), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)


def _store_tokens(user, tok, keep=None):
    d = dict(keep or {})
    d.update(access=tok["access_token"], refresh=tok.get("refresh_token") or d.get("refresh"),
             exp=time.time() + float(tok.get("expires_in") or 0), scope=tok.get("scope") or d.get("scope") or "")
    _save(user, d)
    return d


def link_finish(code, state, user="owner"):
    """Trade the code for tokens and remember who linked. Raises ValueError for an unknown, stale or foreign state."""
    verifier, exp, who = _pending.pop(state, (None, 0, None))
    if not verifier or exp < time.time() or not code or who != user:
        raise ValueError("unknown or expired login")
    tok = _form("https://id.kick.com/oauth/token", grant_type="authorization_code", client_id=ID, client_secret=SECRET,
                redirect_uri=REDIRECT, code_verifier=verifier, code=code)
    me = json.loads(_open(Request("https://api.kick.com/public/v1/users",
                                  headers={"Authorization": "Bearer " + tok["access_token"]})))["data"][0]
    _store_tokens(user, tok, {"user_id": me.get("user_id"), "name": me.get("name")})


def _link(user):
    try:
        with open(_path(user)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def linked(user="owner"):
    """The linked Kick user's name, or None."""
    return _link(user).get("name")


def can_chat(user="owner"):
    return "chat:write" in (_link(user).get("scope") or "").split()  # a link made before chat existed lacks it


def unlink(user="owner"):
    try:
        os.remove(_path(user))
    except OSError:
        pass


def _user_token(user):
    d = _link(user)
    if not d.get("access"):
        raise ValueError("Kick isn't linked")
    if d.get("exp", 0) - 60 > time.time():
        return d["access"]
    if not d.get("refresh"):
        raise ValueError("The Kick link expired; link again")
    tok = _form("https://id.kick.com/oauth/token", grant_type="refresh_token", client_id=ID, client_secret=SECRET,
                refresh_token=d["refresh"])
    return _store_tokens(user, tok, d)["access"]


def _channel(slug):
    """Kick's record for a channel (by slug), or None."""
    req = Request("https://api.kick.com/public/v1/channels?" + urlencode({"slug": slug}),
                  headers={"Authorization": "Bearer " + _app_token()})
    data = json.loads(_open(req)).get("data") or []
    return data[0] if data else None


def send_chat(user, slug, text):
    """Send `text` to a channel's chat as the account's linked Kick user. Raises ValueError with a sentence for the UI."""
    text = (text or "").strip()
    if not text or len(text) > 500:
        raise ValueError("Messages are 1 to 500 characters.")
    if not can_chat(user):
        raise ValueError("Unlink and link Kick again to allow chatting.")
    try:
        if slug not in _ids:
            ch = _channel(slug)
            if not ch:
                raise ValueError("Kick doesn't know that channel.")
            _ids[slug] = ch["broadcaster_user_id"]
        body = json.dumps({"broadcaster_user_id": _ids[slug], "content": text, "type": "user"}).encode()
        r = json.loads(_open(Request("https://api.kick.com/public/v1/chat", body, {
            "Authorization": "Bearer " + _user_token(user), "Content-Type": "application/json"})))
    except ValueError:
        raise
    except Exception as e:
        print("Kick chat:", describe(e), flush=True)
        raise ValueError("Kick refused the message." + (" Link again to refresh permission." if "403" in str(e) or "401" in str(e) else ""))
    if not (r.get("data", r)).get("is_sent"):
        raise ValueError("Kick didn't send it (slow mode, followers-only or banned?).")


def check(slug):
    """{"live", "game", "title", "uptime_s", "viewers"} for a channel, {"live": False} when offline,
    None when it could not be checked (no creds, network, unknown channel)."""
    if not enabled():
        return None
    try:
        ch = _channel(slug)
    except Exception as e:
        print("Kick API:", describe(e), flush=True)
        _token["value"] = None  # an expired or revoked token gets a fresh one next time
        return None
    if not ch:
        return None
    st = ch.get("stream") or {}
    if not st.get("is_live"):
        return {"live": False}
    up = None
    try:
        up = time.time() - datetime.fromisoformat(st["start_time"].replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except (KeyError, ValueError, AttributeError):
        pass
    return {"live": True, "game": (ch.get("category") or {}).get("name"), "title": ch.get("stream_title"),
            "uptime_s": up, "viewers": st.get("viewer_count")}


def live_now(first=24):
    """Who is live right now, biggest first, in the same shape as twitch.live_now. [] when it can't be fetched."""
    if not enabled():
        return []
    try:
        req = Request("https://api.kick.com/public/v1/livestreams?" + urlencode({"limit": max(1, min(int(first), 100)), "sort": "viewer_count"}),
                      headers={"Authorization": "Bearer " + _app_token()})
        rows = json.loads(_open(req)).get("data") or []
    except Exception as e:
        print("Kick live list:", describe(e), flush=True)
        _token["value"] = None
        return []
    return [{"login": r["slug"], "name": r["slug"], "title": r.get("stream_title"), "game": (r.get("category") or {}).get("name"),
             "viewers": r.get("viewer_count"), "avatar": r.get("profile_picture"), "thumb": r.get("thumbnail")} for r in rows if r.get("slug")]
