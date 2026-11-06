"""Twitch's official API (Helix) with an app token: is a channel live, its title/game/viewers, who is live now, profile pictures.
Needs TVREMOTE_TWITCH_CLIENT_ID and TVREMOTE_TWITCH_CLIENT_SECRET; without them enabled() is False and callers fall back.
Accounts can also link their own Twitch login (authorization code) to send chat messages as themselves.
The login redirect reuses the https listener Kick's login already needs (same host and port, path /auth/twitch/callback)."""
import json
import os
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

ID = os.environ.get("TVREMOTE_TWITCH_CLIENT_ID", "")
SECRET = os.environ.get("TVREMOTE_TWITCH_CLIENT_SECRET", "")
DATA = os.environ.get("TVREMOTE_DATA", "/data")
SCOPES = "user:write:chat"


def _default_redirect():
    k = urlparse(os.environ.get("TVREMOTE_KICK_REDIRECT", ""))
    return f"{k.scheme}://{k.netloc}/auth/twitch/callback" if k.netloc else ""


REDIRECT = os.environ.get("TVREMOTE_TWITCH_REDIRECT") or _default_redirect()  # must be registered on the Twitch app too
_token = {"value": None, "exp": 0.0}
_pending = {}  # state -> (expiry, account)
_users = {}  # login -> (fetched at, {"id", "name", "avatar"}) : names and pictures change rarely
USER_TTL = 86400


def enabled():
    return bool(ID and SECRET)


def can_link():
    return enabled() and bool(REDIRECT)


def _open(req, timeout=10):  # one place to stub in tests
    req.add_header("User-Agent", "tvremote/1.0")
    return urlopen(req, timeout=timeout).read()


def describe(e):
    body = ""
    if hasattr(e, "read"):
        try:
            body = " " + e.read(300).decode(errors="replace")
        except Exception:
            pass
    return f"{type(e).__name__}: {e}{body}"


def _form(url, **fields):
    return json.loads(_open(Request(url, urlencode(fields).encode())))


def _app_token():
    if _token["value"] and time.time() < _token["exp"] - 60:
        return _token["value"]
    j = _form("https://id.twitch.tv/oauth2/token", grant_type="client_credentials", client_id=ID, client_secret=SECRET)
    _token["value"], _token["exp"] = j["access_token"], time.time() + float(j.get("expires_in") or 3600)
    return _token["value"]


def _helix(path, params=(), token=None, body=None):
    req = Request("https://api.twitch.tv/helix/" + path + (("?" + urlencode(params)) if params else ""),
                  json.dumps(body).encode() if body is not None else None,
                  {"Authorization": "Bearer " + (token or _app_token()), "Client-Id": ID, "Content-Type": "application/json"})
    out = _open(req)
    return json.loads(out) if out else {}


def users(logins):
    """{login: {"id", "name", "avatar"}} for the channels Twitch knows (others are left out). Batched, remembered for a day."""
    now, out, need = time.time(), {}, []
    for l in {l.lower() for l in logins if l}:
        hit = _users.get(l)
        if hit and now - hit[0] < USER_TTL:
            out[l] = hit[1]
        else:
            need.append(l)
    for i in range(0, len(need), 100):
        chunk = need[i:i + 100]
        try:
            rows = _helix("users", [("login", l) for l in chunk]).get("data") or []
        except Exception as e:
            print("Twitch users:", describe(e), flush=True)
            _token["value"] = None
            continue
        for r in rows:
            u = {"id": r["id"], "name": r.get("display_name") or r["login"], "avatar": r.get("profile_image_url")}
            _users[r["login"].lower()] = (now, u)
            out[r["login"].lower()] = u
    return out


def _uptime(started):
    try:
        return time.time() - datetime.fromisoformat(started.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except (AttributeError, ValueError):
        return None


def check(login):
    """{"live", "game", "title", "uptime_s", "viewers"} for a channel, {"live": False} when offline,
    None when it could not be checked (no credentials, network)."""
    if not enabled():
        return None
    try:
        rows = _helix("streams", [("user_login", login)]).get("data") or []
    except Exception as e:
        print("Twitch API:", describe(e), flush=True)
        _token["value"] = None  # an expired or revoked token gets a fresh one next time
        return None
    if not rows or rows[0].get("type") != "live":
        return {"live": False}
    s = rows[0]
    return {"live": True, "game": s.get("game_name"), "title": s.get("title"), "uptime_s": _uptime(s.get("started_at")), "viewers": s.get("viewer_count")}


def live_now(first=24, game=None):
    """Who is live right now, biggest first: [{"login", "name", "title", "game", "viewers", "avatar", "thumb"}]. [] when it can't be fetched."""
    if not enabled():
        return []
    params = [("first", max(1, min(int(first), 100)))] + ([("game_id", game)] if game else [])
    try:
        rows = _helix("streams", params).get("data") or []
    except Exception as e:
        print("Twitch live list:", describe(e), flush=True)
        _token["value"] = None
        return []
    pics = users([r["user_login"] for r in rows])
    return [{"login": r["user_login"], "name": r.get("user_name") or r["user_login"], "title": r.get("title"), "game": r.get("game_name"),
             "viewers": r.get("viewer_count"), "avatar": (pics.get(r["user_login"]) or {}).get("avatar"), "lang": r.get("language"), "started": r.get("started_at"),
             "thumb": (r.get("thumbnail_url") or "").replace("{width}", "440").replace("{height}", "248"),
             "game_img": f"https://static-cdn.jtvnw.net/ttv-boxart/{r['game_id']}-52x72.jpg" if r.get("game_id") else None}
            for r in rows if r.get("type") == "live"]


def search_live(query, first=10):
    """Live channels whose name matches `query` (Twitch's own channel search, live ones only), in live_now's shape. [] on any failure.
    The browse list only holds the biggest streams; this is how a small streamer you type the name of is found."""
    if not enabled() or not query:
        return []
    try:
        found = _helix("search/channels", [("query", query), ("live_only", "true"), ("first", max(1, min(int(first), 25)))]).get("data") or []
    except Exception as e:
        print("Twitch search:", describe(e), flush=True)
        return []
    return live_among([r["broadcaster_login"] for r in found if r.get("is_live") and r.get("broadcaster_login")])


def live_among(logins):
    """The ones of these channel names that are live right now, in live_now's shape ([] on any failure). One request for up to 100 names:
    this is how the browse list finds out whether a streamer from the other site is live here too."""
    logins = [l for l in dict.fromkeys(x.lower() for x in logins if x)][:100]
    if not enabled() or not logins:
        return []
    try:
        rows = _helix("streams", [("user_login", l) for l in logins]).get("data") or []
    except Exception as e:
        print("Twitch live lookup:", describe(e), flush=True)
        _token["value"] = None
        return []
    pics = users([r["user_login"] for r in rows])
    return [{"login": r["user_login"], "name": r.get("user_name") or r["user_login"], "title": r.get("title"), "game": r.get("game_name"),
             "viewers": r.get("viewer_count"), "avatar": (pics.get(r["user_login"]) or {}).get("avatar"), "lang": r.get("language"), "started": r.get("started_at"),
             "thumb": (r.get("thumbnail_url") or "").replace("{width}", "440").replace("{height}", "248"),
             "game_img": f"https://static-cdn.jtvnw.net/ttv-boxart/{r['game_id']}-52x72.jpg" if r.get("game_id") else None}
            for r in rows if r.get("type") == "live"]


# ---- linking an account (so chat messages go out as that person) ----

def _path(user):
    return os.path.join(DATA, "twitch_link.json" if user == "owner" else f"twitch_link.{user}.json")


def _save(user, d):
    fd = os.open(_path(user), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)


def _link(user):
    try:
        with open(_path(user)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def link_url(user="owner"):
    state, now = secrets.token_urlsafe(16), time.time()
    for s in [s for s, v in _pending.items() if v[0] < now]:
        del _pending[s]
    _pending[state] = (now + 600, user)
    return "https://id.twitch.tv/oauth2/authorize?" + urlencode({
        "response_type": "code", "client_id": ID, "redirect_uri": REDIRECT, "scope": SCOPES, "state": state, "force_verify": "true"})


def _store(user, tok, keep=None):
    d = dict(keep or {})
    d.update(access=tok["access_token"], refresh=tok.get("refresh_token") or d.get("refresh"),
             exp=time.time() + float(tok.get("expires_in") or 0), scope=" ".join(tok["scope"]) if isinstance(tok.get("scope"), list) else d.get("scope", ""))
    _save(user, d)
    return d


def link_finish(code, state, user="owner"):
    """Trade the code for tokens and remember who linked. Raises ValueError for an unknown, stale or foreign state."""
    exp, who = _pending.pop(state, (0, None))
    if exp < time.time() or not code or who != user:
        raise ValueError("unknown or expired login")
    tok = _form("https://id.twitch.tv/oauth2/token", client_id=ID, client_secret=SECRET, code=code, grant_type="authorization_code", redirect_uri=REDIRECT)
    me = _helix("users", token=tok["access_token"])["data"][0]  # no login given: the token's own user
    _store(user, tok, {"user_id": me["id"], "name": me.get("display_name") or me["login"]})


def linked(user="owner"):
    return _link(user).get("name")


def can_chat(user="owner"):
    return "user:write:chat" in (_link(user).get("scope") or "").split()


def unlink(user="owner"):
    try:
        os.remove(_path(user))
    except OSError:
        pass


def _user_token(user):
    d = _link(user)
    if not d.get("access"):
        raise ValueError("Twitch isn't linked")
    if d.get("exp", 0) - 60 > time.time():
        return d["access"]
    if not d.get("refresh"):
        raise ValueError("The Twitch link expired; link again")
    tok = _form("https://id.twitch.tv/oauth2/token", client_id=ID, client_secret=SECRET, grant_type="refresh_token", refresh_token=d["refresh"])
    return _store(user, tok, d)["access"]


def send_chat(user, login, text):
    """Send `text` to a channel's chat as the account's linked Twitch user. Raises ValueError with a sentence for the UI."""
    text = (text or "").strip()
    if not text or len(text) > 500:
        raise ValueError("Messages are 1 to 500 characters.")
    if not can_chat(user):
        raise ValueError("Link Twitch to chat as yourself.")
    ch = users([login]).get(login.lower())
    if not ch:
        raise ValueError("Twitch doesn't know that channel.")
    try:
        r = _helix("chat/messages", token=_user_token(user), body={"broadcaster_id": ch["id"], "sender_id": _link(user)["user_id"], "message": text})
    except ValueError:
        raise
    except Exception as e:
        print("Twitch chat:", describe(e), flush=True)
        raise ValueError("Twitch refused the message." + (" Link again to refresh permission." if "401" in str(e) else ""))
    row = (r.get("data") or [{}])[0]
    if not row.get("is_sent", True):
        raise ValueError((row.get("drop_reason") or {}).get("message") or "Twitch didn't send it (slow mode, followers-only or banned?).")
