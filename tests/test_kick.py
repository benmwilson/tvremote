import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import kick  # noqa: E402


def stub(channel):
    """Replace the network: the token endpoint answers a token, the channels endpoint answers `channel`."""
    def fake(req, timeout=10):
        if "id.kick.com" in req.full_url:
            return json.dumps({"access_token": "t", "expires_in": 3600}).encode()
        assert req.get_header("Authorization") == "Bearer t" and "slug=chan" in req.full_url
        return json.dumps({"data": channel}).encode()
    return fake


class KickTest(unittest.TestCase):
    def setUp(self):
        kick.ID, kick.SECRET = "id", "secret"
        kick._token.update(value=None, exp=0.0)

    def test_disabled_without_credentials(self):
        kick.ID = ""
        self.assertIsNone(kick.check("chan"))

    def test_live(self):
        kick._open = stub([{"stream_title": "hi", "category": {"name": "Just Chatting"},
                            "stream": {"is_live": True, "start_time": "2020-01-01T00:00:00Z", "viewer_count": 7}}])
        r = kick.check("chan")
        self.assertTrue(r["live"])
        self.assertEqual((r["game"], r["title"], r["viewers"]), ("Just Chatting", "hi", 7))
        self.assertGreater(r["uptime_s"], 0)

    def test_offline(self):
        kick._open = stub([{"stream": {"is_live": False}}])
        self.assertEqual(kick.check("chan"), {"live": False})

    def test_unknown_channel_and_errors_are_none(self):
        kick._open = stub([])
        self.assertIsNone(kick.check("chan"))

        def boom(req, timeout=10):
            raise OSError("down")
        kick._open = boom
        self.assertIsNone(kick.check("chan"))


if __name__ == "__main__":
    unittest.main()
