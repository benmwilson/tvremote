"""TV cards: a cached, per-TV snapshot the UI can read without ever waiting on a TV."""
import threading
import time


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
    Each refresh gives every provider `timeout` seconds. A provider that raises is unreachable at once. One that
    is merely late keeps showing its last good fields, and stays reachable while that data is under `stale_after`
    seconds old; if it finishes after its deadline, the next refresh adopts the result. A provider still running
    from an earlier refresh is not started again."""

    def __init__(self, providers, order, timeout=2.0, every=3.0):
        self.providers, self.order, self.timeout, self.every = providers, order, timeout, every
        self.stale_after = every * 3
        self._cards = {i: {"id": i, "reachable": False} for i in order}
        self._good = {}
        self._running = {}  # id -> (thread, box)
        self._lock = threading.Lock()

    def _store(self, tv_id, card):
        with self._lock:
            self._cards[tv_id] = {**card, "id": tv_id, "reachable": True}
            self._good[tv_id] = time.time()

    def _mark(self, tv_id, slow):
        with self._lock:
            recent = slow and time.time() - self._good.get(tv_id, 0) < self.stale_after
            self._cards[tv_id] = {**self._cards[tv_id], "reachable": recent}

    def _one(self, tv_id, fn):
        prev = self._running.get(tv_id)
        if prev:
            thread, box = prev
            if thread.is_alive():
                return self._mark(tv_id, slow=True)
            if "card" in box:  # it finished after its deadline: keep what it found
                self._store(tv_id, box["card"])
        box = {}

        def run():
            try:
                box["card"] = fn()
            except Exception:
                box["failed"] = True
        thread = threading.Thread(target=run, daemon=True)
        self._running[tv_id] = (thread, box)
        thread.start()
        thread.join(self.timeout)
        if "card" in box and not thread.is_alive():
            self._store(tv_id, box.pop("card"))  # adopted now, so the next refresh does not adopt it again
        elif box.get("failed"):
            self._mark(tv_id, slow=False)
        else:
            self._mark(tv_id, slow=True)

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
