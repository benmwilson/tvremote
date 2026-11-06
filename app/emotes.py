"""Emotes for a Twitch channel's chat: Twitch's own plus the three big third-party sets (7TV, BetterTTV, FrankerFaceZ), each as the
channel's own emotes and the global ones. The phone shows them in the chat feed (a word that is an emote name becomes the picture),
in the picker, and as suggestions while typing. Pictures load straight from each provider's CDN, so the Pi only fetches the lists.
Lists are cached (channel sets 15 minutes, global sets 6 hours; a failed fetch is remembered for a minute so a dead provider is not hammered)."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen

import twitch

MAX_PER_SET = 1500  # a few streamers have thousands of 7TV emotes: enough for the picker and for rendering chat
TTL = {"channel": 900, "global": 21600, "failed": 60}
_cache = {}  # (provider, scope, key) -> (fetched at, ttl, emotes)
_lock = threading.Lock()
PROVIDERS = ("twitch", "7tv", "bttv", "ffz")


def _get(url, timeout=8):  # one place to stub in tests
    req = Request(url, headers={"User-Agent": "tvremote/1.0", "Accept": "application/json"})
    return json.loads(urlopen(req, timeout=timeout).read())


def _e(name, url, animated=False):
    return {"n": name, "u": url, "a": bool(animated)}


def _7tv_list(emotes):
    out = []
    for e in emotes or []:
        host = (e.get("data") or {}).get("host") or {}
        if e.get("name") and host.get("url"):
            out.append(_e(e["name"], "https:" + host["url"].rstrip("/") + "/2x.webp", (e.get("data") or {}).get("animated")))
    return out


def _bttv_list(emotes):
    return [_e(e["code"], f"https://cdn.betterttv.net/emote/{e['id']}/2x.webp", e.get("imageType") == "gif") for e in emotes or [] if e.get("code") and e.get("id")]


def _ffz_list(sets, ids):
    out = []
    for i in ids:
        for e in ((sets or {}).get(str(i)) or {}).get("emoticons") or []:
            u = (e.get("urls") or {}).get("2") or (e.get("urls") or {}).get("1")
            if e.get("name") and u:
                out.append(_e(e["name"], "https:" + u if u.startswith("//") else u))
    return out


def _twitch_list(rows):
    return [_e(r["name"], f"https://static-cdn.jtvnw.net/emoticons/v2/{r['id']}/default/dark/2.0") for r in rows or [] if r.get("name") and r.get("id")]


def _fetch(provider, scope, tid):
    """The emotes of one provider for one scope ('global', or 'channel' with the Twitch user id)."""
    if provider == "7tv":
        j = _get("https://7tv.io/v3/emote-sets/global" if scope == "global" else f"https://7tv.io/v3/users/twitch/{tid}")
        return _7tv_list(j.get("emotes") if scope == "global" else (j.get("emote_set") or {}).get("emotes"))
    if provider == "bttv":
        if scope == "global":
            return _bttv_list(_get("https://api.betterttv.net/3/cached/emotes/global"))
        j = _get(f"https://api.betterttv.net/3/cached/users/twitch/{tid}")
        return _bttv_list((j.get("channelEmotes") or []) + (j.get("sharedEmotes") or []))
    if provider == "ffz":
        if scope == "global":
            j = _get("https://api.frankerfacez.com/v1/set/global")
            return _ffz_list(j.get("sets"), j.get("default_sets") or [])
        j = _get(f"https://api.frankerfacez.com/v1/room/id/{tid}")
        return _ffz_list(j.get("sets"), [(j.get("room") or {}).get("set")])
    if provider == "twitch":
        rows = twitch._helix("chat/emotes/global" if scope == "global" else "chat/emotes", [] if scope == "global" else [("broadcaster_id", tid)]).get("data")
        return _twitch_list(rows)
    return []


def _set(provider, scope, tid):
    key = (provider, scope, tid if scope == "channel" else "")
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < hit[1]:
            return hit[2]
    try:
        emotes, ttl = _fetch(provider, scope, tid)[:MAX_PER_SET], TTL[scope]
    except Exception as e:
        print(f"Emotes {provider} {scope}:", twitch.describe(e), flush=True)
        emotes, ttl = [], TTL["failed"]
    with _lock:
        _cache[key] = (now, ttl, emotes)
    return emotes


def kick_native(slug):
    """Kick's own emotes for a channel (the channel's, then the global ones): [{"provider": "kick", "scope", "emotes": [{"n", "u", "a", "id"}]}].
    From the same endpoint kick.com's own chat uses (Kick answers a browser's request, not a plain script's)."""
    key = ("kick", "native", slug)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < hit[1]:
        return hit[2]
    try:
        req = Request(f"https://kick.com/emotes/{slug}", headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        groups = json.loads(urlopen(req, timeout=8).read())
        out = []
        for g in groups:
            scope = "global" if g.get("id") == "Global" else "channel" if g.get("slug") else None  # the channel's own group has a slug; "Emoji" has neither
            if scope is None:
                continue  # (the emoji group is left out: the phone's own keyboard has them)
            em = [{"n": e["name"], "u": f"https://files.kick.com/emotes/{e['id']}/fullsize", "a": False, "id": e["id"]} for e in g.get("emotes") or [] if e.get("name") and e.get("id")]
            if em:
                out.append({"provider": "kick", "scope": scope, "emotes": em[:MAX_PER_SET]})
        ttl = TTL["channel"]
    except Exception as e:
        print("Emotes kick:", twitch.describe(e), flush=True)
        out, ttl = [], TTL["failed"]
    with _lock:
        _cache[key] = (now, ttl, out)
    return out


def _kick_channel_7tv(uid):
    j = _get(f"https://7tv.io/v3/users/kick/{uid}")
    return _7tv_list((j.get("emote_set") or {}).get("emotes"))


def for_kick(uid):
    """The 7TV emotes for a Kick streamer (by Kick user id): the channel's own set, then 7TV's global set. [] sets are left out."""
    sets = []
    if uid:
        key = ("7tv", "kick-channel", str(uid))
        now = time.time()
        with _lock:
            hit = _cache.get(key)
        if hit and now - hit[0] < hit[1]:
            em = hit[2]
        else:
            try:
                em, ttl = _kick_channel_7tv(uid)[:MAX_PER_SET], TTL["channel"]
            except Exception as e:
                print("Emotes 7tv kick:", twitch.describe(e), flush=True)
                em, ttl = [], TTL["failed"]
            with _lock:
                _cache[key] = (now, ttl, em)
        sets.append({"provider": "7tv", "scope": "channel", "emotes": em})
    sets.append({"provider": "7tv", "scope": "global", "emotes": _set("7tv", "global", None)})
    return [s for s in sets if s["emotes"]]


def for_kick_channel(slug, uid):
    """Everything for a Kick channel's chat on the phone: Kick's own emotes and the 7TV sets, the channel's before the global ones."""
    sets = kick_native(slug) + for_kick(uid)
    return sorted(sets, key=lambda s: s["scope"] == "global")  # (stable: Kick's before 7TV's within each scope)


def for_channel(login):
    """[{"provider", "scope", "emotes": [{"n", "u", "a"}]}] for a Twitch channel: channel sets first, then the global ones.
    Providers that have nothing for it (or are down) are left out. Without Twitch credentials only the third-party global sets come back."""
    tid = None
    if twitch.enabled():
        u = twitch.users([login]).get((login or "").lower())
        tid = u["id"] if u else None
    jobs = [(p, "channel") for p in PROVIDERS] if tid else []
    jobs += [(p, "global") for p in PROVIDERS if twitch.enabled() or p != "twitch"]
    with ThreadPoolExecutor(max_workers=8) as ex:  # the providers are independent: ask them all at once
        results = list(ex.map(lambda j: _set(j[0], j[1], tid), jobs))
    return [{"provider": p, "scope": sc, "emotes": em} for (p, sc), em in zip(jobs, results) if em]
