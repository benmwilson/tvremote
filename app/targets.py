"""TV cards: a cached, per-TV snapshot the UI can read without ever waiting on a TV."""
import threading


def health(kind, status):
    """Quiet playback-health summary from a target's /api/status dict. kind is "pi" or "chromecast"."""
    if status.get("error"):
        state = "error"
    elif status.get("starting"):
        state = "starting"
    elif status.get("buffering"):
        state = "buffering"
    elif status.get("running"):
        state = "smooth"
    else:
        state = "idle"
    return {"state": state, "drops": status.get("drops") if kind == "pi" else None, "start_s": status.get("start_s")}


class Cards:
    """providers: id -> function returning that TV's card dict (may be slow, may raise).
    Each refresh gives every provider `timeout` seconds; one that is late or failing keeps its last good
    fields and is marked reachable=False. A provider still running from an earlier refresh is not started again."""

    def __init__(self, providers, order, timeout=2.0, every=3.0):
        self.providers, self.order, self.timeout, self.every = providers, order, timeout, every
        self._cards = {i: {"id": i, "reachable": False} for i in order}
        self._running = {}
        self._lock = threading.Lock()

    def _one(self, tv_id, fn):
        prev = self._running.get(tv_id)
        if prev and prev.is_alive():
            ok, card = False, None
        else:
            box = {}

            def run():
                try:
                    box["card"] = fn()
                except Exception:
                    pass
            t = threading.Thread(target=run, daemon=True)
            self._running[tv_id] = t
            t.start()
            t.join(self.timeout)
            ok, card = "card" in box and not t.is_alive(), box.get("card")
        with self._lock:
            old = self._cards[tv_id]
            self._cards[tv_id] = {**card, "id": tv_id, "reachable": True} if ok else {**old, "reachable": False}

    def refresh_now(self):
        threads = [threading.Thread(target=self._one, args=(i, f)) for i, f in self.providers.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def start(self):
        def loop():
            while True:
                self.refresh_now()
                threading.Event().wait(self.every)
        threading.Thread(target=loop, daemon=True).start()

    def snapshot(self):
        with self._lock:
            return [dict(self._cards[i]) for i in self.order]
