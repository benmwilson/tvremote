"""Who did what last, kept light: one line per person (an account name, "owner", or "guest:<session id>"), held in memory
and saved to /data/activity.json at most every 30 seconds. Not a log: nothing accumulates, and polling never counts."""
import json
import os
import threading
import time

SAVE_EVERY = 30
_lock = threading.Lock()
_data = None
_saved = 0.0


def _path():
    return os.path.join(os.environ.get("TVREMOTE_DATA", "/data"), "activity.json")


def _load():
    global _data
    if _data is None:
        try:
            with open(_path()) as f:
                d = json.load(f)
            _data = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            _data = {}
    return _data


def note(who, text, now=None):
    """Remember the latest thing `who` did. The file is only rewritten if the last write was a while ago."""
    global _saved
    now = now or time.time()
    with _lock:
        d = _load()
        d[who] = {"text": text[:80], "ts": now}
        if len(d) > 200:  # guest sessions come and go: drop the oldest
            for k in sorted(d, key=lambda k: d[k]["ts"])[:len(d) - 200]:
                del d[k]
        if now - _saved >= SAVE_EVERY:
            _saved = now
            try:
                tmp = _path() + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(d, f)
                os.replace(tmp, _path())
            except OSError:
                pass  # a read-only disk must not stop the app


def last(who):
    return _load().get(who)


def forget(who):
    with _lock:
        _load().pop(who, None)


def reset():  # for tests
    global _data, _saved
    _data, _saved = None, 0.0
