"""Kick's official API (api.kick.com) with an app token: is a channel live, its title, category and uptime.
The Pi can't read kick.com itself (it refuses non-browser clients), but the sanctioned API accepts a bearer token.
Needs TVREMOTE_KICK_CLIENT_ID and TVREMOTE_KICK_CLIENT_SECRET; without them enabled() is False and callers fall back.
shortcut: app token only (public data), no user login; add the authorization-code flow when accounts can link Kick."""
import json
import os
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
    return urlopen(req, timeout=timeout).read()


def _app_token():
    if _token["value"] and time.time() < _token["exp"] - 60:
        return _token["value"]
    body = urlencode({"grant_type": "client_credentials", "client_id": ID, "client_secret": SECRET}).encode()
    j = json.loads(_open(Request("https://id.kick.com/oauth/token", body)))
    _token["value"], _token["exp"] = j["access_token"], time.time() + float(j.get("expires_in") or 3600)
    return _token["value"]


def check(slug):
    """{"live", "game", "title", "uptime_s", "viewers"} for a channel, {"live": False} when offline,
    None when it could not be checked (no creds, network, unknown channel)."""
    if not enabled():
        return None
    try:
        req = Request("https://api.kick.com/public/v1/channels?" + urlencode({"slug": slug}),
                      headers={"Authorization": "Bearer " + _app_token()})
        data = json.loads(_open(req)).get("data") or []
    except Exception:
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
