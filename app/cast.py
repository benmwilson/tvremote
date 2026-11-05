"""Chromecast target: mDNS discovery + playback via pychromecast.

Exposes the same surface as the Pi target (play / cmd / status / qualities / debug) so the UI doesn't care.
"""
import json
import os
import re
import socket
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlparse

import hlsproxy
import pychromecast
import zeroconf
from pychromecast.config import APP_MEDIA_RECEIVER, APP_YOUTUBE
from pychromecast.discovery import CastBrowser, SimpleCastListener
from pychromecast.quick_play import quick_play

DEFAULT_CFG = {"name": "Bedroom", "device": None}  # device = Chromecast friendly name; None = first one found


class CastError(Exception):
    pass


def youtube_id(url):
    """Video id from a YouTube URL, or None (channel /live URLs need yt-dlp)."""
    u = urlparse(url)
    if "youtu.be" in (u.hostname or ""):
        return u.path.strip("/").split("/")[0] or None
    m = re.match(r"^/(?:shorts|live|embed)/([\w-]{11})", u.path)
    if m:
        return m.group(1)
    v = parse_qs(u.query).get("v")
    return v[0] if v else None


def run(cmd, timeout=30):
    """Run a helper, return stdout; raise CastError with the last error line on failure."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CastError(f"{cmd[0]} failed: {e}")
    out = p.stdout.strip()
    if p.returncode or not out:
        err = (p.stderr.strip().splitlines() or ["no output"])[-1]
        raise CastError(re.sub(r"^(ERROR|error):\s*", "", err)[:200])
    return out


class CastTarget:
    type = "chromecast"
    id = "bedroom"

    def __init__(self, data_dir, resolve, title_fn, ytdl_heights, sl_default):
        self.resolve, self.title_fn, self.heights, self.sl_default = resolve, title_fn, ytdl_heights, sl_default
        self.cfg = dict(DEFAULT_CFG)
        try:
            with open(os.path.join(data_dir, "targets.json")) as f:
                self.cfg.update(json.load(f).get(self.id, {}))
        except (OSError, ValueError):
            pass
        self.name = self.cfg["name"]
        self.via_mode = os.environ.get("TVREMOTE_CAST_VIA", "direct")  # flip the default once proxy is proven better
        self.lock = threading.RLock()
        self.devices = {}   # uuid -> CastInfo
        self.cast = None
        self.port = os.environ.get("TVREMOTE_PORT", "8765")
        self.state = {"url": None, "source": None, "player": None, "quality": None, "error": None,
                      "started": None, "via": None, "proxy_tried": False, "first_play": None}
        self.zc = zeroconf.Zeroconf()
        listener = SimpleCastListener(self._seen, self._gone, self._seen)
        self.browser = CastBrowser(listener, self.zc)
        self.browser.start_discovery()

    # ---- discovery / connection ----
    def _seen(self, uuid, _name):
        info = self.browser.devices.get(uuid)
        if not info:
            return
        with self.lock:
            self.devices[uuid] = info
            c = self.cast
            if c and c.uuid == uuid and c.cast_info.host != info.host:  # Chromecast came back on a new IP
                self._drop()

    def _gone(self, uuid, _name, _info):
        with self.lock:
            self.devices.pop(uuid, None)

    def _drop(self):
        try:
            self.cast.disconnect(timeout=2)
        except Exception:
            pass
        self.cast = None

    def device(self):
        """CastInfo of the configured (or first) Chromecast, or None."""
        want = self.cfg.get("device")
        for info in list(self.devices.values()):
            if info.cast_type == "cast" and (not want or info.friendly_name == want):
                return info
        return None

    def info(self):
        d = self.device()
        model = d.model_name if d else None
        return {"id": self.id, "name": self.name, "type": self.type, "online": bool(d),
                "device": d.friendly_name if d else None, "model": model,
                "kind": "Google TV" if model and "google tv" in model.lower() else ("Chromecast" if d else None)}

    def _connect(self):
        with self.lock:
            d = self.device()
            if not d:
                raise CastError("Bedroom Chromecast not found on the network")
            if self.cast and (self.cast.uuid != d.uuid or self.cast.socket_client.is_stopped):
                self._drop()
            if not self.cast:
                c = pychromecast.get_chromecast_from_cast_info(d, self.zc)
                c.wait(timeout=15)
                self.cast = c
            return self.cast

    # ---- playback ----
    def _lan_ip(self, host):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 9))
            return s.getsockname()[0]

    def _quality(self, player, quality):
        if player == "streamlink":
            return quality if quality and re.fullmatch(r"[a-z0-9_]+(,[a-z0-9_]+)*", quality) and not quality.isdigit() else self.sl_default
        return quality if quality in self.heights else "1080"

    def _media_url(self, url, source, player, q):
        """(url, content type, stream type) for the Default Media Receiver."""
        if player == "streamlink":
            cmd = ["streamlink", "--stream-url"] + (["--twitch-disable-ads"] if source == "Twitch" else []) + [url, q]
            return run(cmd).splitlines()[0], "application/x-mpegURL", "LIVE"
        fmt = f"b[vcodec^=avc1][height<={q}][ext=mp4]/b[ext=mp4][height<={q}]/b[ext=mp4]"
        return run(["yt-dlp", "--no-playlist", "-g", "-f", fmt, url], 45).splitlines()[0], "video/mp4", "BUFFERED"

    def play(self, url, quality=None, via=None, cancelled=lambda: False):
        """via: "direct" (Chromecast fetches the stream itself) or "proxy" (the Pi fetches, serves it on the LAN)."""
        via = via if via in ("direct", "proxy") else self.via_mode
        source, player = self.resolve(url)
        q = self._quality(player, quality)
        if cancelled():
            return  # a newer tap replaced this one while its link was being worked out
        # Work out the new stream before touching anything. If that fails (a channel that is offline), whatever is playing keeps
        # playing and the status keeps describing it, instead of showing the playing stream under the failed channel's name.
        if source == "YouTube":
            vid = youtube_id(url) or run(["yt-dlp", "--no-playlist", "--print", "id", url]).splitlines()[0]
        else:
            media, ctype, stype = self._media_url(url, source, player, q)
        cast = self._connect()
        if cancelled():
            return
        hlsproxy.clear()  # any previous relay session ends; _start_proxy sets a new one
        with self.lock:
            self.state.update(url=url, source=source, player=player, quality=q, error=None,
                              started=time.time(), via=None, proxy_tried=False, mode=via, first_play=None)
        if source == "YouTube":
            quick_play(cast, "youtube", {"media_id": vid})
            self.state["via"] = "youtube"
            return
        if via == "proxy" and player == "streamlink":
            self.state["proxy_tried"] = True
            return self._start_proxy(url, source, q, media)
        if cast.app_id != APP_MEDIA_RECEIVER:  # e.g. the YouTube app is open and would ignore the LOAD
            cast.start_app(APP_MEDIA_RECEIVER)
        cast.media_controller.play_media(media, ctype, title=self.title_fn(url), stream_type=stype)
        self.state["via"] = "direct"

    def _start_proxy(self, url, source, q, media=None):
        """The Pi fetches the HLS segments (wired) and relays them; the Chromecast only talks plain HTTP to the Pi."""
        media = media or self._media_url(url, source, "streamlink", q)[0]
        hlsproxy.set_upstream(media)
        cast = self._connect()
        if cast.app_id != APP_MEDIA_RECEIVER:
            cast.start_app(APP_MEDIA_RECEIVER)
        cast.media_controller.play_media(f"http://{self._lan_ip(self.device().host)}:{self.port}/cast/{hlsproxy.token()}/live.m3u8",
                                         "application/x-mpegURL", title=self.title_fn(url), stream_type="LIVE")
        self.state["via"] = "pi-proxy"

    def _fallback_proxy(self):
        """Direct URL was refused (CORS/403/token): serve the stream from the Pi instead."""
        st = self.state
        if st["proxy_tried"] or st["player"] != "streamlink":
            return
        st["proxy_tried"] = True
        self._start_proxy(st["url"], st["source"], st["quality"])

    # ---- controls ----
    def cmd(self, action, value=None, quality=None):
        """Returns (ok, error)."""
        if action == "stop":
            hlsproxy.clear()
            if self.cast:
                self.cast.quit_app()
            self.state["url"] = None
            return True, None
        if action == "restart":
            if not self.state["url"]:
                return False, "Nothing to restart"
            self.play(self.state["url"], quality or self.state["quality"], self.state.get("mode"))
            return True, None
        cast = self._connect()
        mc, rc = cast.media_controller, cast.socket_client.receiver_controller
        ms = mc.status
        if action == "pause":
            mc.play() if ms.player_is_paused else mc.pause()
        elif action == "mute":
            rc.set_volume_muted(not cast.status.volume_muted)
        elif action == "volume":
            rc.set_volume(max(0.0, min(1.0, float(value) / 100)))
        elif action == "volume_delta":
            rc.set_volume(max(0.0, min(1.0, (cast.status.volume_level or 0) + float(value) / 100)))
        elif action == "seek":
            mc.seek(max(0.0, ms.adjusted_current_time + float(value)))
        elif action == "seek_to":
            mc.seek(max(0.0, float(value)))
        else:
            return False, "Unknown action"
        return True, None

    # ---- status ----
    def status(self):
        st = self.state
        data = {"running": False, "url": st["url"], "quality": st["quality"], "source": st["source"],
                "streamlink": st["player"] == "streamlink", "target": self.id}
        c = self.cast
        if not c or not st["url"]:
            return data
        ms, cs = c.media_controller.status, c.status
        if c.app_id != (APP_YOUTUBE if st["source"] == "YouTube" else APP_MEDIA_RECEIVER):
            # another app is in front (or ours hasn't started yet): its media status is not ours
            data["starting"] = time.time() - (st["started"] or 0) < 25
            return data
        if ms.idle_reason == "ERROR" and ms.player_state == "IDLE":
            try:
                self._fallback_proxy()
            except Exception as e:
                st["error"] = f"Chromecast couldn't load the stream ({e})"
            if st["proxy_tried"] and st["via"] != "pi-proxy" and not st["error"]:
                st["error"] = "Chromecast couldn't load the stream"
            data["error"] = st["error"] or None
        if ms.player_state == "PLAYING" and st.get("first_play") is None:
            st["first_play"] = time.time()
        if ms.player_state in ("PLAYING", "PAUSED", "BUFFERING"):
            dur = ms.duration
            data.update(running=True, title=ms.title or self.title_fn(st["url"]), paused=ms.player_is_paused,
                        position=ms.adjusted_current_time, duration=dur,
                        live=bool(ms.stream_type_is_live or not dur),
                        volume=(cs.volume_level or 0) * 100 if cs else None, muted=bool(cs and cs.volume_muted),
                        height=None, hwdec=st["via"], drops=None, buffering=ms.player_state == "BUFFERING",
                        start_s=round(st["first_play"] - st["started"], 1) if st.get("first_play") and st.get("started") else None)
        else:
            data["starting"] = time.time() - (st["started"] or 0) < 25 and not data.get("error") and ms.player_state != "IDLE" \
                or time.time() - (st["started"] or 0) < 8
        return data

    def qualities(self, url, fallback):
        if not url or self.resolve(url)[0] == "YouTube":
            return []  # YouTube receiver picks its own quality
        return fallback(url)

    def debug(self):
        c, st = self.cast, dict(self.state)
        ms = c.media_controller.status if c else None
        media = {k: v for k, v in vars(ms).items() if not isinstance(v, (dict, list))} if ms else None
        return {"state": st, "proc": {"pid": None, "returncode": None},
                "uptime": round(time.time() - st["started"]) if st["started"] else None,
                "mpv": media, "versions": {"pychromecast": getattr(pychromecast, "__version__", "?"),
                                           **{k: v for k, v in self.info().items() if k in ("device", "model", "kind")}},
                "last_error": st["error"], "log": json.dumps(self.info(), indent=1)}
