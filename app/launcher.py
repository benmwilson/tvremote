"""The TV's launcher: what the home screen offers (who is live, favourites, groups, recents) as rows of tiles, and moving around it with
remote-control keys. Pure logic with no screen and no input device in it: the home screen draws what `rows` and `focus` say, and any source
of keys (the phone's pad, a keyboard, the TV remote through HDMI-CEC or an IR receiver) feeds `key()`.

    pad = Launcher(rows)            # rows: [{"title": "Live now", "tiles": [{"id", "name", "kind", "url", ...}]}]
    ev = pad.key("down")            # -> {"redraw": True} | {"play": url} | {"group": id} | {"back": True} | None
"""

# every way a key arrives -> the one name the launcher uses (Linux input names from CEC / IR / a keyboard, Roku ECP names from the phone's pad)
KEYMAP = {
    "KEY_UP": "up", "KEY_DOWN": "down", "KEY_LEFT": "left", "KEY_RIGHT": "right",
    "KEY_ENTER": "select", "KEY_OK": "select", "KEY_SELECT": "select", "KEY_KPENTER": "select", "KEY_SPACE": "select",
    "KEY_BACK": "back", "KEY_EXIT": "back", "KEY_ESC": "back", "KEY_BACKSPACE": "back",
    "KEY_HOME": "home", "KEY_MENU": "home", "KEY_HOMEPAGE": "home",
    "KEY_PLAYPAUSE": "play", "KEY_PLAY": "play", "KEY_PAUSE": "play",
    "KEY_INFO": "info",
    "Up": "up", "Down": "down", "Left": "left", "Right": "right", "Select": "select", "Back": "back", "Home": "home", "Play": "play", "Info": "info",
}


def normal_key(name):
    """The launcher's name for a key as the source called it, or None when the launcher has no use for it."""
    return KEYMAP.get(str(name))


def build_rows(favs=(), recents=(), groups=(), live_first=True):
    """The launcher's rows from the app's own lists. Each item: {"name", "url", "live", "source", "avatar"?}; groups: {"id", "name", "members"}.
    "Live now" comes first (anyone live among favourites and recents, once each), then Groups, Favourites and Recents (without repeats)."""
    seen, live_tiles, rows = set(), [], []
    def key(x):
        return (x.get("name") or x.get("url") or "").lower()
    def tile(x, kind="stream"):
        return {"id": x.get("id") or x.get("url"), "kind": kind, "name": x.get("name") or x.get("url"), "url": x.get("url"),
                "live": x.get("live") is True, "source": x.get("source"), "avatar": x.get("avatar"), "members": x.get("members"),
                "thumb": x.get("thumb"), "viewers": x.get("viewers"), "title": x.get("title"), "game": x.get("game")}
    if live_first:
        for x in list(favs) + list(recents):
            if x.get("live") is True and key(x) not in seen:
                seen.add(key(x))
                live_tiles.append(tile(x))
        if live_tiles:
            rows.append({"title": "Live now", "tiles": live_tiles})
    gtiles = [tile(g, "group") for g in groups]
    if gtiles:
        rows.append({"title": "Groups", "tiles": gtiles})
    ftiles = [tile(x) for x in favs if key(x) not in {key(t) for t in live_tiles}]
    if ftiles:
        rows.append({"title": "Favourites", "tiles": ftiles})
    rtiles = [tile(x) for x in recents if key(x) not in {key(t) for t in live_tiles} and key(x) not in {key(t) for t in ftiles}]
    if rtiles:
        rows.append({"title": "Recent", "tiles": rtiles})
    return rows


class Launcher:
    def __init__(self, rows=()):
        self.rows = [r for r in rows if r.get("tiles")]
        self.row = 0
        self.col = 0
        self._cols = {}  # the column each row was left at, so going down and back up returns to where you were

    def set_rows(self, rows):
        """New contents (the lists changed): keep the focus on the same tile when it is still there, else the nearest place."""
        was = self.focused()
        self.rows = [r for r in rows if r.get("tiles")]
        self.row = self.col = 0
        self._cols = {}
        if was:
            for i, r in enumerate(self.rows):
                for j, t in enumerate(r["tiles"]):
                    if t["id"] == was["id"]:
                        self.row, self.col = i, j
                        return
        self._clamp()

    def focused(self):
        if not self.rows:
            return None
        self._clamp()
        return self.rows[self.row]["tiles"][self.col]

    def _clamp(self):
        if not self.rows:
            self.row = self.col = 0
            return
        self.row = max(0, min(self.row, len(self.rows) - 1))
        self.col = max(0, min(self.col, len(self.rows[self.row]["tiles"]) - 1))

    def key(self, name):
        """One key press. Returns what the caller should do: {"redraw": True} when the focus moved, {"play": url} or {"group": id} for a
        select, {"back": True} / {"home": True} for those keys, None when nothing happens."""
        k = normal_key(name) or (name if name in ("up", "down", "left", "right", "select", "back", "home", "play", "info") else None)
        if not k or not self.rows:
            return {"back": True} if k == "back" else {"home": True} if k == "home" else None
        before = (self.row, self.col)
        if k == "left":
            self.col = max(0, self.col - 1)
        elif k == "right":
            self.col = min(len(self.rows[self.row]["tiles"]) - 1, self.col + 1)
        elif k in ("up", "down"):
            self._cols[self.row] = self.col
            self.row = max(0, min(len(self.rows) - 1, self.row + (1 if k == "down" else -1)))
            self.col = self._cols.get(self.row, self.col)
            self._clamp()
        elif k in ("select", "play"):
            t = self.focused()
            return {"group": t["id"]} if t["kind"] == "group" else {"play": t["url"]}
        elif k == "back":
            return {"back": True}
        elif k == "home":
            self.row = self.col = 0
            return {"redraw": True} if before != (0, 0) else {"home": True}
        else:
            return None
        return {"redraw": True} if (self.row, self.col) != before else None
