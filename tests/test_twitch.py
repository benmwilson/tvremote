import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import kick  # noqa: E402
import tlscallback  # noqa: E402
import twitch  # noqa: E402


def stub(streams=(), users=()):
    """The token endpoint answers a token; /streams and /users answer the given rows."""
    def fake(req, timeout=10):
        if "id.twitch.tv" in req.full_url:
            return json.dumps({"access_token": "t", "expires_in": 3600}).encode()
        assert req.get_header("Authorization") == "Bearer t" and req.get_header("Client-id") == "id"
        return json.dumps({"data": list(streams if "/streams" in req.full_url else users)}).encode()
    return fake


class TwitchTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.multiple(twitch, ID=twitch.ID, SECRET=twitch.SECRET, DATA=twitch.DATA, REDIRECT=twitch.REDIRECT, _open=twitch._open)
        p.start()
        self.addCleanup(p.stop)
        twitch.ID, twitch.SECRET = "id", "secret"
        twitch._token.update(value=None, exp=0.0)
        twitch._users.clear()

    def test_disabled_without_credentials(self):
        twitch.ID = ""
        self.assertIsNone(twitch.check("x"))
        self.assertEqual(twitch.live_now(), [])

    def test_live_and_offline(self):
        twitch._open = stub([{"type": "live", "title": "hi", "game_name": "Chess", "viewer_count": 9, "started_at": "2020-01-01T00:00:00Z"}])
        r = twitch.check("x")
        self.assertEqual((r["live"], r["game"], r["title"], r["viewers"]), (True, "Chess", "hi", 9))
        self.assertGreater(r["uptime_s"], 0)
        twitch._open = stub([])
        self.assertEqual(twitch.check("x"), {"live": False})

    def test_a_failure_is_none_not_offline(self):
        def boom(req, timeout=10):
            raise OSError("down")
        twitch._open = boom
        self.assertIsNone(twitch.check("x"))

    def test_live_now_has_pictures_and_sized_thumbnails(self):
        twitch._open = stub([{"type": "live", "user_login": "abc", "user_name": "Abc", "title": "t", "game_name": "g", "viewer_count": 5,
                              "thumbnail_url": "https://x/{width}x{height}.jpg"}],
                            [{"id": "1", "login": "abc", "display_name": "Abc", "profile_image_url": "https://x/p.png"}])
        r = twitch.live_now(5)
        self.assertEqual((r[0]["login"], r[0]["avatar"], r[0]["viewers"]), ("abc", "https://x/p.png", 5))
        self.assertEqual(r[0]["thumb"], "https://x/440x248.jpg")

    def test_default_redirect_follows_kicks_host_and_port(self):
        with mock.patch.dict("os.environ", {"TVREMOTE_KICK_REDIRECT": "https://1.2.3.4:8766/auth/kick/callback"}):
            self.assertEqual(twitch._default_redirect(), "https://1.2.3.4:8766/auth/twitch/callback")

    def test_link_state_must_match_account(self):
        twitch.REDIRECT = "https://h/auth/twitch/callback"
        url = twitch.link_url("bob")
        state = url.split("state=")[1].split("&")[0]
        with self.assertRaises(ValueError):
            twitch.link_finish("code", state, "alice")  # a state is for the account that asked
        with self.assertRaises(ValueError):
            twitch.link_finish("code", "unknown", "bob")

    def test_chat_needs_the_scope(self):
        with tempfile.TemporaryDirectory() as d:
            twitch.DATA = d
            with self.assertRaises(ValueError):
                twitch.send_chat("bob", "chan", "hello")
            self.assertFalse(twitch.can_chat("bob"))

    def test_kick_live_now_shape(self):
        with mock.patch.multiple(kick, ID="id", SECRET="s", _open=lambda req, timeout=10: json.dumps({"data": [
                {"slug": "k", "stream_title": "t", "category": {"name": "g"}, "viewer_count": 3, "profile_picture": "p", "thumbnail": "th"}]}).encode()), \
                mock.patch.dict(kick._token, {"value": "t", "exp": 9e12}):
            r = kick.live_now(3)
        self.assertEqual((r[0]["login"], r[0]["game"], r[0]["avatar"], r[0]["viewers"]), ("k", "g", "p", 3))

    def test_tls_listener_knows_both_sites(self):
        self.assertEqual(tlscallback.PATHS, {"/auth/kick/callback": "kick", "/auth/twitch/callback": "twitch"})


if __name__ == "__main__":
    unittest.main()
