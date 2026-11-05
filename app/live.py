"""Is each channel live, since when, playing what, and when was it last live? Checked gently in the background.

The history is kept on disk, so "last live" survives restarts. It only knows what it has seen: a channel that was live
and went offline while the app was off will show the last time it was seen live."""
import json
import os
import threading
import time


class LiveWatch:
    """urls: () -> the channel URLs to watch. check: url -> {"live": bool, "game", "title", "uptime_s"} or None when
    it could not be checked. busy: () -> True while the Pi is playing a stream (the background pass then waits)."""

    def __init__(self, path, urls, check, every=600, stale=180, busy=lambda: False, clock=time.time):
        self.path, self.urls, self.check = path, urls, check
        self.every, self.stale, self.busy, self._clock = every, stale, busy, clock
        self._lock = threading.Lock()
        self._thread = None
        self._data = self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._data, f)
            os.replace(tmp, self.path)
        except OSError:
            pass  # a read-only disk must not stop the app

    def snapshot(self):
        with self._lock:
            return json.loads(json.dumps(self._data))

    def live_for(self, url):
        """Seconds the channel has been live, counted on from the last check; None if it isn't live."""
        e = self._data.get(url) or {}
        if e.get("live") and e.get("uptime_s") is not None and e.get("checked"):
            return e["uptime_s"] + (self._clock() - e["checked"])
        return None

    def refresh_one(self, url):
        result = self.check(url)
        now = self._clock()
        with self._lock:
            e = self._data.setdefault(url, {})
            e["checked"] = now
            if result is None:
                e["live"] = None  # could not tell; keep the history
            else:
                e["live"] = bool(result.get("live"))
                if e["live"]:
                    e["last_live"] = now
                    for k in ("game", "title"):
                        if result.get(k):
                            e[k] = result[k]
                    e["uptime_s"] = result.get("uptime_s")
        self._save()

    def refresh_now(self):
        wanted = list(self.urls())
        for url in wanted:
            if self.busy():
                break  # a stream is playing: leave the Pi alone
            self.refresh_one(url)
        with self._lock:
            for url in [u for u in self._data if u not in wanted]:
                del self._data[url]
        self._save()

    def poke(self):
        """Someone is looking at the list: refresh in the background if anything is stale. Returns the thread or None."""
        if self._thread and self._thread.is_alive():
            return None
        now = self._clock()
        if not any(now - (self._data.get(u) or {}).get("checked", 0) > self.stale for u in self.urls()):
            return None
        self._thread = threading.Thread(target=self.refresh_now, daemon=True)
        self._thread.start()
        return self._thread

    def start(self):
        def loop():
            while True:
                self.refresh_now()
                threading.Event().wait(self.every)
        threading.Thread(target=loop, daemon=True).start()
