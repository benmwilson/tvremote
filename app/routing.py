"""Which site a link belongs to and how it is played, and pulling a link out of shared text. Pure functions: no Flask, no state.
app.py imports these names, so `app.route`, `app.normalise` and `app.SOURCES` still work."""
import re
from urllib.parse import urlparse

# Routing table: one line per site. player: "streamlink", "ytdlp" or "auto" (YouTube: live -> streamlink).
SOURCES = [
    # (name,      hosts,                              player)
    ("Kick",    ("kick.com",),                       "streamlink"),
    ("Twitch",  ("twitch.tv",),                      "streamlink"),
    ("YouTube", ("youtube.com", "youtu.be"),         "auto"),
]


def route(url):
    """Return (source name, player) for a URL."""
    host = (urlparse(url).hostname or "").lower()
    for name, hosts, player in SOURCES:
        if any(host == h or host.endswith("." + h) for h in hosts):
            return name, player
    return "Other", "ytdlp"


def normalise(text, site="kick"):
    """Pull the first URL out of shared text; a bare word becomes a channel on `site`."""
    text = (text or "").strip()
    m = re.search(r"https?://[^\s<>\"']+", text)
    if m:
        return m.group(0).rstrip(").,")
    if re.fullmatch(r"[A-Za-z0-9_\-]{2,40}", text):
        return f"https://{'twitch.tv' if site == 'twitch' else 'kick.com'}/{text}"
    return None


if __name__ == "__main__":
    assert route("https://www.twitch.tv/xqc") == ("Twitch", "streamlink")
    assert route("https://youtu.be/x") == ("YouTube", "auto") and route("https://example.com/a")[0] == "Other"
    assert normalise("Watch X on Kick https://kick.com/x).") == "https://kick.com/x"
    assert normalise("xqc", "twitch") == "https://twitch.tv/xqc" and normalise("two words") is None
    print("ok")
