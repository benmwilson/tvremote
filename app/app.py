#!/usr/bin/env python3
"""TV Remote: web controller for mpv / streamlink on the Pi."""
import functools
import gzip
from datetime import datetime
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import signal
import subprocess
import threading
import time
import zlib
from urllib.parse import quote, urlparse

from flask import Flask, Response, abort, copy_current_request_context, g, has_request_context, jsonify, send_file, redirect, request, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("TVREMOTE_DATA", "/data")
PORT = int(os.environ.get("TVREMOTE_PORT", "8765"))
CEC_PHYS = os.environ.get("TVREMOTE_CEC_PHYS", "1.0.0.0")
SOCK = "/tmp/tvremote-mpv.sock"
FAV_FILE = os.path.join(DATA, "favourites.json")
LOG_FILE = os.path.join(DATA, "player.log")

# Routing table: one line per site. player: "streamlink", "ytdlp" or "auto" (YouTube: live -> streamlink).
SOURCES = [
    # (name,      hosts,                              player)
    ("Kick",    ("kick.com",),                       "streamlink"),
    ("Twitch",  ("twitch.tv",),                      "streamlink"),
    ("YouTube", ("youtube.com", "youtu.be"),         "auto"),
]
SL_DEFAULT = "1080p60,best"
YTDL_HEIGHTS = ("1080", "720", "480")

import hlsproxy
import groups
import inproc
import live
import access
import accounts
import activity
import chat
import chatwin
import discover
import sessions
import store
import throttle
import kick
import mpvipc
import multi
import pihealth
from multi import cap
import roku
import shell
import targets
import tlscallback
import tvconfig
from sessions import SESSION_DAYS, drop_guest, drop_session, drop_user_sessions, guest_sessions, new_session, session_user

try:
    from cast import CastTarget
except ImportError:  # pychromecast missing: Pi target only
    CastTarget = None
CAST = None  # created at startup (mDNS discovery runs in the background)

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 1_000_000  # every body this app takes is a few lines of JSON; refuse anything big (413)


@app.after_request
def security_headers(resp):
    """Basic hardening on every response: no sniffing, never framed by another site (clickjacking), no referrer sent to other sites,
    and nothing from the API is cached (it describes what is on the TV right now)."""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.path.startswith("/api/") and "Cache-Control" not in resp.headers:
        resp.headers["Cache-Control"] = "no-store"
    return resp


def tgt():
    """The Chromecast target for ?target=bedroom, None for the Pi (no target, or "living"). Anything else is
    refused: guessing would risk controlling the wrong TV."""
    return target_for(request.args.get("target") or "living")


def target_for(t):
    if t == "living":
        return None
    if t in TV_ONLY:  # never fall back to the Pi: nothing is plugged in here
        abort(Response(json.dumps({"ok": False, "error": "Nothing is plugged in to this TV to play on. Use its TV controls."}),
                       409, mimetype="application/json"))
    if t == "bedroom":
        if not CAST:  # never fall back to the Pi: that would control the wrong TV
            abort(Response(json.dumps({"ok": False, "error": "The bedroom Chromecast isn't available. "
                                       "Check that it is powered on and on the same Wi-Fi."}),
                           503, mimetype="application/json"))
        return CAST
    abort(Response(json.dumps({"ok": False, "error": "Unknown TV."}), 400, mimetype="application/json"))


lock = threading.Lock()
state = {"proc": None, "url": None, "quality": None, "source": None, "player": None}
# Several streams on the Pi's screen at once. Tile 0 is the main player above; the others are started by multi.py.
wall = multi.Wall(lambda url, q, sock, extra: player_cmd(url, q, sock, extra, direct=True)[:2], lambda url: resolve(url), lambda url: fav_name(url),
                  volume=lambda: settings.get("volume"))
_qcache = {}
_live_cache = {}


@app.before_request
def guard():
    """CSRF check, then login (a cookie from the login page, or Basic auth with an account for scripts)."""
    g.user = accounts.OWNER  # the stand-in for "nobody is set up yet"; a login cookie says who it really is
    origin = request.headers.get("Origin")
    if request.method != "GET" and origin and urlparse(origin).netloc != request.host:
        return Response("Cross-site request blocked", 403)
    if request.path == "/play" and request.headers.get("Sec-Fetch-Site") == "cross-site":
        return Response("Cross-site request blocked", 403)
    if not auth_required():
        # Nobody can log in until setup has made the first admin, and an upgrade's older data must not be readable meanwhile
        # either: only the wizard's own page and calls (and the container's health check) work.
        if ((request.method in ("GET", "HEAD") and (request.path in SETUP_GET or re.fullmatch(r"/icon-\d+\.png", request.path)))
                or (request.method == "POST" and request.path == "/api/setup/admin")
                or (request.remote_addr == "127.0.0.1" and request.path == "/api/status")):
            return None
        if request.method == "GET" and "text/html" in request.headers.get("Accept", ""):
            return redirect("/", 303)
        return Response(json.dumps({"ok": False, "error": "Finish the setup first."}), 403, mimetype="application/json")
    if re.fullmatch(r"/cast/[\w-]+/(live\.m3u8|seg)|/logo\.svg|/icon-\d+\.png|/manifest\.json|/request|/join/[\w-]+", request.path):
        return None
    if request.remote_addr == "127.0.0.1" and request.path == "/api/status":  # container healthcheck
        return None
    if request.path in ("/login", "/logout"):
        return None
    who = session_user(request.cookies.get("tv_auth", ""))  # one read of the session: valid and whose, together
    if who:
        g.user = who
        if who == accounts.GUEST and guest_blocked():
            return Response(json.dumps({"ok": False, "error": "Guests can control the TVs, but not do that."}), 403, mimetype="application/json")
        return None
    a = request.authorization
    wants_page = "text/html" in request.headers.get("Accept", "")
    # a browser page load needs the login cookie: Safari remembers the old password pop-up and would otherwise stay logged in after logout
    if a and not wants_page:  # curl and scripts: Basic auth with an account
        name = (a.username or "").strip().lower()
        if throttle.blocked(request.remote_addr, name):
            return too_many()
        if name and accounts.verify(name, a.password or ""):
            g.user = name
            return None
        throttle.fail(request.remote_addr, name)
        time.sleep(0.8)  # slows guessing, like the login page
    if request.method == "GET" and "text/html" in request.headers.get("Accept", ""):  # a browser: show the login page
        return redirect("/login?next=" + quote(request.full_path.rstrip("?")), 303)
    return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="TV Remote"'})


SETUP_GET = {"/", "/logo.svg", "/manifest.json", "/login", "/api/auth", "/api/setup"}  # what works before the first admin exists


# What a one-time-link guest may do: use the TVs and see the pages that show them. Everything else (including any route added
# later) is refused, so a new endpoint is closed to guests until it is listed here on purpose.
GUEST_GET = {"/", "/share", "/logo.svg", "/manifest.json", "/login", "/logout", "/play", "/api/auth", "/api/status", "/api/tv",
             "/api/qualities", "/api/targets", "/api/roku", "/api/recents", "/api/search", "/api/favourites", "/api/kick",
             "/api/shell", "/api/stats", "/api/chat/feed", "/api/sleep", "/api/roku/apps", "/api/roku/icon", "/api/watch", "/api/detect"}  # /api/watch checks the guest setting itself
GUEST_POST = {"/api/play", "/api/cmd", "/api/tv/wake", "/api/roku/key", "/api/sleep", "/api/targets/refresh", "/api/move", "/api/roku/launch", "/logout"}


def guest_blocked():
    path = request.path
    if request.method in ("GET", "HEAD"):
        return not (path in GUEST_GET or re.fullmatch(r"/icon-\d+\.png", path))
    return not (request.method == "POST" and path in GUEST_POST)


def auth_required():
    """Login is needed once accounts have ever been set up. A fresh install is closed to changes until setup makes the first admin.
    An unreadable accounts file counts as set up (fail closed)."""
    return accounts.initialised()


def too_many():
    return Response("Too many failed tries. Wait a few minutes and try again.", 429)


def safe_next(n):
    """Where to go after login: only a plain path on this site. Browsers treat a backslash like a slash, so backslashes and
    control characters are out too."""
    if not n or not n.startswith("/") or n.startswith("//") or "\\" in n or any(ord(c) < 32 for c in n):
        return "/"
    p = urlparse(n)
    return n if not p.scheme and not p.netloc else "/"


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0b0e14"><title>TV Remote login</title>
<style>:root{color-scheme:dark}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0e14;color:#f6f1ee;font:17px -apple-system,system-ui,sans-serif}
form{width:min(340px,86vw);display:grid;gap:14px}h1{margin:0 0 6px;font-size:28px}
input,button{font:inherit;min-height:52px;border-radius:14px;border:0;padding:0 16px;box-sizing:border-box;width:100%}
input{background:#1a2030;color:#f6f1ee}button{background:#62b0ff;color:#0b0e14;font-weight:700}.e{color:#ff6b6b;min-height:1.2em;margin:0}.pw{position:relative;display:block}.pw>input{padding-right:56px}.pw-t{position:absolute;right:4px;top:50%;transform:translateY(-50%);width:auto;min-height:40px;padding:0 12px;background:none;color:#8c97a8;font-size:14px}</style></head>
<body><form method="post" action="/login"><img src="/logo.svg" width="84" height="84" alt="" style="justify-self:center;border-radius:20px"><h1 style="text-align:center">TV Remote</h1>
<input name="username" placeholder="Username" autocomplete="username" autocapitalize="none" aria-label="Username" autofocus required>
<input type="password" name="password" placeholder="Password" autocomplete="current-password" aria-label="Password" required>
<input type="hidden" name="next" value="__NEXT__"><p class="e" role="alert">__ERR__</p><button>Log in</button><a href="/request" style="text-align:center;color:#8c97a8;padding:10px">Request access</a></form><script>document.querySelectorAll('input[type=password]').forEach(function(i){var w=document.createElement('span'),b=document.createElement('button');w.className='pw';i.replaceWith(w);w.append(i);var E='<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">',O=E+'<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/></svg>',C=E+'<path d="M4 4l16 16M9.9 5.8A8.6 8.6 0 0112 5.5c6 0 9.5 6.5 9.5 6.5a16 16 0 01-3 3.7M6.3 7.6C3.9 9.3 2.5 12 2.5 12s3.5 6.5 9.5 6.5c1.5 0 2.8-.4 4-1M9.9 9.9a3 3 0 004.2 4.2"/></svg>';b.type='button';b.className='pw-t';b.innerHTML=O;b.setAttribute('aria-label','Show password');b.onclick=function(){var s=i.type==='password';i.type=s?'text':'password';b.innerHTML=s?C:O;b.setAttribute('aria-label',s?'Hide password':'Show password')};w.append(b)})</script></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    nxt = safe_next(request.values.get("next", "/"))
    if not auth_required():
        return redirect(nxt, 303)
    err = ""
    if request.method == "POST":
        who = request.form.get("username", "").strip().lower()
        if throttle.blocked(request.remote_addr, who):
            return too_many()
        user = who if accounts.verify(who, request.form.get("password", "")) else None  # always runs the slow hash, even for an unknown name
        if user:
            throttle.ok(request.remote_addr, user)
            accounts.touch(user)
            activity.note(user, "Logged in")
            r = redirect(nxt, 303)
            r.set_cookie("tv_auth", new_session(user), max_age=SESSION_DAYS * 86400, httponly=True, samesite="Lax")
            return r
        throttle.fail(request.remote_addr, who)
        time.sleep(0.8)  # slows guessing
        err = "Wrong username or password"
    return Response(LOGIN_PAGE.replace("__NEXT__", html.escape(nxt, quote=True)).replace("__ERR__", err), 401 if err else 200,
                    mimetype="text/html", headers={"Cache-Control": "no-store"})


@app.route("/logout", methods=["GET", "POST"])
def logout():
    drop_session(request.cookies.get("tv_auth", ""))
    r = redirect("/login", 303)
    r.delete_cookie("tv_auth")
    return r


@app.get("/api/auth")
def api_auth():
    info = {}
    if g.user == accounts.GUEST:
        v = sessions.load().get(sessions.token_hash(request.cookies.get("tv_auth", ""))) or {}
        info = {"guest_until": v.get("exp"), "label": v.get("label", "")}
    role = accounts.role(g.user)
    return jsonify(required=auth_required(), user=g.user, owner=accounts.is_owner(g.user), role=role,
                   admin=role == "admin", guest=g.user == accounts.GUEST,
                   watch=g.user != accounts.GUEST or phone_settings()["phone_guests"], **info)


# ---------- routing ----------

def route(url):
    """Return (source name, player) for a URL."""
    host = (urlparse(url).hostname or "").lower()
    for name, hosts, player in SOURCES:
        if any(host == h or host.endswith("." + h) for h in hosts):
            return name, player
    return "Other", "ytdlp"


LIVE_CHECK_TTL = 300  # a stream can start or end, so an answer is only good for a few minutes


def youtube_is_live(url):
    if re.search(r"/live/?($|\?)", urlparse(url).path + "?"):
        return True
    now = time.time()
    hit = _live_cache.get(url)
    if hit and now - hit[0] < LIVE_CHECK_TTL:
        return hit[1]
    try:
        try:
            live = inproc.youtube_is_live(url)
        except Exception as e:
            app.logger.info("yt-dlp library failed (%s): using the command", e)
            out = subprocess.run(["yt-dlp", "--no-playlist", "--print", "is_live", url],
                                 capture_output=True, text=True, timeout=20).stdout
            live = out.strip().splitlines()[-1] == "True"
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return False  # could not tell: say "not live" this once, but don't remember it (a blip must not stick until a restart)
    for k in [k for k, v in _live_cache.items() if now - v[0] >= LIVE_CHECK_TTL]:
        del _live_cache[k]  # and the cache can't only grow
    _live_cache[url] = (now, live)
    return live


def resolve(url):
    """(source, player) with 'auto' resolved to streamlink or ytdlp."""
    source, player = route(url)
    if player == "auto":
        player = "streamlink" if youtube_is_live(url) else "ytdlp"
    return source, player


GUEST_SITES = ("kick.com", "twitch.tv", "youtube.com", "youtu.be")  # what a guest may play: a site, not an address (see below)


def guest_link_refused(url):
    """A guest on a one-time link may play things on the TVs, but must not be able to point the Pi at the home network (the router, the NAS,
    this app on localhost). Checking an address once is not enough (a redirect or a DNS answer can change after the check), so a guest is limited
    to links on the streaming sites this app is for: Kick, Twitch and YouTube. None when fine (or not a guest), else a sentence.
    Regular accounts are not limited: a movie on the NAS is theirs to play."""
    if not (has_request_context() and getattr(g, "user", None) == accounts.GUEST):
        return None
    p = urlparse(url or "")
    host = (p.hostname or "").lower()
    if p.scheme in ("http", "https") and any(host == s or host.endswith("." + s) for s in GUEST_SITES):
        return None
    return "Guests can play Kick, Twitch and YouTube links."


def normalise(text, site="kick"):
    """Pull the first URL out of shared text; a bare word becomes a channel on `site`."""
    text = (text or "").strip()
    m = re.search(r"https?://[^\s<>\"']+", text)
    if m:
        return m.group(0).rstrip(").,")
    if re.fullmatch(r"[A-Za-z0-9_\-]{2,40}", text):
        return f"https://{'twitch.tv' if site == 'twitch' else 'kick.com'}/{text}"
    return None


# ---------- mpv ----------

def mpv(cmd, timeout=1.0):
    """Send one command over mpv's IPC socket and return the reply."""
    return mpvipc.send(SOCK, cmd, timeout)


def sprop(sock, name):
    """One property of the mpv on `sock`, or None."""
    r = mpvipc.send(sock, ["get_property", name])
    return r.get("data") if r and r.get("error") == "success" else None


_title_warming = set()


def warm_title(url):
    """Fetch a stream's title and details in the background if they are not known: only the first window used to get them."""
    if not url or url in _qcache or url in _title_warming:
        return
    _title_warming.add(url)

    def run():
        try:
            sl_info(url)
        finally:
            _title_warming.discard(url)
    threading.Thread(target=run, daemon=True).start()


def sel_info():
    """(url, source, player, socket) of the stream the remote controls: the main one, or the selected one while several are on the screen."""
    if wall.active() and wall.selected:
        t = wall.extras[wall.selected - 1]
        source, player = resolve(t["url"])
        return t["url"], t.get("source") or source, player, wall.sock(wall.selected, SOCK)
    return state["url"], state["source"], state["player"], SOCK


def prop(name):
    r = mpv(["get_property", name])
    if r and r.get("error") == "success":
        return r.get("data")
    return None


def cec(*args):
    try:
        subprocess.run(["cec-ctl", "-d", "/dev/cec0", *args],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


SETTINGS_FILE = os.path.join(DATA, "settings.json")
settings = {"stats": False, "qr": True, "shell": os.environ.get("TVREMOTE_SHELL", "1") != "0"}  # the TV's home screen; TVREMOTE_SHELL=0 starts with it off
HOME = None  # the home screen (shell.Shell), created at startup


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            return {**settings, **json.load(f)}
    except (OSError, ValueError):
        return dict(settings)


def save_settings():
    store.write_json(SETTINGS_FILE, settings)


def screen(kind, detail="", source=None):
    """Show a screen on the TV's home screen, if it is on. `source` (Kick, Twitch, YouTube) tints it in that site's colour."""
    if HOME and settings.get("shell"):
        if kind == "playing" and wall.active():  # several streams: the background behind them, not a hidden window
            labels = [(fav_name(state["url"] or ""), state["source"], bool(state.get("ended")))] + [(wall.name(t["url"]), t.get("source"), bool(t.get("error"))) for t in wall.extras]
            HOME.show_wall(wall.geo, labels)
            return
        accent = shell.SRC_BGR.get(source)
        if accent:
            HOME.show(kind, detail, accent)
        else:
            HOME.show(kind, detail)


def describe_error(raw):
    """Plain words for the player's last error."""
    if not raw:
        return None
    if re.search(r"No playable streams|offline", raw, re.I):
        return "Isn't live right now"
    if re.search(r"HTTP.?error 404|Not Found", raw, re.I):
        return "That link wasn't found (404)"
    if re.search(r"logged-in|log in|login|sign in", raw, re.I):
        return "That needs a login, which isn't supported"
    return raw


def on_input(tv_id):
    """True when this TV is awake and already showing the HDMI input its target uses. Re-selecting an input the
    TV is already on relaunches it, which flashes the screen."""
    ip, hdmi = ROKU_IPS.get(tv_id), INPUTS.get(tv_id)
    if not (ip and hdmi):
        return False
    try:
        info = roku.info(ip)
    except (roku.RokuError, OSError):
        return False
    return info.get("power") == "PowerOn" and info.get("app_id") == "tvinput." + hdmi.lower()


_phys = {"t": 0, "v": None}


def cec_phys():
    """The Pi's HDMI address as the TV sees it now (cached 30 s), so moving the Pi to another TV or port needs no config change.
    Falls back to TVREMOTE_CEC_PHYS when the adapter doesn't say."""
    if time.time() - _phys["t"] > 30:
        try:
            out = subprocess.run(["cec-ctl", "-d", "/dev/cec0"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        _phys.update(t=time.time(), v=discover.parse_phys(out))
    return _phys["v"] or CEC_PHYS


def switch_tv_to_pi():
    if on_input("living"):
        return
    cec("--to", "0", "--image-view-on")
    cec("--active-source", f"phys-addr={cec_phys()}")


def stop_player():
    p = state["proc"]
    state["proc"] = None  # first, so the watcher knows this end was on purpose
    if p and p.poll() is None:
        try:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(3)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    chat_stop()


_chat = {"obj": None, "pref": False, "col": False, "win": None, "bar": None, "key": None}  # off by default


def chat_send(text):
    """Draw (or clear) the short-lived chat lines over the picture (used when several streams share the screen); True when mpv accepted it."""
    r = mpv(["osd-overlay", 1, "ass-events", text, 1920, 1080] if text else ["osd-overlay", 1, "none", ""])
    return bool(r and r.get("error") == "success")


def chat_column_send(text):
    """The chat column: the stream moves over (a quarter of the screen is freed on the right, and the picture sits at the top) and two windows fill
    the space: the chat on the right and a 'now playing' bar under the picture, on one shared background.
    text None: close them and give the screen back to the stream. True when it was done."""
    win = _chat["win"] = _chat["win"] or chatwin.chat_panel()
    bar = _chat["bar"] = _chat["bar"] or chatwin.bar_panel()
    if text is None:
        win.close()
        bar.close()
        if _chat["col"]:
            mpv(["set_property", "video-margin-ratio-right", 0])
            mpv(["set_property", "video-align-y", 0])
        _chat["col"] = False
        return True
    if not _chat["col"]:
        r = mpv(["set_property", "video-margin-ratio-right", 0.25])
        if not (r and r.get("error") == "success"):
            return False  # the stream's player is not up yet: the chat tries again in a moment
        mpv(["set_property", "video-align-y", -1])  # the picture at the top of its space; the bar goes underneath
        _chat["col"] = True
    url, src = state["url"] or "", state["source"] or "Twitch"
    if not (bar.running() or bar.open()) or not bar.update(chat.ass_bar(fav_name(url), stream_title(url) or "", src)):
        return False
    return (win.running() or win.open()) and win.update(text)


def chat_strip_send(text):
    """Several streams: the chat of the stream the remote controls shows in the strip on the right (the windows have made room for it). The window
    stays up while the feed changes; chat_strip_close() takes it away. True when it was drawn."""
    if text is None:
        return True
    win = _chat["win"] = _chat["win"] or chatwin.chat_panel()
    return bool((win.running() or win.open()) and win.update(text))


def chat_strip_close():
    if _chat["win"]:
        _chat["win"].close()


def chat_stop():
    c, _chat["obj"] = _chat["obj"], None
    if c:
        c.stop()


def chat_channel():
    """(channel, source) of the stream whose chat the TV would show, or (None, None): the one the remote controls."""
    url, source = sel_info()[:2]
    return (chat.channel_of(url or ""), source) if source in ("Twitch", "Kick") and (state["proc"] or wall.active()) else (None, None)


def chat_sync():
    """Match the chat to what is playing: a live Twitch or Kick channel with chat switched on. One stream: the picture moves over and the chat and a
    now-playing bar fill the space. Several: the windows make room (the chat layouts) and the strip shows the controlled stream's chat."""
    chat_stop()
    ch, source = chat_channel()
    _chat["key"] = (ch, source, wall.active(), bool(_chat["pref"]))
    if wall.active():
        want = bool(_chat["pref"])
        if want != wall.chat:
            wall.chat = want
            wall_later(wall_relayout)  # every window moves to its place beside (or without) the strip
        if not want:
            chat_strip_close()
        elif ch:
            _chat["obj"] = (chat.KickChat if source == "Kick" else chat.TwitchChat)(ch, chat_strip_send, column=True)
        return
    if wall.chat:
        wall.chat = False
    chat_strip_close() if not _chat["col"] else None
    if ch and _chat["pref"]:
        _chat["obj"] = (chat.KickChat if source == "Kick" else chat.TwitchChat)(ch, chat_column_send, column=True)


CHAT_QUALITY = "720p60,720p,480p,360p,best"  # the picture is shown in 1440x810 beside the chat: 1080p there dropped about 2 frames a second, 720p none


def _quality_height(q):
    m = re.match(r"(\d+)", q or "")
    return int(m.group(1)) if m else 0


def chat_lower_quality():
    """One stream, chat going on: play it at 720p (and remember the quality it had). A quality picked while the chat is up is left alone."""
    q = state.get("quality")
    if state["url"] and state["proc"] and not wall.active() and _quality_height(q) > 720 and chat.channel_of(state["url"]):
        _chat["restore_q"] = q
        with lock:
            start_player(state["url"], CHAT_QUALITY, "quality")


def chat_restore_quality():
    """Chat going off: give the stream the quality it had before the chat lowered it (only if it is still the lowered one)."""
    q, _chat["restore_q"] = _chat.get("restore_q"), None
    if q and state["url"] and state["proc"] and not wall.active() and _quality_height(state.get("quality")) == 720:
        with lock:
            start_player(state["url"], q, "quality")


def chat_refresh():
    """Another stream became the controlled one (or a window was swapped): show its chat. Cheap enough to call often."""
    if _chat["pref"] and wall.active():
        ch, source = chat_channel()
        if _chat.get("key") != (ch, source, True, True):
            chat_sync()


def _watch(proc):
    """Follow one player: hide the loading screen once it is up, and show how it ended."""
    for _ in range(120):  # up to a minute for mpv to open its window
        if proc.poll() is not None or prop("pid") is not None:
            break
        time.sleep(0.5)
    if proc.poll() is None:
        if settings.get("volume") is not None:
            mpv(["set_property", "volume", settings["volume"]])  # a new mpv starts at 100: put back what was chosen
        screen("playing")
    proc.wait()
    if state["proc"] is not proc:
        return  # replaced, or stopped on purpose
    if wall.active():  # one of several: say so in its slot, and leave the other windows alone
        state["ended"] = True
        screen("playing")
        return
    raw = last_error()
    name = fav_name(state["url"] or "")
    if raw and re.search(r"No playable streams|offline", raw, re.I):
        screen("offline", name)
    elif proc.returncode or raw:
        screen("error", describe_error(raw) or "The player stopped unexpectedly.")
    else:
        screen("ended", name)


def buffering_screen():
    """Drawn only while the picture is stalled, so redrawing costs the stream nothing."""
    return "\n".join([
        r"{\an7\pos(0,0)\p1\bord0\shad0\1c&H000000&\1a&HB0&}m 0 0 l 1920 0 1920 1080 0 1080{\p0}",
        r"{\an5\pos(960,500)\fs72\b1\bord0\shad0\1c&HF6F1EE&}Buffering…",
        r"{\an5\pos(960,590)\fs36\b0\bord0\shad0\1c&HA9978C&}Waiting for the stream. If this keeps happening, lower the quality on your phone."])


def lag_pill():
    """A small corner note, drawn only while the stream stutters (overlay 4), so a lag has an explanation on screen."""
    return "\n".join([
        r"{\an9\pos(1860,50)\p1\bord0\shad0\1c&H000000&\1a&H60&}m -330 -22 l 0 -22 0 34 -330 34{\p0}",
        r"{\an9\pos(1840,56)\fs34\b1\bord0\shad0\1c&HF6F1EE&}Stream lagging…"])


def stats_overlay(drop_delta, drops, cache_s, fps, stalls):
    """Debug numbers as plain text for show-text (top-left). Plain show-text stays up; osd-overlay text kept vanishing."""
    c = "-" if cache_s is None else f"{cache_s:.1f}s"
    text = f"dropped {drop_delta}/s ({drops} total)   buffer {c}   {fps or '-'} fps   stalls {stalls}"
    return text


def is_lagging(drop_delta, cache_s):
    """A stutter: a burst of dropped frames, or the buffer running nearly dry."""
    return drop_delta >= 8 or (cache_s is not None and cache_s < 0.6)


_swapping = threading.Event()  # set while osd_swap draws, so nothing else flashes over it


def _stall_monitor(proc):
    """Count stalls and show Buffering on the TV while the picture waits for data; a smaller note while it only stutters."""
    stalled_at = None
    drops, lagging, calm, tick, stats_up = None, False, 0, 0, False
    while proc.poll() is None:
        time.sleep(1)
        stalled = prop("paused-for-cache") is True
        d, cache = prop("frame-drop-count"), prop("demuxer-cache-duration")
        if isinstance(d, (int, float)) and prop("pause") is not True and not stalled:
            bad = drops is not None and not _swapping.is_set() and is_lagging(d - drops, cache if isinstance(cache, (int, float)) else None)
            calm = 0 if bad else calm + 1
            if bad and not lagging:
                lagging = True
                mpv(["osd-overlay", 4, "ass-events", lag_pill(), 1920, 1080])
            elif lagging and calm >= 4:  # stays up a few seconds so it can be read
                lagging = False
                mpv(["osd-overlay", 4, "none", ""])
        tick += 1
        if settings.get("stats") and tick % 2 == 0 and isinstance(d, (int, float)):  # a redraw costs a little, so only every 2 s
            fps = prop("estimated-vf-fps")
            mpv(["show-text", stats_overlay(int(d - drops) if drops is not None else 0, int(d),
                 cache if isinstance(cache, (int, float)) else None, round(fps) if isinstance(fps, (int, float)) else None,
                 state.get("stalls", 0)), 2600])  # a bit longer than the 2 s redraw, so it never blinks off
            if not stats_up:
                for k, v in (("osd-font-size", 26), ("osd-border-size", 2), ("osd-margin-x", 28), ("osd-margin-y", 22), ("osd-color", "#C8D0DC")):
                    mpv(["set_property", k, v])
            stats_up = True
        elif stats_up and not settings.get("stats"):
            for k, v in (("osd-font-size", 44), ("osd-border-size", 3), ("osd-margin-x", 60), ("osd-margin-y", 50), ("osd-color", "#FFFFFF")):
                mpv(["set_property", k, v])
            mpv(["show-text", "", 1])
            stats_up = False
        if isinstance(d, (int, float)):
            drops = d
        if stalled and stalled_at is None:
            stalled_at = time.time()
            state["stalls"] = state.get("stalls", 0) + 1
            if not _swapping.is_set():
                mpv(["osd-overlay", 3, "ass-events", buffering_screen(), 1920, 1080])
        elif not stalled and stalled_at is not None:
            state["stall_s"] = state.get("stall_s", 0) + time.time() - stalled_at
            stalled_at = None
            mpv(["osd-overlay", 3, "none", ""])


HOT = os.environ.get("TVREMOTE_HOT", "1") != "0"  # change a window's stream in place where it can (set 0 to always restart the window)


_addr_cache = {}  # (link, quality) -> (when, address). Remembered a few minutes, so changing a window's stream does not wait for streamlink
ADDR_TTL = 300


def stream_url(url, q, low=False):
    """The stream's own address (what streamlink would fetch), so mpv can play it directly. A window that plays an address can change
    stream with one command (loadfile) instead of being closed and opened again. low: run at a low priority (warming ahead of time)."""
    hit = _addr_cache.get((url, q))
    if hit and time.time() - hit[0] < ADDR_TTL:
        return hit[1]
    try:
        out = [inproc.stream_address(url, q)]  # in this process: 0.4 s, not 5-9 s for a new command (see inproc.py)
    except RuntimeError:
        raise  # nothing to play (offline...): the command would say the same
    except Exception as e:
        app.logger.info("streamlink library failed (%s): using the command", e)
        p = subprocess.run((["nice", "-n", "10"] if low else []) + ["streamlink", "--stream-url", url, q], capture_output=True, text=True, timeout=30)
        out = p.stdout.strip().splitlines()
        if p.returncode or not out:
            raise RuntimeError((p.stderr.strip().splitlines() or ["no stream"])[-1][:200])
    now = time.time()
    for k in [k for k, v in _addr_cache.items() if now - v[0] > ADDR_TTL]:
        del _addr_cache[k]
    _addr_cache[(url, q)] = (now, out[0])
    return out[0]


def sl_quality(quality, default_q=None):
    """A streamlink quality that is safe to pass on (stream names only, never a leading dash), else the default."""
    ok = lambda q: bool(q and re.fullmatch(r"[a-z0-9_]+(,[a-z0-9_]+)*", q) and not q.isdigit())
    return quality if ok(quality) else (default_q if ok(default_q) else SL_DEFAULT)


def hot_load(url, quality=None):
    """Change what the running player plays, in place: no window closes or opens, so nothing on the TV redraws. True when done; False when it
    can't be done this way (nothing running, a Twitch stream, a window that is the wrong shape), and the caller starts a player as usual."""
    p = state["proc"]
    if not (HOT and p and p.poll() is None and state.get("hot") and state.get("wid") == wall.main_wid()):
        return False
    source, player = resolve(url)
    if player != "streamlink" or source == "Twitch":
        return False
    width = wall.main_args()[1]
    q = sl_quality(quality, cap(player, width) if width else None)
    if url == state["url"]:
        _addr_cache.pop((url, q), None)  # Refresh: a fresh address, not the remembered one
    addr = stream_url(url, q)  # raises when it is offline: the caller then starts it the usual way
    if not _swapping.is_set() and not wall.active():  # a quality change draws its own card
        before = state.get("started")
        osd_swap(None, ready=lambda: _picture_changed_since(before), title=fav_name(url), source=source,
                 line="Refreshing the stream" if url == state["url"] else "Switching stream")
    r = mpvipc.send(SOCK, ["loadfile", addr, "replace"], 5)
    if not (r and r.get("error") == "success"):
        return False
    state.update(url=url, quality=q, source=source, player=player, started=time.time(), stalls=0, stall_s=0, ended=False)
    if is_channel(url):
        threading.Thread(target=livewatch.refresh_one, args=(url,), daemon=True).start()
    threading.Thread(target=sl_info, args=(url,), daemon=True).start()  # the title and details
    threading.Thread(target=switch_tv_to_pi, daemon=True).start()
    screen("playing")  # always: the home screen is a window behind the stream and must be paused (a multi-view collapsing in place leaves it running)
    return True


def hls_edge(text):
    """When the live edge of an HLS media playlist happened (seconds since 1970), from its program-date-time tags; None without them."""
    t = None
    for line in text.splitlines():
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            try:
                t = datetime.fromisoformat(line.split(":", 1)[1].strip().replace("Z", "+00:00")).timestamp()
            except ValueError:
                t = None
        elif line.startswith("#EXTINF:") and t is not None:
            try:
                t += float(line[8:].split(",")[0])
            except ValueError:
                pass
    return t


SYNC_MAX = 30  # seconds: how far back a window can be wound (it plays from what it has buffered)


def wall_sync():
    """Line the streams up in time. Each stream's playlist says when its live edge happened; the player says how far behind that edge the picture
    is. The picture's real time is the difference. The leading windows are wound back to the most delayed one, and play on from their buffer.
    {note, spread, aligned}: what it found, in words."""
    n = wall.count()
    names = [(wall.main_url or state["url"] or ""), *[t["url"] for t in wall.extras]]
    rects = multi.tiles(wall.geo, n) if wall.active() else None
    found = {}

    def measure(i):
        try:
            url = names[i]
            q = (multi.quality_for("streamlink", wall.main_quality if i == 0 else wall.extras[i - 1].get("quality")) or cap("streamlink", rects[i][2]))
            edge = hls_edge(hlsproxy._get(stream_url(url, q)).decode(errors="replace"))
            sock = wall.sock(i, SOCK)
            pos, cached = sprop(sock, "time-pos"), sprop(sock, "demuxer-cache-time")
            behind = max(0.0, cached - pos) if pos is not None and cached is not None else None
            found[i] = {"shown": edge - behind if edge is not None and behind is not None else None, "behind": behind}
        except Exception as e:
            app.logger.info("Sync: couldn't read window %d (%s)", i, e)
            found[i] = {"shown": None, "behind": None}
    ts = [threading.Thread(target=measure, args=(i,)) for i in range(n)]
    for x in ts:
        x.start()
    for x in ts:
        x.join()
    timed = {i: r["shown"] for i, r in found.items() if r["shown"] is not None}
    label = lambda i: chanName_py(names[i])
    untimed = [label(i) for i in range(n) if i not in timed]
    if len(timed) < 2:  # no stream has timing to compare: each is moved to its own live edge, which at least removes buffering lag
        for i, r in found.items():
            if r["behind"] and r["behind"] > 2:
                mpvipc.send(wall.sock(i, SOCK), ["seek", r["behind"] - 0.5, "relative"])
        return {"note": "These streams carry no timing to compare, so each was moved to its live edge.", "spread": None, "aligned": 0}
    target = min(timed.values())
    spread = max(timed.values()) - target
    if spread > SYNC_MAX:
        return {"note": f"They are {spread:.0f} s apart: too far to line up.", "spread": round(spread, 1), "aligned": 0}
    for i, shown in timed.items():
        d = shown - target
        if d > 0.4:
            mpvipc.send(wall.sock(i, SOCK), ["seek", -d, "relative+exact"])  # wind the leading picture back: it plays on from its buffer
    note = f"Lined up {len(timed)} streams. They were {spread:.1f} s apart." if spread > 0.4 else "Already in step."
    if untimed:
        note += " No timing for " + ", ".join(untimed) + "."
    return {"note": note, "spread": round(spread, 1), "aligned": len(timed)}


def chanName_py(url):
    p = [x for x in urlparse(url or "").path.split("/") if x]
    return p[0] if p else (url or "a stream")


def warm_addresses():
    """After the wall changes: work out, quietly, every stream's address at every window size in this layout. A swap then finds each one
    ready and takes effect at once."""
    if not (HOT and wall.active()):
        return
    rects = multi.tiles(wall.geo, wall.count())
    urls = [wall.main_url] + [t["url"] for t in wall.extras]
    wants = {cap("streamlink", r[2]) for r in rects}
    wants.add(sl_quality(wall.saved_quality, None))  # and the full-screen quality: removing a stream (or ending the multi-view) leaves one stream
    for u in urls:                                    # at that quality, which must be ready at once instead of a second's wait for streamlink
        source, player = resolve(u)
        if player != "streamlink" or source == "Twitch":
            continue
        for q in wants:
            try:
                stream_url(u, q, low=True)
            except Exception:
                pass


def _rewarm_loop():
    """Stream addresses are remembered for ADDR_TTL seconds. While a multi-view stays up, refresh them just before they run out, so removing
    a stream minutes later is as quick as removing it straight away."""
    while True:
        threading.Event().wait(ADDR_TTL - 60)
        try:
            warm_addresses()
        except Exception as e:
            app.logger.info("Couldn't refresh the stream addresses: %s", e)


def player_cmd(url, quality, sock, extra=(), default_q=None, direct=False):
    """(command, quality, source, player) for one mpv window. `extra` are more mpv arguments (a window name and size);
    default_q is the quality to use when none was asked for (a small window must not pull a 1080p60 stream)."""
    source, player = resolve(url)
    quality = quality or default_q
    ipc = f"--input-ipc-server={sock}"
    if player == "streamlink":
        # quality reaches argv: allow only stream names like "1080p60,best" (no leading "-")
        ok = quality and re.fullmatch(r"[a-z0-9_]+(,[a-z0-9_]+)*", quality) and not quality.isdigit()
        q = quality if ok else (default_q if default_q and re.fullmatch(r"[a-z0-9_]+(,[a-z0-9_]+)*", default_q) else SL_DEFAULT)
        if direct and source != "Twitch":  # Twitch's ad filter lives inside streamlink's own pipe, so Twitch keeps using it
            try:
                return ["mpv", ipc, *extra, stream_url(url, q)], q, source, player
            except Exception as e:  # offline, or no address: the normal way shows the usual "not live" message
                app.logger.warning("No direct stream address for %s (%s): playing it through streamlink", url, e)
        cmd = ["streamlink", "--player-verbose", "--player", "mpv", "--player-args", " ".join([ipc, *extra])]
        if source == "Twitch":
            cmd.append("--twitch-disable-ads")
        cmd += [url, q]
    else:
        h = quality if quality in YTDL_HEIGHTS else "1080"
        fmt = f"bv*[vcodec^=avc1][height<={h}]+ba/b[height<={h}]/b"
        cmd = ["mpv", ipc, *extra, f"--ytdl-format={fmt}", url]
        q = h
    return cmd, q, source, player


def start_player(url, quality=None, screen_kind="loading"):
    try:
        if hot_load(url, quality):
            return
    except Exception as e:  # offline, or no address: start it the usual way (which shows the usual "not live" message)
        app.logger.info("Not changed in place (%s): starting a new player", e)
    # before the old stream stops, so a swap never shows the desktop
    if wall.active():
        screen("playing")  # the wall's background: every slot says which stream it is waiting for
    else:
        screen(screen_kind, fav_name(url) + ("  \u00b7  " + quality if screen_kind == "quality" and quality else ""), route(url)[0])
    stop_player()
    try:
        os.unlink(SOCK)
    except FileNotFoundError:
        pass
    win, width = wall.main_args()  # while several streams share the screen, this one is a window of its own
    cmd, q, source, player = player_cmd(url, quality, SOCK, win, cap(resolve(url)[1], width) if width else None, direct=HOT)
    log = open(LOG_FILE, "w")
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True)
    _pending["t"] = 0.0
    state.update(proc=proc, url=url, quality=q, source=source, player=player, cmd=cmd, started=time.time(), stalls=0, stall_s=0, wid=wall.main_wid(), ended=False,
                 hot=cmd[0] == "mpv" and player == "streamlink")
    chat_sync()
    threading.Thread(target=_watch, args=(proc,), daemon=True).start()
    if is_channel(url):
        threading.Thread(target=livewatch.refresh_one, args=(url,), daemon=True).start()
    threading.Thread(target=_stall_monitor, args=(proc,), daemon=True).start()
    if player == "streamlink":  # warm the metadata cache for the title
        threading.Thread(target=sl_info, args=(url,), daemon=True).start()
    threading.Thread(target=switch_tv_to_pi, daemon=True).start()


def log_lines(tail=100_000):
    """Player log lines without mpv's per-frame 'AV:' status spam (it ends in CR, so splitlines splits it)."""
    try:
        with open(LOG_FILE, errors="replace") as f:
            lines = f.read()[-tail:].splitlines()
    except OSError:
        return []
    return [l for l in lines if not re.match(r"\s*(AV|A|V):", l)]


def last_error():
    """Last error-looking line from the player log, e.g. 'HTTP error 404 Not Found'."""
    lines = log_lines()
    # specific causes first; generic "Exiting..."/"Failed to open" lines are only a fallback
    for pat in (r"HTTP.?error|ERROR:|logged-in|login|not found|no playable|unsupported", r"error|failed"):
        for line in reversed(lines):
            if re.search(pat, line, re.I) and not line.startswith(("Exiting", "Failed to open")):
                return re.sub(r"^\[[^\]]*\]\s*(ERROR:\s*)?", "", line.strip())[:200]
    return None


def qualities(url):
    if not url:
        return []
    player = state["player"] if url == state["url"] else resolve(url)[1]
    if player == "ytdlp":
        return list(YTDL_HEIGHTS)
    return sl_info(url)[0]


def sl_info(url):
    """(quality list, metadata dict) from `streamlink --json`, cached 2 min."""
    cached = _qcache.get(url)
    if cached and time.time() - cached[0] < 120:
        return cached[1], cached[2]
    try:
        try:
            j = inproc.streamlink_json(url)
        except Exception as e:
            app.logger.info("streamlink library failed (%s): using the command", e)
            j = json.loads(subprocess.run(["streamlink", "--json", url], capture_output=True, text=True, timeout=20).stdout)
        streams, meta = list(j.get("streams", {})), j.get("metadata") or {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        streams, meta = [], {}
    order = [s for s in streams if s not in ("worst", "best")][::-1]
    if order:  # don't cache failures
        now = time.time()
        for k in [k for k, v in _qcache.items() if now - v[0] > 600]:
            del _qcache[k]  # the lookup only trusts 2 minutes, so anything older is dead weight
        _qcache[url] = (now, order, meta)
    return order, meta


def display_title(url):
    """Streamlink sources: 'author: title' from metadata, else the channel name (never the raw URL)."""
    meta = (_qcache.get(url) or (0, 0, {}))[2]
    title = meta.get("title") or livewatch.snapshot().get(url or "", {}).get("title")  # its details may be missing or expired: the live check keeps the last title
    return ": ".join(p for p in (meta.get("author"), title) if p) or fav_name(url)


# ---------- favourites ----------

def fav_file(user=None):
    """Each account has its own favourites file. Outside a request (background threads, the TV's home screen) it is the owner's."""
    user = user or (g.user if has_request_context() else (accounts.founder_name() or accounts.OWNER))
    # a guest maps to favourites.guest.json, which never exists (guests can't save): they see an empty list, never anyone's favourites
    return FAV_FILE if user == accounts.OWNER else os.path.join(DATA, f"favourites.{user}.json")


def load_favs(user=None):
    try:
        with open(fav_file(user)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


_files_lock = threading.RLock()  # favourites and recents are load, change, save: two at once must take turns or one change is lost


def under_files_lock(fn):
    @functools.wraps(fn)
    def inner(*a, **k):
        with _files_lock:
            return fn(*a, **k)
    return inner


def save_favs(favs, user=None):
    store.write_json(fav_file(user), favs, indent=2)


def fav_name(url):
    path = urlparse(url).path.strip("/")
    return path.split("/")[0] if path else (urlparse(url).hostname or url)


# ---------- routes ----------

_remote_page = {"mtime": None, "raw": b"", "gz": b"", "etag": ""}


def remote_page():
    """The remote's page, read once (again if the file changes) with a compressed copy and a tag, so a phone gets 45 KB instead of 200 KB
    and, when it already has the current page, a 304 with no body."""
    path = os.path.join(BASE, "index.html")
    m = os.path.getmtime(path)
    if _remote_page["mtime"] != m:
        with open(path, "rb") as f:
            raw = f.read()
        _remote_page.update(mtime=m, raw=raw, gz=gzip.compress(raw, 6), etag='"%x-%x"' % (zlib.crc32(raw), len(raw)))
    return _remote_page


@app.get("/")
@app.get("/share")
def index():
    pg = remote_page()
    head = {"ETag": pg["etag"], "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}  # no-cache: ask every time, and the tag makes that cheap
    if request.headers.get("If-None-Match") == pg["etag"]:
        return Response(status=304, headers=head)
    gz = "gzip" in request.headers.get("Accept-Encoding", "")
    return Response(pg["gz"] if gz else pg["raw"], mimetype="text/html", headers={**head, **({"Content-Encoding": "gzip"} if gz else {})})


@app.get("/logo.svg")
def logo():
    return send_from_directory(BASE, "logo.svg", max_age=86400)


@app.get("/icon-<int:n>.png")
def icon(n):
    if n not in (180, 512):
        abort(404)
    return send_from_directory(BASE, f"icon-{n}.png", max_age=86400)


@app.get("/manifest.json")
def manifest():
    return jsonify(name="TV Remote", short_name="TV Remote", start_url="/", display="standalone",
                   background_color="#0b0e14", theme_color="#0b0e14",
                   icons=[{"src": "/icon-180.png", "sizes": "180x180", "type": "image/png"},
                          {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                          {"src": "/logo.svg", "sizes": "any", "type": "image/svg+xml"}])


@app.get("/play")
def play_get():
    """Used by the iPhone Shortcut: start playing, then show the remote."""
    url = normalise(request.args.get("u") or request.args.get("url"))
    c = tgt()
    if url and guest_link_refused(url):
        url = None  # shown as nothing playing; the guest is not told what is on the home network
    if url:
        try:
            play_async(c, url, request.args.get("q"), request.args.get("via"))
        except Exception as e:  # shown on the remote via /api/status
            if c:
                c.state["error"] = str(e)
    return redirect(f"/?target={c.id if c else 'living'}", code=303)


def switch_input(tid):
    """Wake the Roku TV and select this target's HDMI input (set in the setup wizard, e.g. HDMI2)."""
    ip, hdmi = ROKU_IPS.get(tid), INPUTS.get(tid)
    if not (ip and hdmi):
        app.logger.warning("Input switch skipped for %s: no Roku address and HDMI input are saved for it (setup wizard, TV step).", tid)
        return
    try:
        info = roku.info(ip)
        if info.get("power") != "PowerOn":
            roku.key(ip, "PowerOn")
            time.sleep(6)  # TV needs a few seconds before it accepts an input change
            fetch_roku(tid)  # so the remote stops saying "off" now, not at the next scheduled look
        elif info.get("app_id") == "tvinput." + hdmi.lower():
            return  # already on it: re-selecting relaunches the input
        roku.key(ip, "Input" + hdmi)
        fetch_roku(tid)
    except (roku.RokuError, OSError) as e:
        app.logger.warning("Input switch failed for %s (%s, %s): %s", tid, ip, hdmi, e)  # playback already started: best effort, but say why


RECENT_FILE = os.path.join(DATA, "recents.json")


def load_recents():
    try:
        with open(RECENT_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def remember(url):
    """Keep the last few links that were played, newest first. A guest's plays are not kept (they would show up in everyone else's list)."""
    if has_request_context() and g.user == accounts.GUEST:
        return
    try:
        with _files_lock:
            items = [r for r in load_recents() if r.get("url") != url]
            items.insert(0, {"name": fav_name(url)[:40], "url": url, "source": route(url)[0]})
            store.write_json(RECENT_FILE, items[:8])
    except OSError:
        pass  # history is a convenience


_sleep = {}  # per TV: when to stop (epoch seconds)


@app.route("/api/sleep", methods=["GET", "POST"])
def api_sleep():
    """Stop playing (and switch the TV off) after N minutes. POST {minutes}: 0 cancels."""
    tid = request.args.get("target") or "living"
    if tid not in ("living", "bedroom"):
        return jsonify(ok=False, error="Unknown TV."), 400
    if request.method == "POST":
        try:
            m = int((request.get_json(silent=True) or {}).get("minutes", 0))
        except (TypeError, ValueError):
            return jsonify(ok=False, error="Bad value"), 400
        if not 0 <= m <= 720:
            return jsonify(ok=False, error="Choose up to 12 hours."), 400
        if m:
            _sleep[tid] = time.time() + m * 60
        else:
            _sleep.pop(tid, None)
    return jsonify(ok=True, at=_sleep.get(tid))


def on_sleep(tid):
    """The timer ran out: stop what is playing and turn the TV off."""
    if tid == "living":
        screen("idle")
        with lock:
            stop_player()
            state.update(url=None, source=None, quality=None)
    elif CAST:
        try:
            CAST.cmd("stop")
        except Exception:
            pass
    ip = ROKU_IPS.get(tid)
    if ip:
        try:
            roku.key(ip, "PowerOff")
        except (roku.RokuError, OSError):
            pass


def check_sleep(now=None):
    now = now or time.time()
    for tid in [t for t, at in _sleep.items() if at <= now]:
        _sleep.pop(tid, None)
        on_sleep(tid)


def sleep_loop():
    while True:
        time.sleep(5)
        check_sleep()


@app.get("/api/recents")
def api_recents():
    if g.user == accounts.GUEST:
        return jsonify([])  # what other people played is not for someone on a guest link
    livewatch.poke()
    snap = livewatch.snapshot()
    return jsonify([{**r, "live": e.get("live"), "game": e.get("game"), "title": e.get("title"), "live_for": livewatch.live_for(r.get("url") or ""), "last_live": e.get("last_live")}
                    for r in load_recents() for e in [snap.get(r.get("url"), {})]])


_start_seq = {}  # per TV: the newest start request. A request that is no longer the newest quits without starting.


_pending = {"t": 0.0}  # when a start on the Pi's screen was accepted and has no player yet: the remote shows "Starting" meanwhile


def play_async(c, url, quality, via=None):
    """Start `url` and answer at once. On the Pi the slow part (finding the stream's address, waking the TV) runs in the background, so a tap
    is answered in milliseconds instead of after seconds; the remote shows Starting until the player is up, and a failure shows on the TV and
    through /api/status. A Chromecast start is still waited for (it reports its errors to the caller)."""
    if c:
        return play_on(c, url, quality, via)
    _pending["t"] = time.time()
    who = getattr(g, "user", None) if has_request_context() else None

    def run():
        if who is not None:
            g.user = who  # the copied request context does not carry what the app stored per request
        try:
            play_on(None, url, quality, via)
        except Exception as e:
            app.logger.warning("Starting %s failed: %s", url, e)
            screen("error", str(e)[:160])
        finally:
            _pending["t"] = 0.0  # the player is up (or it failed): "Starting" is over
    threading.Thread(target=copy_current_request_context(run), daemon=True).start()  # keeps who asked: a guest's plays must not be remembered


def play_on(c, url, quality, via=None):
    """Start `url` on the Chromecast target `c`, or on the Pi when c is None. Newest tap wins: an older start that is
    still working out its link (yt-dlp can take ~20 s) gives up instead of running after the newer one."""
    tid = c.id if c else "living"
    cur = c.state if c else state
    if cur.get("url") == url and time.time() - (cur.get("started") or 0) < 3:
        return  # the same link was started a moment ago (a double tap): leave it be
    remember(url)
    was_wall = False
    if not c:
        was_wall = wall.active()
        wall.clear()  # starting something on the Pi's screen is a single stream; more are added from the Multi-view sheet
        if was_wall:
            wall.saved_quality = None  # the new stream starts at its own default, not the old main stream's pick
    # Wake the TV first, in the background: working out the link can fail (a channel that is offline), and a Chromecast that is
    # powered by its TV is not even on the network until the TV is awake.
    threading.Thread(target=switch_input, args=(tid,), daemon=True).start()
    seq = _start_seq[tid] = _start_seq.get(tid, 0) + 1
    newest = lambda: _start_seq.get(tid) == seq
    if c:
        c.play(url, quality, via, cancelled=lambda: not newest())
    else:
        resolve(url)  # the slow part (cached afterwards), done before taking the player lock so a newer tap is never stuck behind it
        if was_wall:  # out of a multi-view: the main window makes itself full screen and loads the new stream in place, instead of every window closing and a new one opening
            if newest():
                wall_relayout(url)  # takes the player lock itself, and starts a player the usual way when it can't load in place
        else:
            with lock:
                if newest():
                    start_player(url, quality)


@app.post("/api/play")
def api_play():
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    url = normalise(data.get("url") or data.get("text") or request.get_data(as_text=True),
                    data.get("site", "kick"))
    if not url:
        return jsonify(ok=False, error="No link found"), 400
    refused = guest_link_refused(url)
    if refused:
        return jsonify(ok=False, error=refused), 403
    c = tgt()  # outside the try: its 503 must reach the client, not be turned into a 502
    try:
        play_async(c, url, data.get("quality"), data.get("via"))
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 502
    return jsonify(ok=True, url=url)


COMMANDS = {
    "pause": lambda v: ["cycle", "pause"],
    "mute": lambda v: ["cycle", "mute"],
    "volume": lambda v: ["set_property", "volume", max(0.0, min(130.0, float(v)))],
    "volume_delta": lambda v: ["add", "volume", float(v)],
    "seek": lambda v: ["seek", float(v), "relative"],
    "seek_to": lambda v: ["seek", max(0.0, float(v)), "absolute"],
}


_vol_saved = {"t": 0.0}


@app.post("/api/cmd")
def api_cmd():
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    c = tgt()
    if action == "stop":  # a start still working out its link must not launch after this
        _start_seq[c.id if c else "living"] = _start_seq.get(c.id if c else "living", 0) + 1
    if c:
        try:
            ok, err = c.cmd(action, data.get("value"), data.get("quality"))
        except Exception as e:
            return jsonify(ok=False, error=str(e)), 502
        return jsonify(ok=ok, error=err), (200 if ok else 400)
    if action == "stop":
        stop_pi()
        return jsonify(ok=True)
    if action == "restart":
        if not state["url"]:
            return jsonify(ok=False, error="Nothing to restart"), 400
        if data.get("quality") and state.get("hot") and state["proc"]:  # changing in place: say so on the TV, with a wheel until it plays
            before = state.get("started")
            osd_swap(stream_label(data["quality"]), ready=lambda: _picture_changed_since(before))
        with lock:
            start_player(state["url"], data.get("quality") or state["quality"], "quality" if data.get("quality") else "refresh")
        return jsonify(ok=True)
    if action == "chat":
        _chat["pref"] = data.get("value") != "off"
        if _chat["pref"]:
            chat_lower_quality()
        else:
            chat_restore_quality()
        chat_sync()
        return jsonify(ok=True)
    fn = COMMANDS.get(action)
    if not fn:
        return jsonify(ok=False, error="Unknown action"), 400
    try:
        cmd = fn(data.get("value", 0))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Bad value"), 400
    on_wall = wall.active() and action in ("volume", "volume_delta", "mute", "unmute")
    if on_wall and action in ("mute", "unmute"):  # the stream the card controls: its sound on or off (the same switch as its speaker)
        wall.set_sound(wall.selected, action == "unmute" or wall.selected not in wall.sound, SOCK)
        osd_volume(sel_info()[3])
        return jsonify(ok=True)
    if on_wall:  # the level of the stream the card controls only: with several heard at once, each has its own
        r = mpvipc.send(sel_info()[3], cmd)
    else:
        sock = sel_info()[3]
        r = mpv(cmd) if sock == SOCK else mpvipc.send(sock, cmd)  # pause, seek...: the stream the card shows
    ok = bool(r and r.get("error") == "success")
    if ok and action == "pause" and sel_info()[3] == SOCK:
        osd_paused(bool(prop("pause")))
    if ok and action in ("volume", "volume_delta") and sel_info()[3] == SOCK and prop("volume") is not None:
        settings["volume"] = prop("volume")
        if data.get("final") or time.time() - _vol_saved["t"] > 2:  # a drag sends many: write the file when it ends, or every few seconds
            _vol_saved["t"] = time.time()
            save_settings()
    if ok and action in ("volume", "volume_delta", "mute", "unmute"):
        osd_volume(sel_info()[3])  # in the window of the stream being controlled
    return jsonify(ok=ok)


def wall_relayout(main_url=None):
    """Bring every window in line with the wall: only a window whose stream or place changed is restarted (a swap redraws two
    streams; adding or removing one moves them all). With one stream left the main player goes back to the full screen at the
    quality it had."""
    url = main_url or (wall.main_url if wall.active() and wall.main_url else state["url"])
    if wall.active():
        wall.start_windows()  # they work out their stream addresses while the main one does
    with lock:
        p = state["proc"]
        if HOT and not wall.active() and p and p.poll() is None and state.get("hot") and state.get("wid"):
            mpvipc.send(SOCK, ["set_property", "fullscreen", True])  # a window can make itself full screen: nothing closes
            mpvipc.send(SOCK, ["set_property", "mute", False])
            state["wid"] = None
            try:
                if hot_load(url, wall.saved_quality):
                    return
            except Exception as e:
                app.logger.info("Not changed in place (%s): restarting full screen", e)
            state["wid"] = "restart"  # makes the normal path below restart it
        if url != state["url"] or state.get("wid") != wall.main_wid() or not (p and p.poll() is None):
            start_player(url, multi.quality_for(resolve(url)[1], wall.main_quality) if wall.active() else wall.saved_quality, "refresh")
    if wall.active():
        screen("playing")  # the names on the background may have changed


wall.on_change = lambda: screen("playing")  # a window ended by itself: redraw the background so its slot says so
_detected = {}  # name -> (when, answer): a streamer's sites are looked up at most once a minute


@app.get("/api/detect")
def api_detect():
    """Which site is this streamer on? Looks the name up on Kick and Twitch together. `pick` is the link to use when it is clear:
    live on only one of them, or known on only one. When it is live on both (or offline on both), the phone asks which."""
    name = (request.args.get("name") or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_\-]{2,25}", name):
        return jsonify(ok=False, error="That doesn't look like a channel name."), 400
    hit = _detected.get(name)
    if hit and time.time() - hit[0] < 60:
        return jsonify(hit[1])
    urls = {"Kick": f"https://kick.com/{name}", "Twitch": f"https://www.twitch.tv/{name}"}
    found = {}
    ts = [threading.Thread(target=lambda s=s: found.__setitem__(s, check_live(urls[s])), daemon=True) for s in urls]
    for t in ts:
        t.start()
    for t in ts:
        t.join(25)
    matches = [{"source": s, "url": urls[s], "live": bool(found[s].get("live")), "title": found[s].get("title"),
                "viewers": found[s].get("viewers")} for s in urls if found.get(s) is not None]
    live = [m for m in matches if m["live"]]
    pick = live[0] if len(live) == 1 else matches[0] if len(matches) == 1 else None
    answer = {"ok": True, "name": name, "matches": matches, "pick": pick["url"] if pick else None}
    for k in [k for k, v in _detected.items() if time.time() - v[0] > 300]:
        del _detected[k]
    _detected[name] = (time.time(), answer)
    return jsonify(answer)


_wall_job = threading.Lock()
_wall_pending = [0]  # changes queued or running: the phone shows a spinner and holds its buttons until it is 0


def wall_later(fn, *args):
    """Run a multi-view change in the background: restarting windows takes seconds, and the phone must not wait for it.
    Jobs run one at a time, and each reads the wall as it is by then, so a quick second tap is not lost."""
    def run():
        with _wall_job:
            try:
                fn(*args)
            except Exception as e:
                app.logger.warning("Multi-view change failed: %s", e)
            finally:
                _wall_pending[0] -= 1
        if _wall_pending[0] == 0:
            warm_addresses()  # quietly, once things have settled
    _wall_pending[0] += 1
    threading.Thread(target=run, daemon=True).start()


def reconcile_job():
    """Make every window play what the wall says. A window that plays a stream address changes stream in place (loadfile: nothing closes or
    opens); one that can't is restarted. All the new addresses are worked out first, then the windows are told at the same moment. Queued
    changes just converge: each run looks at the wall as it is now."""
    try:
        if not wall.active():
            return
        rects, todo = multi.tiles(wall.geo, wall.count()), {}
        if wall.main_url and wall.main_url != state["url"]:
            todo[0] = (wall.main_url, multi.quality_for("streamlink", wall.main_quality) or cap("streamlink", rects[0][2]))
        for i, t in enumerate(wall.extras, start=1):
            if t.get("started") != t["url"] or t.get("q_started") != t.get("quality"):
                todo[i] = (t["url"], multi.quality_for("streamlink", t.get("quality")) or cap("streamlink", rects[i][2]))

        def can(i, url):
            source, player = resolve(url)
            if not (HOT and player == "streamlink" and source != "Twitch"):
                return False
            if i == 0:
                p = state["proc"]
                return bool(p and p.poll() is None and state.get("hot") and state.get("wid") == wall.main_wid())
            t = wall.extras[i - 1]
            return bool(t.get("proc") and t["proc"].poll() is None and t.get("hot") and t.get("wid") == multi.window_id(wall.geo, wall.count(), i))

        addrs, hot = {}, [i for i, (u, q) in todo.items() if can(i, u)]

        def get(i):
            try:
                addrs[i] = stream_url(*todo[i])
            except Exception as e:
                app.logger.warning("No stream address for window %d (%s)", i, e)
        ts = [threading.Thread(target=get, args=(i,)) for i in hot]
        for x in ts:
            x.start()
        for x in ts:
            x.join()
        sent = {}
        go = [threading.Thread(target=lambda i=i: sent.__setitem__(i, mpvipc.send(wall.sock(i, SOCK), ["loadfile", addrs[i], "replace"], 5))) for i in addrs]
        for x in go:
            x.start()  # all at once
        for x in go:
            x.join()
        for i, r in sent.items():
            if not (r and r.get("error") == "success"):
                continue
            url, q = todo[i]
            if i == 0:
                state.update(url=url, source=route(url)[0], player=resolve(url)[1], quality=q, started=time.time(), stalls=0, stall_s=0)
            else:
                wall.extras[i - 1].update(started=url, q_started=wall.extras[i - 1].get("quality"), source=route(url)[0], error=None)
        left = [i for i in todo if i not in sent or not (sent[i] and sent[i].get("error") == "success")]
        if 0 in left:
            wall_relayout(wall.main_url)  # the main window can't change in place: restart it (a no-op if it already plays that)
        if any(i for i in left):
            wall.start_windows()  # restarts only the windows that do not match
        for k in range(wall.count()):  # the windows kept their own mute setting: put it with the streams
            mpvipc.send(wall.sock(k, SOCK), ["set_property", "mute", k not in wall.sound])
        screen("playing")
    except Exception as e:
        app.logger.warning("Changing the windows' streams failed: %s", e)


def tile_retry():
    wall.start_windows()
    screen("playing")


def _restart_main_quality(label):
    if state.get("hot") and state["proc"]:
        before = state.get("started")
        osd_swap(label or "Auto", ready=lambda: _picture_changed_since(before))
    with lock:
        start_player(state["url"], multi.quality_for(resolve(state["url"])[1], label), "quality")


@app.post("/api/wall")
def api_wall():
    """Several streams on the living-room TV: {action: add|remove|layout|sound|select|swap|only|quality|replace|sync|clear, url, site, i, layout, quality}."""
    if tgt():
        return jsonify(ok=False, error="Only the living-room TV can show several streams at once."), 400
    d = request.get_json(silent=True) or {}
    act, i = d.get("action"), d.get("i")
    i = i if isinstance(i, int) and not isinstance(i, bool) else 0
    if act == "add":
        url = normalise(d.get("url"), d.get("site", "kick"))
        if not url:
            return jsonify(ok=False, error="No link found"), 400
        # Once a wall exists, a quick second add must not be refused because the first add is still restarting the main window (for those
        # seconds the player reports "not running"): the wall itself knows what the main stream is.
        if not wall.active() and not (state["url"] and pi_status()["running"]):
            return jsonify(ok=False, error="Start a stream first, then add the others."), 400
        with lock:
            if not wall.active():
                wall.saved_quality = state["quality"]
        if not wall.add(url, state["url"]):
            return jsonify(ok=False, error=f"That one is already on screen, or the screen is full ({multi.MAX_TILES})."), 400
        remember(url)  # a stream added to the screen is a recent one too
        wall_later(wall_relayout)
    elif act == "remove":
        if i == 0:  # the first stream: the next one takes its place
            new_main = wall.drop_main()
            if not new_main:
                return jsonify(ok=False, error="Nothing to remove"), 400
            wall_later(wall_relayout, new_main)
        else:
            wall.remove(i)
            wall_later(wall_relayout)
    elif act == "layout":
        if d.get("layout") not in multi.LAYOUTS:
            return jsonify(ok=False, error="Unknown layout"), 400
        wall.layout = d["layout"]
        if wall.active():
            wall_later(wall_relayout)
    elif act == "sound":  # switch one stream's sound on or off, the others as they are
        wall.set_sound(i, bool(d.get("on")), SOCK)
    elif act == "swap":
        if not wall.swap(i, state["url"]):
            return jsonify(ok=False, error="Nothing to swap"), 400
        wall_later(reconcile_job)
    elif act == "replace":  # a different stream in this window: loaded in place where the window can, else that window alone restarts
        url = normalise(d.get("url"), d.get("site", "kick"))
        if not url:
            return jsonify(ok=False, error="No link found"), 400
        if not wall.replace(i, url):
            return jsonify(ok=False, error="That one is already on the screen."), 400
        remember(url)
        wall_later(reconcile_job)
    elif act == "sync":  # line the streams up in time; answers with what it found
        with _wall_job:  # not while the windows are still changing
            result = wall_sync()
        return jsonify(ok=True, wall=wall_status(), sync=result)
    elif act == "select":  # which stream the card controls: nothing on the screen changes
        wall.select(i)
    elif act == "retry":  # a stream that went offline may be back
        if i == 0:
            state["wid"] = "retry"  # makes the next change restart the main window
            wall_later(wall_relayout)
        else:
            wall.retry(i)
            wall_later(tile_retry)
    elif act == "only":  # back to a single stream, keeping this one (the main stream is tile 0)
        keep = (wall.main_url or state["url"]) if i == 0 else next(iter(t["url"] for k, t in enumerate(wall.extras, start=1) if k == i), None)
        if not keep:
            return jsonify(ok=False, error="No such stream"), 400
        if i:
            wall.saved_quality = None  # the other stream starts at its own default, not the old main stream's pick
        wall.clear()
        wall_later(wall_relayout, keep)
    elif act == "quality":
        label = d.get("quality") if d.get("quality") in multi.QUALITIES else None  # anything else is Auto
        if i == 0:
            wall.main_quality = label
            wall_later(_restart_main_quality, label)
        elif 1 <= i <= len(wall.extras):
            wall.extras[i - 1]["quality"] = label
            osd_swap(label or "Auto", sock=wall.sock(i, SOCK))  # in that window, for a moment
            wall_later(reconcile_job)  # only this window differs: it loads the new quality in place, or restarts alone
        else:
            return jsonify(ok=False, error="No such stream"), 400
    elif act == "clear":
        wall.clear()
        if state["url"]:
            wall_later(wall_relayout)
    else:
        return jsonify(ok=False, error="Unknown action"), 400
    return jsonify(ok=True, wall=wall_status())


def stop_pi():
    wall.clear()  # every window ends with the stream
    screen("idle")  # first: the home screen still shows its last screen (in the old site's colour) until it is redrawn
    with lock:  # waits for a start that is mid-way, then ends it
        stop_player()
        state.update(url=None, source=None, quality=None)  # a deliberate stop leaves no old error or "now playing" behind
    screen("idle")  # again: that start may have drawn its Loading screen after the first Ready


# ---- Move: play on the other TV, stop here, and put a TV that the move woke back to sleep ----
_woken = {}  # tv id -> when a Move switched it on from off. Nothing else adds to this, so a TV you turned on yourself is never switched off
WOKEN_TTL = 12 * 3600


def tv_is_off(tid):
    ip = ROKU_IPS.get(tid)
    if not ip:
        return False  # no Roku, so we can't tell and won't claim to have woken it
    try:
        return roku.info(ip).get("power") != "PowerOn"
    except (roku.RokuError, OSError):
        return False


def tv_still_on_our_input(tid):
    """True when the TV is on and still on the HDMI input this remote put it on: nobody has switched it to a console or an app."""
    ip, hdmi = ROKU_IPS.get(tid), INPUTS.get(tid)
    if not (ip and hdmi):
        return False
    try:
        info = roku.info(ip)
    except (roku.RokuError, OSError):
        return False
    return info.get("power") == "PowerOn" and info.get("app_id") == "tvinput." + hdmi.lower()


def power_back_off(tid):
    """After moving away from `tid`: switch it off again if a Move had woken it and nothing else has used it since."""
    woke = _woken.pop(tid, 0)
    if not woke or time.time() - woke > WOKEN_TTL or not tv_still_on_our_input(tid):
        return False
    try:
        roku.key(ROKU_IPS[tid], "PowerOff")
    except (roku.RokuError, OSError) as e:
        app.logger.warning("Couldn't switch %s back off: %s", tid, e)
        return False
    refresh_roku_soon(tid)
    return True


@app.post("/api/move")
def api_move():
    d = request.get_json(silent=True) or {}
    src, dst, url = d.get("from"), d.get("to"), normalise(d.get("url") or "")
    if src not in tvconfig.IDS or dst not in tvconfig.IDS or src == dst or not url:
        return jsonify(ok=False, error="Pick a different TV to move it to."), 400
    if guest_link_refused(url):
        return jsonify(ok=False, error="Guests can play Kick, Twitch and YouTube links."), 403
    if not TV_ENABLED.get(dst) or not TV_ENABLED.get(src):
        return jsonify(ok=False, error="That TV isn't set up to play on."), 400
    same_tv = bool(TV_OF.get(src) and TV_OF.get(src) == TV_OF.get(dst))  # e.g. the Pi and the Chromecast on one TV
    c_dst = target_for(dst)
    was_off = tv_is_off(dst) and not same_tv
    try:
        play_on(c_dst, url, None)  # start it there first: if that fails, what is playing here is left alone
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 502
    if was_off:
        _woken[dst] = time.time()
    _start_seq[src] = _start_seq.get(src, 0) + 1
    if src == "bedroom":
        c_src = target_for("bedroom")
        try:
            c_src.cmd("stop")
        except Exception as e:
            app.logger.warning("Couldn't stop the Chromecast after a move: %s", e)
    else:
        stop_pi()
    return jsonify(ok=True, same_tv=same_tv, switched_off=False if same_tv else power_back_off(src))  # the shared TV stays on


def paused_screen(title=""):
    """ASS for the pause overlay: the picture dimmed, a pause symbol and "Paused". Overlay 2 (chat uses 1).
    Drawn once and left alone: the picture is frozen while paused, so unlike the chat overlay it costs no frames."""
    parts = [r"{\an7\pos(0,0)\p1\bord0\shad0\1c&H000000&\1a&H99&}m 0 0 l 1920 0 1920 1080 0 1080{\p0}",
             r"{\an7\pos(0,0)\p1\bord0\shad0\1c&HF6F1EE&}m 850 380 l 920 380 920 580 850 580 m 1000 380 l 1070 380 1070 580 1000 580{\p0}",
             r"{\an8\pos(960,620)\fs72\b1\bord0\shad0\1c&HF6F1EE&}Paused"]
    if title:
        parts.append(r"{\an8\pos(960,720)\fs38\b0\bord0\shad0\1c&HA9978C&}" + chat._esc(title))
    return "\n".join(parts)


def osd_paused(paused):
    """Show the pause overlay while paused; clear it on resume."""
    if paused:
        mpv(["osd-overlay", 2, "ass-events", paused_screen(prop("media-title") or ""), 1920, 1080])
    else:
        mpv(["osd-overlay", 2, "none", ""])


def volume_text(vol, muted):
    """Plain text for show-text: a block bar and the number, or Muted. (osd-overlay drawing flashed and lagged the stream.)"""
    v = max(0, min(100, round(vol)))
    return "Muted" if muted else "Volume %d%%  %s%s" % (v, "█" * (v // 5), "░" * (20 - v // 5))


def osd_volume(sock=SOCK):
    """Show the volume on the TV for 1.5 s, in the window whose volume it is (with several streams, each has its own)."""
    if sock == SOCK:
        vol, muted = prop("volume"), prop("mute")
        if vol is not None:
            mpv(["show-text", volume_text(vol, muted), 1500])
        return
    vol, muted = sprop(sock, "volume"), sprop(sock, "mute")
    if vol is not None:
        mpvipc.send(sock, ["show-text", volume_text(vol, muted), 1500])


_transition = {"win": None}


def transition_show(title, line, source):
    """Put the card over the picture (a window of its own, so it can't flash the stream). Waits up to 2 s for the window: True when it is up."""
    win = _transition["win"] = _transition["win"] or chatwin.transition_panel()
    return bool((win.running() or win.open(tries=8)) and win.update(shell.transition_screen(title, line, source)))


def transition_close():
    if _transition["win"]:
        _transition["win"].close()


def osd_swap(label, sock=SOCK, ready=None, max_s=8.0, title=None, source=None, line=None):
    """While a stream or its quality changes in place, cover the picture with a card (the channel, "Switching to 720p" or `line`) until the new picture
    plays, then say "Quality: 720p" for a moment (not for a stream change). `ready()` says the new picture is playing; without it the card stays 2.5 s.
    In a multi-view tile the small on-screen wheel is used instead. Runs in the background, never blocks the change. Returns the thread."""
    if sock == SOCK:
        _swapping.set()  # the stall monitor keeps its Buffering / lagging screens off while the card is up
        try:  # the card goes up first (about a second for its window), so the change happens behind it, not before it
            transition_show(title or fav_name(state["url"] or ""), line or f"Switching to {label}", source or state["source"])
        except Exception as e:
            app.logger.info("Transition card not shown (%s)", e)

        def run_main():
            t0 = time.time()
            try:
                time.sleep(0.8)  # long enough to be read
                while time.time() - t0 < (max_s if ready else 2.5) and not (ready and ready()):
                    time.sleep(0.25)
                transition_close()
                if line is None:
                    mpvipc.send(sock, ["show-text", f"Quality: {label}", 1500])
            finally:
                transition_close()
                _swapping.clear()
        t = threading.Thread(target=run_main, daemon=True)
        t.start()
        return t

    def run():
        frames, t0, i = "◐◓◑◒", time.time(), 0
        try:
            while time.time() - t0 < (max_s if ready else 2.5):
                r = mpvipc.send(sock, ["show-text", f"{frames[i % 4]}  Switching to {label}", 900])
                if r is None:
                    return  # no window to draw on
                i += 1
                time.sleep(0.4)
                if ready and time.time() - t0 > 0.6 and ready():
                    break
            mpvipc.send(sock, ["show-text", f"Quality: {label}", 1500])
        finally:
            if sock == SOCK:
                _swapping.clear()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _picture_changed_since(started):
    """True once the player has started the new stream (a change in place stamps `started`) and is actually playing it."""
    return state.get("started") != started and prop("core-idle") is False and prop("time-pos") is not None


PROPS = {
    "title": "media-title", "paused": "pause", "volume": "volume", "muted": "mute",
    "position": "time-pos", "duration": "duration", "height": "height",
    "hwdec": "hwdec-current", "drops": "frame-drop-count", "buffering": "paused-for-cache",
}


@app.get("/api/status")
def api_status():
    if request.args.get("target") in TV_ONLY:
        return jsonify(running=False, starting=False, tvonly=True, target=request.args["target"], error=None, url=None, sleep_at=_sleep.get(request.args["target"]))
    c = tgt()
    if c:
        try:
            d = c.status()
        except Exception as e:
            d = dict(running=False, target=c.id, error=str(e))
        tid = c.id
    else:
        d, tid = pi_status(), "living"
    if not d.get("running") and not d.get("starting") and not d.get("error"):
        d["external"] = external_now(tid)
    return jsonify({**d, "sleep_at": _sleep.get(tid)})


PHONE_QUALITIES = (360, 480, 720, 1080)


def phone_settings():
    """App settings an admin can change from Settings (kept in settings.json). Phone viewing: the biggest picture the phone is sent, and
    whether a guest may use it. More of these (and per-role permissions) can join this one place."""
    q = settings.get("phone_quality", 720)
    return {"phone_quality": q if q in PHONE_QUALITIES else 720, "phone_guests": settings.get("phone_guests", True) is not False,
            "qr": settings.get("qr", True) is not False}


@app.get("/api/config")
def api_config():
    admin_only()
    return jsonify(phone_settings())


@app.post("/api/config")
def api_config_set():
    admin_only()
    body = request.get_json(silent=True) or {}
    if "phone_quality" in body:
        if body["phone_quality"] not in PHONE_QUALITIES:
            return jsonify(ok=False, error="Pick 360, 480, 720 or 1080."), 400
        settings["phone_quality"] = body["phone_quality"]
    for k in ("phone_guests", "qr"):
        if k in body:
            settings[k] = bool(body[k])
    save_settings()
    if HOME and HOME.kind == "idle":
        HOME.show("idle")  # the QR code may have been switched
    return jsonify(ok=True, **phone_settings())


@app.get("/api/watch")
def api_watch():
    """The address of what this TV is playing, so the phone can play it itself (home network only: the address is the stream's own).
    Twitch is left out: its ad filter only works inside the Pi's player. Starts at the TV's position for a video."""
    cfg = phone_settings()
    if g.user == accounts.GUEST and not cfg["phone_guests"]:
        return jsonify(ok=False, error="Watching on the phone is switched off for guests."), 403
    c = tgt()
    idx = 0
    if c:
        url = c.state.get("url")
    else:  # the Pi: with several streams on its screen, ?i= picks one (default: the one being controlled); 0 is the main window
        urls = [wall.main_url or state.get("url"), *[t["url"] for t in wall.extras]] if wall.active() else [state.get("url")]
        want = request.args.get("i", type=int)
        idx = want if want is not None else (wall.selected if wall.active() else 0)
        url = urls[idx] if 0 <= idx < len(urls) else None
    if not url:
        return jsonify(ok=False, error="Nothing is playing on this TV."), 409
    h = cfg["phone_quality"]
    try:
        source, player = resolve(url)
        if source == "Twitch":
            return jsonify(ok=False, error="Twitch streams can't play on the phone yet (the ads can't be filtered out there)."), 422
        if player == "streamlink":
            addr, kind = stream_url(url, f"{h}p60,{h}p,480p,best"), "hls"
        else:
            fmt = f"b[vcodec^=avc1][height<={h}][ext=mp4]/b[ext=mp4][height<={h}]/b[ext=mp4]"
            p = subprocess.run(["yt-dlp", "--no-playlist", "-g", "-f", fmt, url], capture_output=True, text=True, timeout=45)
            lines = p.stdout.strip().splitlines()
            if p.returncode or not lines:
                raise RuntimeError((p.stderr.strip().splitlines() or ["no video address"])[-1][:200])
            addr, kind = lines[0], "mp4"
        pos = (c.status() or {}).get("position") if c else sprop(wall.sock(idx, SOCK) if idx else SOCK, "time-pos")
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        return jsonify(ok=False, error=f"Couldn't get the stream for the phone: {e}"), 502
    return jsonify(ok=True, url=addr, kind=kind, live=kind == "hls", quality=h, title=fav_name(url), i=idx,
                   start=pos if isinstance(pos, (int, float)) and kind == "mp4" else 0)


def pi_status():
    """Status of the stream the remote controls (the main player, or the selected one while several are on the screen); also feeds the living-room card."""
    url, source, player, sock = sel_info()
    P = prop if sock == SOCK else (lambda n: sprop(sock, n))  # the main player, or the selected window
    running = P("pid") is not None
    data = {"running": running, "url": url, "quality": state["quality"] if sock == SOCK else None, "source": source,
            "streamlink": player == "streamlink",
            "wall": wall_status()}  # also while the main window restarts, or the multi-view would vanish for those seconds
    t = pihealth.temperature()
    if pihealth.level(t) in ("warm", "hot"):  # the phone says so: a hot Pi drops frames and, when very hot, has its pictures lowered
        data["heat"] = {"temp": t, "level": pihealth.level(t), "lowered": heat["on"]}
    if running:
        for key, name in PROPS.items():
            data[key] = P(name)
        facts = livewatch.snapshot().get(url or "", {})
        if player == "streamlink":
            warm_title(url)
            data["title"] = display_title(url)
        data["stalls"], data["stall_s"] = state.get("stalls", 0), round(state.get("stall_s", 0))
        data["game"], data["live_for"] = facts.get("game"), livewatch.live_for(url or "")
        data["chat_ok"] = source in ("Twitch", "Kick") and bool(chat.channel_of(url or ""))
        data["chat"] = bool(_chat["obj"])
        if wall.active():  # the mute button is the controlled stream's sound switch
            data["muted"] = wall.selected not in wall.sound
        # live = streamlink, or a stream with no duration
        data["live"] = player == "streamlink" or data["duration"] is None
    else:
        p = state["proc"]
        data["starting"] = bool(p and p.poll() is None) or time.time() - _pending["t"] < 40
        if wall.active() and wall.selected:  # a window of the multi-view that ended by itself
            err_ = wall.extras[wall.selected - 1].get("error")
            if err_:
                data["error"] = err_
        elif state["url"] and not data["starting"]:
            data["error"] = describe_error(last_error())
    return data


_tv = {"t": 0, "v": None}


def tv_cec():
    """TV name and power state over CEC (cached 30s; CEC is slow and shared)."""
    if time.time() - _tv["t"] > 30:
        try:
            out = subprocess.run(["cec-ctl", "-d", "/dev/cec0", "--to", "0", "--give-osd-name",
                                  "--give-device-power-status"], capture_output=True, text=True, timeout=8).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        name = re.search(r"name: (.+)", out)
        pwr = re.search(r"pwr-state: (\w+)", out)
        _tv.update(t=time.time(), v={"name": name.group(1).strip() if name else None,
                                     "power": pwr.group(1) if pwr else None})
    return _tv["v"]


@app.get("/api/tv")
def api_tv():
    return jsonify(tv_cec())


@app.get("/api/qualities")
def api_qualities():
    c = tgt()
    if c:
        return jsonify(c.qualities(request.args.get("url") or c.state["url"], qualities))
    return jsonify(qualities(request.args.get("url") or state["url"]))


ROKU_IPS = {"living": "", "bedroom": ""}  # the Rokus' addresses: filled from /data/targets.json (the setup wizard) at startup


def from_chromecast():
    """Defense in depth next to the token: the request must also come from the Chromecast's address."""
    d = CAST.device() if CAST else None
    return bool(d) and request.remote_addr == d.host


@app.get("/cast/<token>/live.m3u8")
def cast_playlist(token):
    """Relay playlist for the Chromecast (see hlsproxy.py). Unauthenticated (the Chromecast can't send a password) but guarded by a random per-cast token in the path."""
    if not (hlsproxy.valid(token) and from_chromecast()):
        abort(404)
    try:
        body = hlsproxy.playlist()
    except Exception:
        return Response("upstream unavailable", 502)
    return Response(body, mimetype="application/x-mpegURL",
                    headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"})


@app.get("/cast/<token>/seg")
def cast_segment(token):
    if not (hlsproxy.valid(token) and from_chromecast()):
        abort(404)
    data = hlsproxy.segment(request.args.get("u", ""))
    if not data:
        abort(404)
    return Response(data, mimetype="video/mp2t", headers={"Access-Control-Allow-Origin": "*"})


ORDER = ["living", "bedroom"]
LABELS = {"living": "Living room", "bedroom": "Bedroom"}
KINDS = {"living": ("pi", "Pi on HDMI"), "bedroom": ("chromecast", "Chromecast")}  # TVs with nothing plugged in are added here as ("tv", ...)
TV_LABEL = {}  # playback endpoint (or TV-only id) -> the name of its TV, without the " · Pi" / " · Chromecast" suffix
TV_OF = {}  # playback endpoint (or TV-only id) -> the id of the TV it is on, so two endpoints on one TV can tell
TV_ONLY = {}  # id -> name of each TV that has neither the Pi nor a Chromecast: Roku controls only, nothing to play on


def stream_label(quality):
    """'1080p60,best' -> '1080p60'; '720' -> '720p'."""
    first = (quality or "").split(",")[0]
    return (first if "p" in first else first + "p") if first else None


_roku = {}       # tv_id -> (fetched at, info)
_roku_busy = set()
ROKU_TTL = 15       # seconds between asking a TV nobody is looking at (standby detection still needs it)
ROKU_TTL_LIVE = 3   # ...and while a phone has the remote open, so a TV switching on or off shows within a few seconds
_viewed = {"t": 0.0}  # when a phone last asked for the TVs


def roku_ttl():
    return ROKU_TTL_LIVE if time.time() - _viewed["t"] < 30 else ROKU_TTL


def refresh_roku_soon(tv_id):
    """After a power or input key: ask the TV again a few times as it settles, so the page catches the change quickly."""
    def run():
        for wait in (1, 2, 3, 4):  # 1, 3, 6 and 10 seconds after the key
            time.sleep(wait)
            fetch_roku(tv_id)
    threading.Thread(target=run, daemon=True).start()


def on_tv_off(tv_id):
    """The TV went to standby: stop a live stream on the Pi (a paused live stream falls behind and can't resume cleanly),
    pause anything else, including the Chromecast."""
    try:
        if tv_id == "living":
            was_wall = wall.active()
            wall.clear()  # every window ends with the TV: a stale wall would come back around the next stream
            if not pi_playing():
                return
            if pi_status().get("live") or was_wall:
                screen("idle")
                with lock:
                    stop_player()
                    state.update(url=None, source=None, quality=None)
            elif prop("pause") is not True:
                mpv(["set_property", "pause", True])
                osd_paused(True)
        elif tv_id == "bedroom" and CAST:
            st = CAST.status()
            if st.get("running") and not st.get("paused"):
                CAST.cmd("pause")
    except Exception:
        pass  # best effort: the TV is off either way


def fetch_roku(tv_id):
    was_on = (_roku.get(tv_id, (0, {}))[1] or {}).get("power") == "PowerOn"
    try:
        info = roku.info(ROKU_IPS[tv_id])
        if external_app(info):  # an app on the TV itself (YouTube, Netflix...): ask what its player is doing. Read-only.
            try:
                info["media"] = roku.media(ROKU_IPS[tv_id])
            except (roku.RokuError, OSError):
                pass
        if info.get("power") != "PowerOn":
            _woken.pop(tv_id, None)
        if was_on and info.get("power") != "PowerOn":  # went to standby (the TV's own remote, or the app)
            threading.Thread(target=on_tv_off, args=(tv_id,), daemon=True).start()
    except (roku.RokuError, OSError):
        info = _roku.get(tv_id, (0, {}))[1]  # a TV that won't talk ECP keeps its last details
    _roku[tv_id] = (time.time(), info)
    _roku_busy.discard(tv_id)


ROKU_OWN_SCREENS = ("roku", "home", "roku dynamic menu")  # the TV's own home screen and menu: nothing is playing there


def external_app(r):
    """The app the TV itself is running (YouTube, Netflix...), or None when it is on an HDMI input, the home screen, or off."""
    app, app_id = (r or {}).get("app"), (r or {}).get("app_id") or ""
    if (r or {}).get("power") != "PowerOn" or not app or app_id.startswith("tvinput.") or app.strip().lower() in ROKU_OWN_SCREENS:
        return None
    return app


def external_now(tv_id):
    """What the TV is playing on its own, from the cached Roku details (never waits on the TV), or None."""
    r = roku_details(tv_id)
    app = external_app(r)
    if not app:
        return None
    state = (r.get("media") or {}).get("state")
    # only an app whose player is really open can be paused: a menu, a browse screen or a closed player reports "close" (or nothing)
    return {"app": app, "state": state, "paused": state == "pause", "controllable": state in ("play", "pause", "buffer"),
            "duration": (r.get("media") or {}).get("duration")}


def roku_details(tv_id):
    """Cached TV details. A stale or missing entry is refreshed in the background, so a card never waits on a TV."""
    if not ROKU_IPS.get(tv_id):
        return {}
    fetched, info = _roku.get(tv_id, (0, {}))
    if time.time() - fetched > roku_ttl() and tv_id not in _roku_busy:
        _roku_busy.add(tv_id)
        threading.Thread(target=fetch_roku, args=(tv_id,), daemon=True).start()
    return info


def make_card(tv_id, status):
    kind, connection = KINDS.get(tv_id, ("tv", "Roku TV"))
    r = roku_details(tv_id)
    busy = status.get("running") or status.get("starting")
    now = {"title": status.get("title") or fav_name(status.get("url") or ""), "source": status.get("source"),
           "live": bool(status.get("live")), "paused": bool(status.get("paused")),
           "starting": bool(status.get("starting")), "position": status.get("position"),
           "game": livewatch.snapshot().get(status.get("url") or "", {}).get("game"),
           "live_for": livewatch.live_for(status.get("url") or ""),
           "stream": stream_label(status.get("quality")),
           "streams": [{"name": t["name"], "url": t["url"], "source": t["source"], "live": t.get("live"), "off": bool(t.get("error")),
                        "game": t.get("game"), "live_for": t.get("live_for"), "sound": t["i"] in status["wall"]["sound"]}
                       for t in [status["wall"]["main"], *status["wall"]["tiles"]]] if status.get("wall") else None} if busy else None
    return {"id": tv_id, "name": LABELS[tv_id], "kind": kind, "connection": connection, "tv": TV_OF.get(tv_id), "tv_label": TV_LABEL.get(tv_id), "hdmi": INPUTS.get(tv_id) or None,
            "external": None if busy or status.get("error") else external_now(tv_id),  # playing on the TV itself, not through this remote
            "brand": r.get("brand"), "model": r.get("model"), "size": r.get("size"), "tv_name": r.get("name"),
            "power": r.get("power"), "input": r.get("app"), "input_id": r.get("app_id"), "now": now, "error": status.get("error"),
            "health": targets.health(kind, status)}


def living_card():
    return make_card("living", pi_status())


def bedroom_card():
    if not CAST or not CAST.info()["online"]:
        raise OSError("Chromecast not found")
    card = make_card("bedroom", CAST.status())
    card["device"] = CAST.info()["device"]
    return card


def tv_only_card(tv_id):
    return make_card(tv_id, {})


cards = targets.Cards({"living": living_card, "bedroom": bedroom_card}, ORDER)


SHELL_SHOT = "/tmp/tvremote-shell.png"


def shell_state():
    running = bool(HOME and HOME.running())
    return {"enabled": bool(settings.get("shell")), "running": running, "qr": settings.get("qr", True) is not False,
            "vo": HOME.vo if HOME else None, "error": HOME.error if HOME else None,
            "props": HOME.props() if running and hasattr(HOME, "props") else None}  # what the window is doing, for debugging


SCREEN_SHOT = "/tmp/tvremote-screen.png"


@app.get("/api/screen.png")
def api_screen():
    """The Pi's real screen, as the desktop is showing it right now (for debugging what the TV displays)."""
    admin_only()
    try:
        subprocess.run(["grim", "-o", os.environ.get("TVREMOTE_OUTPUT", "HDMI-A-1"), SCREEN_SHOT],
                       check=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return jsonify(ok=False, error="Couldn't capture the screen (is grim installed, and is the desktop running?)."), 502
    return send_file(SCREEN_SHOT, mimetype="image/png", max_age=0)


@app.get("/api/stats")
def api_stats():
    return jsonify(enabled=bool(settings.get("stats")))


@app.post("/api/stats")
def api_stats_set():
    admin_only()
    settings["stats"] = bool((request.get_json(silent=True) or {}).get("enabled"))
    save_settings()
    return jsonify(enabled=settings["stats"])


_feeds = {}  # Twitch channel -> its chat feed: with several streams on the Pi's screen there is one per stream


@app.get("/api/chat/feed")
def api_chat_feed():
    """New chat messages for the phone's chat view (Twitch only). `since` is the last message id the phone has; `i` is the tile
    on the Pi's multi-view screen (0 = the main stream)."""
    c = tgt()
    if c:
        url, source = c.state.get("url"), c.state.get("source")
    else:
        i = request.args.get("i", type=int) or 0
        url = state["url"] if i == 0 else next(iter(t["url"] for k, t in enumerate(wall.extras, start=1) if k == i), None)
        url = url if pi_playing() else None
        source = state["source"] if i == 0 else route(url or "")[0]
    ch = chat.channel_of(url or "") if source == "Twitch" else None
    if not ch:
        return jsonify(ok=False, channel=None, msgs=[], last=0)
    for k in [k for k, f in _feeds.items() if f.stopped.is_set()]:
        del _feeds[k]  # a feed stops itself when nobody has asked for a minute
    f = _feeds.get(ch)
    if not f:
        f = _feeds[ch] = chat.ChatFeed(ch)
    last = request.args.get("since", type=int) or 0
    reset = last > f.n  # the feed was started afresh since the phone last asked
    return jsonify(ok=True, channel=ch, msgs=f.since(0 if reset else last), last=f.n, reset=reset)


@app.get("/api/shell/screenshot")
def api_shell_screenshot():
    """What the home screen window is drawing right now, as a PNG (for debugging)."""
    admin_only()
    r = mpvipc.send(shell.SOCK, ["screenshot-to-file", SHELL_SHOT, "window"], 4)
    if not (r and r.get("error") == "success"):
        return jsonify(ok=False, error="The home screen isn't running, or couldn't take a screenshot."), 502
    return send_file(SHELL_SHOT, mimetype="image/png", max_age=0)


@app.get("/api/shell")
def api_shell():
    return jsonify(shell_state())


@app.post("/api/shell")
def api_shell_set():
    """Turn the TV's home screen on or off (off shows the Pi desktop again). The app and this remote keep running."""
    admin_only()  # the switch lives in the admin part of Settings; it also stops what is playing
    body = request.get_json(silent=True) or {}
    if "qr" in body:  # the QR code on the Ready screen: redraw it now if that screen is showing
        settings["qr"] = bool(body["qr"])
        save_settings()
        if HOME and HOME.kind == "idle":
            HOME.show("idle")
        if "enabled" not in body:
            return jsonify(shell_state())
    settings["shell"] = bool(body.get("enabled"))
    save_settings()
    if HOME:
        if settings["shell"]:
            HOME.start()
            if state["proc"] and state["url"]:  # the new window opens on top of a playing stream, so bring the stream back up
                with lock:
                    start_player(state["url"], state["quality"])
        else:
            HOME.stop()
    return jsonify(shell_state())


@app.post("/api/targets/refresh")
def api_targets_refresh():
    """Ask every TV again right now (the cards otherwise refresh in the background every few seconds, Roku details every 15 s)."""
    ts = [threading.Thread(target=fetch_roku, args=(t,), daemon=True) for t in ROKU_IPS if ROKU_IPS.get(t)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(6)
    cards.refresh_now()
    return api_targets()


@app.get("/api/targets")
def api_targets():
    """One card per TV from the background cache; never waits on a TV. type/online/roku are kept for the old UI."""
    _viewed["t"] = time.time()
    out = []
    for c in cards.snapshot():
        if not TV_ENABLED.get(c["id"], True):
            continue  # switched off in setup (for instance no screen on the Pi)
        kind, connection = KINDS.get(c["id"], ("tv", "Roku TV"))
        out.append({"name": LABELS[c["id"]], "kind": kind, "connection": connection, **c,
                    "type": kind, "online": c["reachable"], "roku": bool(ROKU_IPS.get(c["id"]))})
    return jsonify(out)


INPUTS = {"living": "", "bedroom": ""}  # the HDMI input each TV switches to when you play on it
TV_ENABLED = {"living": True, "bedroom": True}


def apply_tv_config(tvs):
    """Put the saved TVs to use: the Pi's and the Chromecast's Roku, input and name, a card for every TV with nothing plugged in, and
    which endpoints show. Takes effect at once, no restart."""
    ep = tvconfig.endpoints(tvs)
    TV_OF.clear()
    TV_LABEL.clear()
    TV_OF.update({"living": next((t["id"] for t in tvs if t.get("pi")), None), "bedroom": next((t["id"] for t in tvs if t.get("chromecast")), None),
                  **{t["id"]: t["id"] for t in tvs}})
    TV_LABEL.update({t["id"]: t["name"] for t in tvs})
    TV_LABEL.update({e: TV_LABEL.get(TV_OF.get(e)) for e in tvconfig.IDS})
    for tid in tvconfig.IDS:
        ROKU_IPS[tid], INPUTS[tid] = ep[tid]["roku"], ep[tid]["input"]
        TV_ENABLED[tid], LABELS[tid] = ep[tid]["enabled"], ep[tid]["label"]
    for tid in list(TV_ONLY):  # TVs that were on their own before
        for d in (ROKU_IPS, INPUTS, LABELS, TV_ENABLED, KINDS):
            d.pop(tid, None)
    TV_ONLY.clear()
    for t in tvs:
        if not t.get("pi") and not t.get("chromecast") and t.get("roku"):
            TV_ONLY[t["id"]] = t["name"]
            ROKU_IPS[t["id"]], INPUTS[t["id"]], LABELS[t["id"]], TV_ENABLED[t["id"]] = t["roku"], "", t["name"], True
            KINDS[t["id"]] = ("tv", "Roku TV")
    ORDER[:] = [*tvconfig.IDS, *TV_ONLY]
    providers = {"living": living_card, "bedroom": bedroom_card, **{tid: (lambda tid=tid: tv_only_card(tid)) for tid in TV_ONLY}}
    cards.set(providers, ORDER)
    if CAST and ep["bedroom"]["enabled"]:
        CAST.cfg.update(name=LABELS["bedroom"], device=ep["bedroom"]["device"], uuid=ep["bedroom"].get("uuid"))
        CAST.on_uuid = remember_cast_uuid
        CAST.name = LABELS["bedroom"]
    _roku.clear()  # details cached for the old addresses
    _apps.clear()
    _icons.clear()


def remember_cast_uuid(uid):
    """The saved Chromecast was found by name: keep its id too, so renaming it later does not lose it."""
    tvs = tvconfig.load_tvs(DATA)
    for t in tvs:
        if t.get("chromecast") and not t["chromecast"].get("uuid"):
            t["chromecast"]["uuid"] = uid
            tvconfig.save_tvs(DATA, tvs)
            return


def init_tvs(data_dir):
    """At startup: load the saved TVs (converting the old two-slot file the first time, which is kept) and put them to use."""
    tvs = tvconfig.load_tvs(data_dir)
    if tvs and not os.path.exists(os.path.join(data_dir, "tvs.json")):
        tvconfig.save_tvs(data_dir, tvs)
    apply_tv_config(tvs)


@app.get("/api/discover")
def api_discover():
    """Look for TVs: Rokus (network search), Chromecasts (already found), the Pi's own screen. Takes a few seconds. Returns a card per TV
    with a first guess of what is plugged into it (and why); saved TVs keep what was saved."""
    admin_only()
    saved = tvconfig.load_tvs(DATA)
    ips = discover.roku_scan()
    ips += [t["roku"] for t in saved if t.get("roku") and t["roku"] not in ips]
    rokus = discover.describe(ips, roku.info)
    casts = [{"name": d.friendly_name, "model": d.model_name, "host": str(d.host), "uuid": str(d.uuid)}
             for d in (list(CAST.devices.values()) if CAST else []) if d.cast_type == "cast"]
    display = discover.display_connected()
    cec = tv_cec() if display else None
    _phys["t"] = 0  # the Pi may have just been moved: read the address fresh
    pi_input = discover.phys_input(cec_phys()) if display else ""
    return jsonify(rokus=rokus, chromecasts=casts, display=display, cec_name=(cec or {}).get("name"), pi_input=pi_input,
                   tvs=discover.suggest(rokus, casts, display, (cec or {}).get("name"), pi_input, saved))


@app.get("/api/tvs")
def api_tvs():
    admin_only()
    return jsonify(tvs=tvconfig.load_tvs(DATA))


@app.post("/api/tvs")
def api_tvs_set():
    """Save the TVs and put them to use now. Nothing that is playing is touched."""
    admin_only()
    try:
        tvs = tvconfig.clean_tvs(request.get_json(silent=True) or {})
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    apply_tv_config(tvconfig.save_tvs(DATA, tvs))
    return jsonify(ok=True, tvs=tvs)


@app.post("/api/targets/test")
def api_targets_test():
    """Prove a Roku can be controlled: press Info (it shows its information banner and changes nothing)."""
    admin_only()
    ip = tvconfig.lan_address((request.get_json(silent=True) or {}).get("roku"))
    if not ip:
        return jsonify(ok=False, error="That isn't a TV address on your home network."), 400
    try:
        roku.key(ip, "Info")
    except roku.RokuError as e:
        return jsonify(ok=False, error=str(e)), 502
    return jsonify(ok=True)


def roku_ip():
    return ROKU_IPS.get(request.args.get("target") or "living")  # None for a TV we don't know


@app.get("/api/roku")
def api_roku():
    ip = roku_ip()
    if not ip:
        return jsonify(ok=False, error="No Roku configured for this TV"), 404
    try:
        return jsonify(roku.info(ip))
    except roku.RokuError as e:
        return jsonify(ok=False, error=str(e)), 502


@app.post("/api/tv/wake")
def api_tv_wake():
    """Turn the TV on and put it on this target's input (the same thing starting a stream does). Returns at once."""
    tid = request.args.get("target") or "living"
    if tid not in ROKU_IPS and tid != "living":
        return jsonify(ok=False, error="Unknown TV."), 400

    def go():
        ip = ROKU_IPS.get(tid)
        if ip and INPUTS.get(tid):
            switch_input(tid)
            return
        if ip:  # a Roku but no input to pick: just turn it on (this used to do nothing for the bedroom)
            try:
                if roku.info(ip).get("power") != "PowerOn":
                    roku.key(ip, "PowerOn")
            except (roku.RokuError, OSError) as e:
                app.logger.warning("Turn on failed for %s (%s): %s", tid, ip, e)
        if tid == "living":
            switch_tv_to_pi()  # the Pi's TV is also woken and selected over HDMI-CEC
    threading.Thread(target=go, daemon=True).start()
    refresh_roku_soon(tid)
    return jsonify(ok=True)


@app.post("/api/roku/key")
def api_roku_key():
    ip = roku_ip()
    if not ip:
        return jsonify(ok=False, error="No Roku configured for this TV"), 404
    body = request.get_json(silent=True) or {}
    k = body.get("key")
    if k not in roku.KEYS:
        return jsonify(ok=False, error="Unknown key"), 400
    n = body.get("count") if k in ("VolumeUp", "VolumeDown") and isinstance(body.get("count"), int) else 1  # the volume slider presses it a few times
    try:
        for press in range(max(1, min(n, 15))):
            if press:
                time.sleep(0.1)
            roku.key(ip, k)
    except roku.RokuError as e:
        return jsonify(ok=False, error=str(e)), 502
    tid = request.args.get("target") or "living"
    if k == "PowerOff":
        _woken.pop(tid, None)
        threading.Thread(target=on_tv_off, args=(tid,), daemon=True).start()
    if k in ("PowerOn", "PowerOff") or k.startswith("Input"):
        refresh_roku_soon(tid)
    return jsonify(ok=True)


_apps = {}  # tv id -> (fetched at, [apps]): the installed apps change rarely
_icons = {}  # (tv id, app id) -> (bytes, content type)


def tv_apps(tid, ip):
    got = _apps.get(tid)
    if got and time.time() - got[0] < 600:
        return got[1]
    items = roku.apps(ip)
    _apps[tid] = (time.time(), items)
    return items


@app.get("/api/roku/apps")
def api_roku_apps():
    ip = roku_ip()
    if not ip:
        return jsonify(ok=False, error="No Roku configured for this TV"), 404
    try:
        return jsonify(tv_apps(request.args.get("target") or "living", ip))
    except roku.RokuError as e:
        return jsonify(ok=False, error=str(e)), 502


PNG_SIGNATURE = bytes([137, 80, 78, 71, 13, 10, 26, 10])
JPEG_SIGNATURE = bytes([255, 216, 255])


def raster_type(data):
    """The image type judged by the file's own first bytes (png, jpeg, gif, webp), else None. A type named by another device is not trusted:
    an SVG could carry a script that would run as part of this page."""
    if data[:8] == PNG_SIGNATURE:
        return "image/png"
    if data[:3] == JPEG_SIGNATURE:
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@app.get("/api/roku/icon")
def api_roku_icon():
    tid, ip, aid = request.args.get("target") or "living", roku_ip(), request.args.get("id", "")
    if not ip:
        abort(404)
    try:
        if aid not in {a["id"] for a in tv_apps(tid, ip)}:  # only apps that are really installed
            abort(404)
        if (tid, aid) not in _icons:
            if len(_icons) > 80:
                _icons.clear()
            _icons[(tid, aid)] = roku.icon(ip, aid)
    except roku.RokuError:
        abort(404)
    data, _ = _icons[(tid, aid)]
    kind = raster_type(data)
    if not kind:  # whatever the "TV" sent is not a picture we know: never pass it on, whatever type it claims
        abort(404)
    return Response(data, mimetype=kind, headers={"Cache-Control": "max-age=86400", "X-Content-Type-Options": "nosniff",
                                                  "Content-Security-Policy": "sandbox; default-src 'none'"})


@app.post("/api/roku/launch")
def api_roku_launch():
    tid, ip = request.args.get("target") or "living", roku_ip()
    if not ip:
        return jsonify(ok=False, error="No Roku configured for this TV"), 404
    aid = str((request.get_json(silent=True) or {}).get("id", ""))
    try:
        if aid not in {a["id"] for a in tv_apps(tid, ip)}:
            return jsonify(ok=False, error="That app isn't installed on this TV."), 400
        if tv_is_off(tid):  # a TV in standby needs waking first, then a few seconds before it takes the app
            roku.key(ip, "PowerOn")

            def later():
                time.sleep(6)
                try:
                    roku.launch(ip, aid)
                except (roku.RokuError, OSError) as e:
                    app.logger.warning("Launching app %s on %s failed: %s", aid, tid, e)
            threading.Thread(target=later, daemon=True).start()
        else:
            roku.launch(ip, aid)
    except roku.RokuError as e:
        return jsonify(ok=False, error=str(e)), 502
    refresh_roku_soon(tid)
    return jsonify(ok=True)


@app.get("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])
    try:
        try:
            entries = inproc.youtube_search(q).get("entries") or []
        except Exception as e:
            app.logger.info("yt-dlp library failed (%s): using the command", e)
            out = subprocess.run(["yt-dlp", f"ytsearch10:{q}", "--flat-playlist", "-J"],
                                 capture_output=True, text=True, timeout=30).stdout
            entries = json.loads(out).get("entries", [])
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return jsonify(ok=False, error="Search failed"), 502
    return jsonify([{
        "title": e.get("title"), "channel": e.get("channel") or e.get("uploader"),
        "duration": e.get("duration"), "live": e.get("live_status") == "is_live",
        "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}",
        "thumb": f"https://i.ytimg.com/vi/{e.get('id')}/mqdefault.jpg",
    } for e in entries if e.get("id")])


DEBUG_PROPS = ["hwdec-current", "video-codec", "width", "height", "container-fps", "estimated-vf-fps",
               "frame-drop-count", "decoder-frame-drop-count", "vo-delayed-frame-count", "avsync",
               "audio-codec-name", "demuxer-cache-duration", "cache-speed", "current-vo", "current-ao", "pid"]
_versions = {}


def versions():
    if not _versions:
        for name, cmd in {"mpv": ["mpv", "--version"], "streamlink": ["streamlink", "--version"],
                          "yt-dlp": ["yt-dlp", "--version"], "deno": ["deno", "--version"]}.items():
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
                _versions[name] = out.strip().splitlines()[0] if out.strip() else "?"
            except (OSError, subprocess.TimeoutExpired):
                _versions[name] = "missing"
    return _versions


heat = {"on": False, "saved": None, "restored": False}  # the heat guard: lowers multi-view pictures while the Pi is very hot


def heat_check(temp):
    """Above pihealth.HOT: put every multi-view window at 480p (remembering the picks) and say so on the TV; once cooled below
    pihealth.COOL, give the picks back. If it overheats again after that, they stay lowered until a person raises them."""
    if temp is None:
        return
    if temp >= pihealth.HOT and not heat["on"]:
        heat["on"] = True
        if wall.active() and not heat["restored"]:
            heat["saved"] = (wall.main_quality, [e.get("quality") for e in wall.extras])
        if wall.active():
            wall.main_quality = "480"
            for e in wall.extras:
                e["quality"] = "480"
            wall_later(_restart_main_quality, "480")
            wall_later(reconcile_job)
        mpv(["show-text", f"Pi running hot ({temp:.0f} C): lowering the picture quality", 5000])
    elif temp < pihealth.COOL and heat["on"]:
        heat["on"], saved, heat["saved"] = False, heat["saved"], None
        if saved and wall.active() and len(saved[1]) == len(wall.extras):
            heat["restored"] = True
            main, extra = saved
            if wall.main_quality == "480":
                wall.main_quality = main
                wall_later(_restart_main_quality, main)
            for e, q in zip(wall.extras, extra):
                if e.get("quality") == "480":
                    e["quality"] = q
            wall_later(reconcile_job)


def heat_loop():
    while True:
        time.sleep(10)
        try:
            heat_check(pihealth.temperature())
        except Exception as e:
            app.logger.warning("Heat check failed: %s", e)


@app.get("/api/debug")
def api_debug():
    admin_only()
    c = tgt()
    if c:
        d = c.debug()
        d["versions"] = {**versions(), **d["versions"]}
        return jsonify(d)
    p = state["proc"]
    log = chr(10).join(log_lines())[-8000:]
    return jsonify(
        wall=wall_debug(),
        state={k: v for k, v in state.items() if k != "proc"},
        proc={"pid": p.pid if p else None, "returncode": p.poll() if p else None},
        uptime=round(time.time() - state["started"]) if state.get("started") else None,
        mpv={k: prop(k) for k in DEBUG_PROPS} if prop("pid") is not None else None,
        versions=versions(), last_error=last_error(), log=log, pi=pihealth.snapshot(), heat=heat["on"])


def stream_title(url):
    """A stream's own title (not the channel name): from its details if they are known, else from what the live check saw."""
    meta = (_qcache.get(url) or (0, 0, {}))[2]
    return meta.get("title") or livewatch.snapshot().get(url or "", {}).get("title")


def wall_status():
    """The multi-view as the phone sees it: the wall's own state, plus whether each stream is live (so its tile can say LIVE)."""
    w = wall.status()
    if w:
        w["busy"] = _wall_pending[0] > 0
        chat_refresh()
        w["heat"] = heat["on"]
        snap = livewatch.snapshot()
        for t in w["tiles"]:
            t["live"] = snap.get(t["url"], {}).get("live")
            t["game"], t["live_for"] = snap.get(t["url"], {}).get("game"), livewatch.live_for(t["url"])
            warm_title(t["url"])  # every stream on the screen gets its title looked up, not only the one being controlled
            t["title"] = stream_title(t["url"])
        # tile 0 is described like the others, so the phone can list every stream the same way
        mu = wall.main_url or state["url"]  # what the wall says the main window plays (the window may still be catching up)
        w["main"] = {"i": 0, "url": mu, "source": route(mu or "")[0], "name": fav_name(mu or ""),
                     "live": snap.get(mu or "", {}).get("live"), "quality": w["main_quality"],
                     "game": snap.get(mu or "", {}).get("game"), "live_for": livewatch.live_for(mu or ""),
                     "title": (warm_title(mu), stream_title(mu))[1],
                     "error": "Went offline" if state.get("ended") else None}
    return w


def wall_debug():
    """Every window of the multi-view for the debug view: where it goes, what it plays, what mpv says about it. None when not in use."""
    if not wall.active():
        return None
    out = []
    for i in range(wall.count()):
        t = {"i": i, "window": multi.window_id(wall.geo, wall.count(), i)}
        if i == 0:
            p = state["proc"]
            t.update(url=state["url"], source=state["source"], quality=wall.main_quality or "auto", pid=p.pid if p else None,
                     alive=bool(p and p.poll() is None), log=chr(10).join(log_lines(4000))[-1500:])
        else:
            e = wall.extras[i - 1]
            p = e.get("proc")
            try:
                with open(os.path.join(wall.log_dir, f"tvremote-tile{i}.log"), errors="replace") as f:
                    log = f.read()[-1500:]
            except OSError:
                log = ""
            t.update(url=e["url"], source=e.get("source"), quality=e.get("quality") or "auto", pid=p.pid if p else None,
                     alive=bool(p and p.poll() is None), error=e.get("error"), log=log)
        sock = wall.sock(i, SOCK)
        t["mpv"] = {k: (lambda r: r.get("data") if r and r.get("error") == "success" else None)(mpvipc.send(sock, ["get_property", k]))
                    for k in ("hwdec-current", "width", "height", "container-fps", "frame-drop-count", "mute", "volume", "demuxer-cache-duration")} if t["alive"] else None
        out.append(t)
    return {"layout": wall.layout, "sound": sorted(wall.sound), "tiles": out}


def is_channel(url):
    """A Kick or Twitch channel page (not a video, clip or directory)."""
    if route(url)[0] not in ("Kick", "Twitch"):
        return False
    parts = [p for p in urlparse(url).path.split("/") if p]
    return len(parts) == 1 and parts[0] not in ("videos", "directory", "clips")


def run_streamlink_json(url, timeout=30):
    """`streamlink --json` for a channel: streams and metadata when it is live, an error when not, None if it could not run.
    Runs at a low priority so it never competes with a stream that is playing."""
    try:
        return inproc.streamlink_json(url)
    except Exception as e:
        app.logger.info("streamlink library failed (%s): using the command", e)
    try:
        out = subprocess.run(["nice", "-n", "10", "streamlink", "--json", url],
                             capture_output=True, text=True, timeout=timeout).stdout
        return json.loads(out)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def live_seconds(j):
    """How long a channel has been live, from the elapsed-time tag in the stream's own playlist (Kick and Twitch carry one)."""
    try:
        text = hlsproxy._get(j["streams"]["best"]["url"]).decode(errors="replace")
    except Exception:
        return None
    m = re.search(r"ELAPSED-SECS:(\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else None


def check_live(url):
    """{"live", "game", "title", "uptime_s"} for a channel, {"live": False} when offline, None when it could not be checked."""
    if not is_channel(url):
        return None
    if route(url)[0] == "Kick" and kick.enabled():
        r = kick.check(chat.channel_of(url))
        if r is not None:
            return r  # the official API; on a failure fall through to streamlink
    j = run_streamlink_json(url)
    if j is None:
        return None
    if j.get("streams"):
        meta = j.get("metadata") or {}
        return {"live": True, "game": meta.get("category"), "title": meta.get("title"), "uptime_s": live_seconds(j)}
    if "No playable streams" in (j.get("error") or ""):
        return {"live": False}
    return None


def fav_links(f):
    """Every link of a favourite: its own, then the same streamer's other sites (alts)."""
    return [{"url": f.get("url"), "source": f.get("source")}] + list(f.get("alts") or [])


def streamer_key(url):
    """The channel name for a Kick or Twitch channel link (lower case), so the same name on both sites matches."""
    return chat.channel_of(url or "") if is_channel(url or "") else None


def watched_urls():
    urls = [l["url"] or "" for u in [accounts.OWNER] + accounts.names() for f in load_favs(u) for l in fav_links(f)]  # everyone's: one pass serves all
    urls += [t["url"] for t in wall.extras]  # the streams on the multi-view screen, so their tiles can say LIVE
    urls += [r.get("url") or "" for r in load_recents()]  # recents too: the multi-view suggests the ones that are live
    if state.get("url"):
        urls.append(state["url"])
    return list(dict.fromkeys(u for u in urls if is_channel(u)))


def pi_playing():
    p = state.get("proc")
    return bool(p and p.poll() is None)


livewatch = live.LiveWatch(os.path.join(DATA, "live.json"), urls=watched_urls, check=check_live, busy=pi_playing)


# ---------- multi-stream groups ----------

def groups_file(user=None):
    """Each account has its own groups file, like favourites. Outside a request (the history recorder) it is the owner's."""
    user = user or (g.user if has_request_context() else (accounts.founder_name() or accounts.OWNER))
    return os.path.join(DATA, "groups.json" if user == accounts.OWNER else f"groups.{user}.json")


def _describe_link(url):
    """(name, source) for a link a group may hold (a web link the app can play), else None."""
    u = normalise(url or "")
    return (fav_name(u)[:40], route(u)[0]) if u and not guest_link_refused(u) else None


def _on_screen():
    """The links of every stream on the Pi's screen, the main one first."""
    if wall.active():
        return [wall.main_url or state["url"], *[t["url"] for t in wall.extras]]
    return [state["url"]] if state.get("url") and pi_playing() else []


def _dress(members):
    snap = livewatch.snapshot()
    return [{**m, "live": snap.get(m["url"], {}).get("live")} for m in members]


@app.get("/api/groups")
def groups_get():
    if g.user == accounts.GUEST:
        return jsonify(groups=[], suggested=[])
    d = groups.load(groups_file())
    livewatch.poke()
    return jsonify(groups=[{**x, "members": _dress(x["members"])} for x in d["groups"]],
                   suggested=[{**x, "members": _dress(x["members"])} for x in groups.suggestions(d, _describe_link)])


def _refuse_guest():
    if g.user == accounts.GUEST:
        abort(Response(json.dumps({"ok": False, "error": "Guests can't save groups."}), 403, mimetype="application/json"))


@app.post("/api/groups")
@under_files_lock
def groups_add():
    """Save a group: {urls: [...]} or {from_screen: true}, and an optional name. 2 to 4 different streams."""
    _refuse_guest()
    data = request.get_json(silent=True) or {}
    urls = _on_screen() if data.get("from_screen") else data.get("urls")
    members = groups.clean_members([normalise(u) for u in urls or [] if normalise(u)], _describe_link)
    if not members:
        return jsonify(ok=False, error=f"A group is {groups.MIN_MEMBERS} to {groups.MAX_MEMBERS} different streams."), 400
    path = groups_file()
    d = groups.load(path)
    grp = groups.add(d, members, data.get("name"))
    groups.save(path, d)
    return jsonify(ok=True, group=grp)


@app.delete("/api/groups")
@under_files_lock
def groups_delete():
    _refuse_guest()
    path = groups_file()
    d = groups.load(path)
    if not groups.remove(d, (request.get_json(silent=True) or {}).get("id")):
        return jsonify(ok=False, error="No such group"), 404
    groups.save(path, d)
    return jsonify(ok=True)


@app.post("/api/groups/rename")
@under_files_lock
def groups_rename():
    _refuse_guest()
    data = request.get_json(silent=True) or {}
    path = groups_file()
    d = groups.load(path)
    if not groups.rename(d, data.get("id"), str(data.get("name") or "")):
        return jsonify(ok=False, error="No such group"), 404
    groups.save(path, d)
    return jsonify(ok=True)


@app.post("/api/groups/dismiss")
@under_files_lock
def groups_dismiss():
    """Turn a suggestion down: it is not offered again."""
    _refuse_guest()
    path = groups_file()
    d = groups.load(path)
    groups.dismiss(d, str((request.get_json(silent=True) or {}).get("key") or ""))
    groups.save(path, d)
    return jsonify(ok=True)


def start_group(urls):
    """Put these streams on the Pi's screen together, replacing what is there: the first starts as the main stream, the others are added in one go,
    so every window opens at the same moment."""
    play_on(None, urls[0], None)
    for _ in range(120):  # up to 30 s for the main player to be up
        p = state["proc"]
        if state["url"] == urls[0] and p and p.poll() is None and prop("pid") is not None:
            break
        time.sleep(0.25)
    with lock:
        wall.saved_quality = state["quality"]
    for u in urls[1:]:
        if wall.add(u, state["url"]):
            remember(u)
    wall_later(wall_relayout)


@app.post("/api/groups/play")
def groups_play():
    """Start a saved group (or a suggestion): {id} or {urls}. Answers at once; the streams open in the background."""
    _refuse_guest()
    data = request.get_json(silent=True) or {}
    urls = data.get("urls")
    if data.get("id"):
        grp = next((x for x in groups.load(groups_file())["groups"] if x["id"] == data["id"]), None)
        urls = [m["url"] for m in grp["members"]] if grp else None
    members = groups.clean_members([normalise(u) for u in urls or [] if normalise(u)], _describe_link)
    if not members:
        return jsonify(ok=False, error="No such group"), 404
    if tgt():
        return jsonify(ok=False, error="Groups play on the Pi's screen."), 400
    _pending["t"] = time.time()
    who = g.user

    def run():
        g.user = who
        try:
            start_group([m["url"] for m in members])
        except Exception as e:
            app.logger.warning("Starting the group failed: %s", e)
            screen("error", str(e)[:160])
        finally:
            _pending["t"] = 0.0
    threading.Thread(target=copy_current_request_context(run), daemon=True).start()
    return jsonify(ok=True, count=len(members))


_co = {"key": None, "since": 0.0, "done": False}


def _groups_loop():
    """Every 20 s: when the same streams have been on the screen together for a minute, count that as one session of that set (the owner's history,
    which is where suggestions come from). A different set, or a single stream, starts the count again."""
    while True:
        time.sleep(20)
        try:
            urls = _on_screen() if wall.active() else []
            key = groups.key_of(urls) if len(urls) >= groups.MIN_MEMBERS else None
            if key != _co["key"]:
                _co.update(key=key, since=time.time(), done=False)
            elif key and not _co["done"] and time.time() - _co["since"] >= 60:
                _co["done"] = True
                with _files_lock:
                    path = groups_file(accounts.founder_name() or accounts.OWNER)
                    d = groups.load(path)
                    groups.record(d, urls)
                    groups.save(path, d)
        except Exception as e:
            app.logger.warning("Group history failed: %s", e)


@app.get("/api/favourites")
def favs_get():
    livewatch.poke()
    snap = livewatch.snapshot()
    out = []
    for f in load_favs():
        links = []
        for l in fav_links(f):
            e = snap.get(l["url"], {})
            links.append({**l, "live": e.get("live"), "game": e.get("game"), "title": e.get("title"), "last_live": e.get("last_live"),
                          "checked": e.get("checked"), "live_for": livewatch.live_for(l["url"])})
        top = next((l for l in links if l["live"]), links[0])  # the site they are live on, else the first
        states = [l["live"] for l in links]
        seen = [l["last_live"] for l in links if l["last_live"]]
        out.append({**f, "links": links, "play_url": top["url"],
                    "live": True if any(s is True for s in states) else False if all(s is False for s in states) else None,
                    "game": top["game"], "title": top["title"], "last_live": max(seen) if seen else None, "checked": top["checked"], "live_for": top["live_for"]})
    return jsonify(out)


@app.post("/api/favourites")
@under_files_lock
def favs_add():
    data = request.get_json(silent=True) or {}
    url = normalise(data.get("url") or state["url"])
    if not url:
        return jsonify(ok=False), 400
    favs = load_favs()
    if any(url == l["url"] for f in favs for l in fav_links(f)):
        return jsonify(favs)  # already saved (as the main link or as one of a streamer's sites)
    src, key = route(url)[0], streamer_key(url)
    if key and any(streamer_key(l["url"]) == key and l["source"] == src for f in favs for l in fav_links(f)):
        return jsonify(favs)  # the same channel with different capitalisation
    if key:  # the same name on another site is the same favourite: add it as one more chip, not a second entry
        for f in favs:
            if any(streamer_key(l["url"]) == key and l["source"] != src for l in fav_links(f)):
                f.setdefault("alts", []).append({"url": url, "source": src})
                save_favs(favs)
                return jsonify(favs)
    name = data.get("name") or fav_name(url)
    favs.append({"name": name[:40], "url": url, "source": src})
    save_favs(favs)
    return jsonify(favs)


@app.post("/api/favourites/order")
@under_files_lock
def favs_order():
    """Save the favourites in the order the phone shows them. Anything not listed keeps its place at the end."""
    urls = (request.get_json(silent=True) or {}).get("urls")
    if not isinstance(urls, list):
        return jsonify(ok=False), 400
    favs = load_favs()
    rank = {u: i for i, u in enumerate(urls)}
    favs.sort(key=lambda f: rank.get(f.get("url"), len(rank)))  # stable: unlisted ones stay in their old order
    save_favs(favs)
    return jsonify(favs)


@app.get("/api/kick")
def kick_state():
    return jsonify(can_link=kick.can_link(), name=kick.linked(g.user), can_chat=kick.can_chat(g.user))


@app.get("/auth/kick/start")
def kick_start():
    if not kick.can_link():
        return Response("Kick login is not set up (KICK_CLIENT_ID, KICK_CLIENT_SECRET and KICK_REDIRECT in .env).", 404)
    return redirect(kick.link_url(g.user), 303)


@app.get("/auth/kick/done")
def kick_done():
    """Where the https listener sends the browser back to, with Kick's one-time code."""
    try:
        if request.args.get("error"):
            raise ValueError(request.args["error"])
        kick.link_finish(request.args.get("code"), request.args.get("state"), g.user)
        return redirect("/?kick=linked#/settings", 303)
    except Exception as e:  # a bad state, a refused code or a Kick outage: all end the same way for the user
        print("Kick link failed:", kick.describe(e), flush=True)
        return redirect("/?kick=failed#/settings", 303)


@app.post("/api/kick/unlink")
def kick_unlink():
    kick.unlink(g.user)
    return kick_state()


@app.post("/api/kick/chat")
def kick_chat():
    data = request.get_json(silent=True) or {}
    slug = str(data.get("channel", "")).strip("/").lower()
    slug = slug if re.fullmatch(r"[\w-]{2,25}", slug) else None
    try:
        if not slug:
            raise ValueError("No channel.")
        kick.send_chat(g.user, slug, data.get("text"))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True)


def admin_only():
    if not auth_required():
        abort(Response(json.dumps({"ok": False, "error": "Finish the setup first."}), 403, mimetype="application/json"))
    if accounts.role(g.user) != "admin":
        abort(Response(json.dumps({"ok": False, "error": "Only an admin can do that."}), 403, mimetype="application/json"))


def may_change(name, own_password=False):
    """Admins manage ordinary accounts; only the owner may reset, demote or remove another admin (no takeover between admins)."""
    if accounts.is_owner(g.user) or not accounts.exists(name) or accounts.role(name) != "admin":
        return True
    return own_password and name == g.user


def only_admin(name):
    """True when `name` is the last admin: removing or demoting them would lock everyone out."""
    admins = [u["name"] for u in accounts.listing() if u["role"] == "admin"]
    return admins == [name]


def refuse():
    return jsonify(ok=False, error="Only the owner can change another admin."), 403


@app.get("/api/accounts")
def accounts_list():
    admin_only()
    people = accounts.listing()
    return jsonify(users=[{**u, "favs": len(load_favs(u["name"])), "last": activity.last(u["name"])} for u in people])


@app.post("/api/accounts")
def accounts_add():
    admin_only()
    data = request.get_json(silent=True) or {}
    target = str(data.get("name") or "").strip().lower()
    if not may_change(target, own_password=data.get("role") in (None, "admin")):
        return refuse()
    try:
        name = accounts.add(data.get("name"), data.get("password"), data.get("role"))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    drop_user_sessions(name)  # a new password signs that account out everywhere
    return accounts_list()


@app.patch("/api/accounts")
def accounts_role():
    admin_only()
    data = request.get_json(silent=True) or {}
    if not may_change(str(data.get("name", ""))):
        return refuse()
    if data.get("role") != "admin" and only_admin(str(data.get("name", ""))):
        return jsonify(ok=False, error="That's the only admin. Make someone else an admin first."), 400
    try:
        accounts.set_role(str(data.get("name", "")), data.get("role"))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    return accounts_list()


@app.delete("/api/accounts")
def accounts_del():
    admin_only()
    name = str((request.get_json(silent=True) or {}).get("name", ""))
    if name == g.user:
        return jsonify(ok=False, error="You can't remove the account you're signed in with."), 400
    if not may_change(name):
        return refuse()
    if only_admin(name):
        return jsonify(ok=False, error="That's the only admin. Make someone else an admin first."), 400
    if accounts.remove(name):
        activity.forget(name)
        drop_user_sessions(name)  # re-creating the name later must not revive old logins
        for p in (fav_file(name), kick._path(name)):  # its favourites and Kick link go with it
            try:
                os.remove(p)
            except OSError:
                pass
    return accounts_list()


@app.post("/api/accounts/signout")
def accounts_signout():
    admin_only()
    name = str((request.get_json(silent=True) or {}).get("name", ""))
    if not accounts.exists(name):
        return jsonify(ok=False, error="No such account."), 400
    if not may_change(name):
        return refuse()
    drop_user_sessions(name)
    return jsonify(ok=True)


@app.post("/api/account/password")
def account_password():
    data = request.get_json(silent=True) or {}
    if g.user == accounts.OWNER:
        return jsonify(ok=False, error="Create an account first."), 400
    try:
        accounts.change_password(g.user, data.get("current"), data.get("new"))
    except ValueError as e:
        time.sleep(0.8)  # slows guessing
        return jsonify(ok=False, error=str(e)), 400
    drop_user_sessions(g.user)  # signed out everywhere, this device included: log in again with the new one
    return jsonify(ok=True)


@app.get("/api/access")
def access_state():
    admin_only()
    return jsonify(requests=access.requests_pending(), invites=access.invites_pending(), guests=guest_sessions(), used=access.invites_used())


def slug_name(text):
    return re.sub(r"[^a-z0-9_-]", "", re.sub(r"\s+", "-", (text or "").lower()))[:20]


@app.post("/api/requests/<rid>/approve")
def request_approve(rid):
    admin_only()
    data = request.get_json(silent=True) or {}
    req = next((r for r in access.requests_pending() if r["id"] == rid), None)
    if not req:
        return jsonify(ok=False, error="That request is gone."), 404
    name = slug_name(data.get("name") or req["name"])
    if accounts.exists(name):
        return jsonify(ok=False, error="That name is taken. Pick another."), 400
    password = data.get("password") or secrets.token_urlsafe(9)
    role = data.get("role") if data.get("role") in accounts.ROLES else "user"
    if role == "admin" and not accounts.is_owner(g.user):
        return refuse()
    try:
        accounts.add(name, password, role)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    access.pop_request(rid)
    return jsonify(ok=True, name=name, password=None if data.get("password") else password)  # a generated one is shown once


@app.delete("/api/requests/<rid>")
def request_deny(rid):
    admin_only()
    access.pop_request(rid)
    return access_state()


@app.post("/api/invites")
def invite_create():
    admin_only()
    data = request.get_json(silent=True) or {}
    try:
        token = access.create_invite(int(data.get("hours") or 24), data.get("label"), int(data.get("devices") or 1))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Pick how long access lasts."), 400
    return jsonify(ok=True, url=request.host_url + "join/" + token)


@app.delete("/api/invites/<iid>")
def invite_revoke(iid):
    admin_only()
    access.revoke_invite(iid)
    return access_state()


@app.delete("/api/guest-history")
def guest_history_clear():
    admin_only()
    access.clear_used()
    return access_state()


@app.delete("/api/guest-history/<uid>")
def guest_history_remove(uid):
    admin_only()
    access.clear_used(uid)
    return access_state()


@app.delete("/api/guests/<gid>")
def guest_revoke(gid):
    admin_only()
    drop_guest(gid)
    return access_state()


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0b0e14"><title>__TITLE__</title>
<style>:root{color-scheme:dark}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0e14;color:#f6f1ee;font:17px -apple-system,system-ui,sans-serif}
main{width:min(340px,86vw);display:grid;gap:14px}h1{margin:0 0 6px;font-size:26px;text-align:center}p{margin:0;color:#a9b3c3;text-align:center;line-height:1.4}
input,button{font:inherit;min-height:52px;border-radius:14px;border:0;padding:0 16px;box-sizing:border-box;width:100%}
input{background:#1a2030;color:#f6f1ee}button{background:#62b0ff;color:#0b0e14;font-weight:700}.e{color:#ff6b6b;min-height:1.2em}a{color:#8c97a8;text-align:center;padding:10px}</style></head>
<body><main><img src="/logo.svg" width="84" height="84" alt="" style="justify-self:center;border-radius:20px">__BODY__</main></body></html>"""


def page(title, body, status=200):
    return Response(PAGE.replace("__TITLE__", title).replace("__BODY__", body), status, mimetype="text/html", headers={"Cache-Control": "no-store"})


def request_form(err=""):
    return page("Request access", '<h1>Request access</h1><form method="post" action="/request" style="display:grid;gap:14px">'
                '<input name="name" placeholder="Your name" maxlength="30" autocomplete="name" required>'
                '<input name="note" placeholder="Anything to add? (optional)" maxlength="140">'
                f'<p class="e" role="alert">{html.escape(err)}</p><button>Send request</button><a href="/login">Back to log in</a></form>',
                400 if err else 200)


@app.route("/request", methods=["GET", "POST"])
def request_access():
    if request.method == "GET":
        return request_form()
    try:
        access.add_request(request.form.get("name"), request.form.get("note"), request.remote_addr)
    except ValueError as e:
        return request_form(str(e))
    return page("Request sent", "<h1>Request sent</h1><p>Thanks. An admin will look at it. Ask them to tell you when you have an account.</p>"
                '<a href="/login">Back to log in</a>')


@app.route("/join/<token>", methods=["GET", "POST"])
def join(token):
    who = session_user(request.cookies.get("tv_auth", ""))
    if who == accounts.GUEST:  # already in from this link (or another): clicking it again just goes to the remote, without using anything up
        return redirect("/", 303)
    if who:  # signed in to a real account: this link is for someone else, and using it would swap their login for a guest one
        return page("Already signed in", f"<h1>You're signed in</h1><p>You are signed in as {html.escape(who)}, so you don't need this link. "
                    'It is for someone else, and it has not been used up.</p><a href="/">Open the remote</a>')
    inv = access.peek_invite(token)
    if not inv:
        return page("Link expired", "<h1>Link expired</h1><p>This link was already used or has expired. Ask for a new one.</p>", 410)
    if request.method == "GET":  # opening the link only shows this page: a chat app's link preview must not use the link up
        hrs, n = inv["hours"], inv.get("max_uses", 1)
        span = f"{hrs} hour{'s' if hrs != 1 else ''}" if hrs < 48 else f"{hrs // 24} days"
        once = "This link works once." if n == 1 else f"This link works on up to {n} devices, once each."
        return page("Control the TV", f'<h1>Control the TV</h1><p>You can play things and control the TVs for {span} after you continue. '
                    f'{once}</p><form method="post" style="display:grid"><button>Continue</button></form>')
    inv = access.redeem_invite(token)
    if not inv:
        return page("Link expired", "<h1>Link expired</h1><p>This link was already used or has expired. Ask for a new one.</p>", 410)
    seconds = inv["hours"] * 3600
    r = redirect("/", 303)
    r.set_cookie("tv_auth", new_session(accounts.GUEST, seconds, {"label": inv["label"], "since": time.time(), "entry": inv["entry"]}), max_age=seconds, httponly=True, samesite="Lax")
    return r


def actor():
    """The key activity is kept under: an account name, "owner", or one guest session."""
    if g.user == accounts.GUEST:
        return "guest:" + sessions.token_hash(request.cookies.get("tv_auth", ""))[:8]
    return g.user


def action_label():
    """One short line for what a request did, or None for requests that aren't worth remembering (polling, page loads)."""
    p, m = request.path, request.method
    where = f" ({request.args['target']})" if request.args.get("target") else ""
    data = (request.get_json(silent=True) or {}) if m in ("POST", "DELETE") else {}
    if p == "/play" or (m == "POST" and p == "/api/play"):
        url = request.args.get("u") or data.get("url") or data.get("text") or ""
        return "Played " + (fav_name(normalise(url) or url) if url else "something") + where
    if m == "POST" and p == "/api/cmd":
        return {"stop": "Stopped playback", "pause": "Paused or resumed", "mute": "Muted or unmuted",
                "restart": "Restarted the stream"}.get(data.get("action"), "Used the player controls") + where
    if p == "/api/favourites" and m in ("POST", "DELETE"):
        return "Saved a favourite" if m == "POST" else "Removed a favourite"
    if m == "POST" and p == "/api/roku/key":
        return "Used the TV remote" + where
    if m == "POST" and p == "/api/move":
        return f"Moved {fav_name(normalise(data.get('url') or '') or '')} from {data.get('from')} to {data.get('to')}"[:80]
    if m == "POST" and p == "/api/roku/launch":
        return "Opened a TV app" + where
    if m == "POST" and p == "/api/kick/chat":
        return "Sent a Kick chat message"
    return None


@app.after_request
def note_action(resp):
    try:
        if resp.status_code < 400 and hasattr(g, "user"):
            label = action_label()
            if label:
                activity.note(actor(), label)
    except Exception:  # remembering what someone did must never break what they did, but a mistake here must not hide either
        app.logger.exception("could not note the action")
    return resp


SETUP_CODE = secrets.token_hex(3)  # printed in the container log while the install is fresh; the wizard asks for it


def host_ok():
    """Is the page being reached by an IP address or a local name? Refuses a public name that points at this machine (DNS rebinding)."""
    host = request.host
    h = (host[1:host.index("]")] if host.startswith("[") else host.rsplit(":", 1)[0] if host.count(":") == 1 else host).lower()
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return h == "localhost" or "." not in h or h.endswith((".local", ".lan", ".home.arpa", ".localdomain"))


def setup_code_error(data):
    """None when the host, the throttle and the setup code (printed in the container's log) all pass; otherwise the response to send."""
    ip = request.remote_addr
    if not host_ok():
        return jsonify(ok=False, error="Open the remote by its IP address (or a .local name) to set it up."), 403
    if throttle.setup_blocked(ip):
        return too_many()
    if not hmac.compare_digest(str(data.get("code") or "").strip().lower().encode(), SETUP_CODE.encode()):
        throttle.setup_fail(ip)
        return jsonify(ok=False, error="That setup code isn't right. It is printed in the container's log: docker logs tvremote"), 403
    return None


def adopt_open_data(name):
    """The favourites and Kick link made before accounts existed belong to the owner: give them to the founder (once)."""
    for old, new in ((FAV_FILE, fav_file(name)), (kick._path(accounts.OWNER), kick._path(name))):
        if os.path.exists(old) and not os.path.exists(new):
            os.replace(old, new)


SETUP_FILE = "setup.json"
_setup_lock = threading.Lock()


def setup_done():
    try:
        with open(os.path.join(DATA, SETUP_FILE)) as f:
            return bool(json.load(f).get("done"))
    except (OSError, ValueError):
        return False


def setup_mark():
    with open(os.path.join(DATA, SETUP_FILE), "w") as f:
        json.dump({"done": True, "at": time.time()}, f)


@app.get("/api/setup")
def setup_state():
    """Does the setup wizard need to run, and where it stands. Only admins (or anyone, on a fresh install with no login yet) see it."""
    fresh = not auth_required()
    if not fresh and accounts.role(g.user) != "admin":
        return jsonify(needed=False)
    has_admin = any(u["role"] == "admin" for u in accounts.listing())
    needs_owner = has_admin and not fresh and not accounts.founder_name()  # an install from before owners: an admin has to claim it
    return jsonify(needed=not setup_done() or needs_owner, done=setup_done(), fresh=fresh, has_admin=has_admin, needs_owner=needs_owner,
                   user=g.user if not fresh else None, needs_code=fresh or needs_owner)


@app.post("/api/setup/admin")
def setup_admin():
    """Step 1: the first admin account. On a fresh install this also logs the new admin in (login is required from now on)."""
    data = request.get_json(silent=True) or {}
    with _setup_lock:
        fresh = not auth_required()
        if fresh:
            err = setup_code_error(data)
            if err:
                return err
        else:
            admin_only()
            if not accounts.is_owner(g.user):
                return refuse()  # a fresh install, or the owner: never another admin
        wanted = str(data.get("name") or "").strip().lower()
        if accounts.exists(wanted):
            return jsonify(ok=False, error="That name is taken."), 400  # setup creates accounts, it never overwrites one
        try:
            name = accounts.add(wanted, data.get("password"), "admin", founder=fresh)
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400
    r = jsonify(ok=True, name=name)
    if fresh:
        r.set_cookie("tv_auth", new_session(name), max_age=SESSION_DAYS * 86400, httponly=True, samesite="Lax")
        activity.note(name, "Set up the admin account")
    return r


@app.post("/api/setup/claim")
def setup_claim():
    """An install from before owners existed has no owner. An admin who can read the Pi's log (the setup code) claims it explicitly;
    the favourites and Kick link made before accounts existed then become theirs."""
    admin_only()
    if accounts.founder_name():
        return jsonify(ok=False, error="This install already has an owner."), 400
    err = setup_code_error(request.get_json(silent=True) or {})
    if err:
        return err
    if not accounts.claim_founder(g.user):
        return jsonify(ok=False, error="Only an admin can be the owner."), 400
    adopt_open_data(g.user)
    return jsonify(ok=True, name=g.user)


@app.post("/api/setup/finish")
def setup_finish():
    admin_only()
    if not any(u["role"] == "admin" for u in accounts.listing()):
        return jsonify(ok=False, error="Create the admin account first."), 400
    setup_mark()
    return jsonify(ok=True)


@app.delete("/api/favourites")
@under_files_lock
def favs_del():
    url = (request.get_json(silent=True) or {}).get("url")
    favs = [f for f in load_favs() if f.get("url") != url]  # the main link: the whole favourite goes
    for f in favs:  # one of a streamer's other sites: only that chip goes
        if f.get("alts"):
            f["alts"] = [a for a in f["alts"] if a.get("url") != url]
            if not f["alts"]:
                del f["alts"]
    save_favs(favs)
    return jsonify(favs)


if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    if CastTarget:
        CAST = CastTarget(DATA, resolve, display_title, YTDL_HEIGHTS, SL_DEFAULT)
    init_tvs(DATA)  # after the Chromecast exists, so its name and device follow the saved TVs
    cards.start()
    livewatch.start()
    threading.Thread(target=_rewarm_loop, daemon=True).start()
    threading.Thread(target=sleep_loop, daemon=True).start()
    threading.Thread(target=heat_loop, daemon=True).start()
    threading.Thread(target=_groups_loop, daemon=True).start()
    threading.Thread(target=lambda: (inproc.warm(), versions()), daemon=True).start()  # one-time loads and the tool versions, so no tap pays for them
    settings.update(load_settings())
    HOME = shell.Shell(hint=f"Open http://{shell.lan_ip()}:{PORT} on your phone",
                       qr=lambda: f"http://{shell.lan_ip()}:{PORT}" if settings.get("qr", True) is not False else "",
                       favs=lambda: [(f.get("name") or "")[:28] for f in load_favs()][:5])
    if settings["shell"]:
        threading.Thread(target=HOME.start, daemon=True).start()
    if kick.can_link():
        try:
            threading.Thread(target=tlscallback.make_server(kick.REDIRECT, PORT, DATA).serve_forever, daemon=True).start()
        except Exception as e:  # no openssl or the port is taken: the rest of the app still runs
            print("Kick login listener not started:", e, flush=True)
    if not auth_required():
        print(f"FIRST RUN: open the remote and enter this setup code to create the admin account: {SETUP_CODE}", flush=True)
    elif not accounts.founder_name():
        print(f"OWNER NEEDED: log in as an admin and enter this setup code in the setup wizard to become the owner: {SETUP_CODE}", flush=True)
    from waitress import serve
    serve(app, host="0.0.0.0", port=PORT, threads=8)
