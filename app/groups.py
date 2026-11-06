"""Multi-stream groups: saved sets of 2-4 streams that start together on the Pi's screen, and the watching history that suggests new ones.

One file per account (like favourites). Pure functions over a plain dict so they are easy to test:
    {"groups": [{"id", "name", "members": [{"url", "name", "source"}], "created"}],
     "history": {key: {"urls": [...], "count": n, "last": t}},      # sets of streams that were on the screen together, per watching session
     "dismissed": [key, ...]}                                       # suggestions the person turned down
A "key" names a set of streams regardless of order."""
import os
import time
import uuid

import store

MIN_MEMBERS, MAX_MEMBERS = 2, 4
SUGGEST_AFTER = 2  # a set becomes a suggestion once it has been on the screen together in this many separate sessions


def load(path):
    import json
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    return {"groups": list(d.get("groups") or []), "history": dict(d.get("history") or {}), "dismissed": list(d.get("dismissed") or [])}


def save(path, d):
    store.write_json(path, d)


def key_of(urls):
    return "|".join(sorted({u.strip().lower() for u in urls if u}))


def auto_name(members):
    return ", ".join(m["name"] for m in members)[:60]


def clean_members(urls, describe):
    """[{url, name, source}] for 2-4 different links, in the order given; None when that is not a group. describe(url) -> (name, source) or None
    for a link that is not allowed."""
    out, seen = [], set()
    for u in urls or []:
        d = describe(u)
        if not d or u.lower() in seen:
            continue
        seen.add(u.lower())
        out.append({"url": u, "name": d[0], "source": d[1]})
    return out[:MAX_MEMBERS] if len(out) >= MIN_MEMBERS else None


def add(d, members, name=None, now=None):
    """Save a group. A set that is already saved is returned as it is (no duplicates). Returns the group."""
    k = key_of(m["url"] for m in members)
    for g in d["groups"]:
        if key_of(m["url"] for m in g["members"]) == k:
            return g
    g = {"id": uuid.uuid4().hex[:10], "name": (name or auto_name(members)).strip()[:60], "members": members, "created": now or time.time()}
    d["groups"].append(g)
    d["dismissed"] = [x for x in d["dismissed"] if x != k]  # saved on purpose: it is no longer a turned-down suggestion
    return g


def remove(d, gid):
    n = len(d["groups"])
    d["groups"] = [g for g in d["groups"] if g["id"] != gid]
    return len(d["groups"]) != n


def rename(d, gid, name):
    for g in d["groups"]:
        if g["id"] == gid and name.strip():
            g["name"] = name.strip()[:60]
            return True
    return False


def record(d, urls, now=None):
    """The streams on the screen have been the same for a while: count this as one more session of that set. Returns its count."""
    urls = [u for u in urls if u]
    if len(urls) < MIN_MEMBERS:
        return 0
    k = key_of(urls)
    h = d["history"].setdefault(k, {"urls": urls[:MAX_MEMBERS], "count": 0, "last": 0})
    h["count"] += 1
    h["last"] = now or time.time()
    if len(d["history"]) > 60:  # the history can't only grow: keep the 40 most recent sets
        keep = sorted(d["history"].items(), key=lambda kv: kv[1]["last"], reverse=True)[:40]
        d["history"] = dict(keep)
    return h["count"]


def dismiss(d, key):
    if key not in d["dismissed"]:
        d["dismissed"].append(key)


def suggestions(d, describe):
    """Sets watched together often enough, not saved and not turned down, most often first: [{key, name, members, count}]."""
    saved = {key_of(m["url"] for m in g["members"]) for g in d["groups"]}
    out = []
    for k, h in d["history"].items():
        if h["count"] < SUGGEST_AFTER or k in saved or k in d["dismissed"]:
            continue
        members = clean_members(h["urls"], describe)
        if members:
            out.append({"key": k, "name": auto_name(members), "members": members, "count": h["count"], "last": h["last"]})
    return sorted(out, key=lambda s: (s["count"], s["last"]), reverse=True)[:6]
