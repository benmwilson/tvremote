import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import kick  # noqa: E402
import tlscallback  # noqa: E402


class LinkTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        kick.ID, kick.SECRET, kick.REDIRECT = "id", "secret", "https://192.168.1.2:8766/auth/kick/callback"
        kick.LINK = os.path.join(self.dir, "link.json")
        kick._pending.clear()
        self.sent = []

        def fake(req, timeout=10):
            self.sent.append(req)
            if "oauth/token" in req.full_url:
                return json.dumps({"access_token": "a", "refresh_token": "r", "expires_in": 60}).encode()
            return json.dumps({"data": [{"user_id": 5, "name": "ben"}]}).encode()
        kick._open = fake

    def state(self):
        q = parse_qs(urlparse(kick.link_url()).query)
        self.assertEqual(q["code_challenge_method"], ["S256"])
        self.assertEqual(q["redirect_uri"], [kick.REDIRECT])
        return q["state"][0]

    def test_link_roundtrip(self):
        self.assertIsNone(kick.linked())
        kick.link_finish("code", self.state())
        self.assertEqual(kick.linked(), "ben")
        self.assertIn("code_verifier=", self.sent[0].data.decode())
        kick.unlink()
        self.assertIsNone(kick.linked())

    def test_unknown_or_reused_state_is_refused(self):
        with self.assertRaises(ValueError):
            kick.link_finish("code", "nope")
        s = self.state()
        kick.link_finish("code", s)
        with self.assertRaises(ValueError):
            kick.link_finish("code", s)  # one use only

    def test_cant_link_without_redirect(self):
        kick.REDIRECT = ""
        self.assertFalse(kick.can_link())


class NoFollow(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class TlsTest(unittest.TestCase):
    def test_forwards_only_the_login_fields(self):
        srv = tlscallback.make_server("https://127.0.0.1:0/auth/kick/callback", 8765, tempfile.mkdtemp())
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        op = urllib.request.build_opener(NoFollow, urllib.request.HTTPSHandler(context=ctx))
        base = f"https://127.0.0.1:{srv.server_address[1]}"
        with self.assertRaises(urllib.error.HTTPError) as e:
            op.open(base + "/auth/kick/callback?code=c&state=s&evil=1")
        self.assertEqual(e.exception.code, 303)
        self.assertEqual(e.exception.headers["Location"], "http://127.0.0.1:8765/auth/kick/done?code=c&state=s")
        import socket
        idle = socket.create_connection(("127.0.0.1", srv.server_address[1]))  # connects, then says nothing
        self.addCleanup(idle.close)
        with self.assertRaises(urllib.error.HTTPError) as e:
            op.open(base + "/auth/kick/callback?code=c&state=s")  # still answered while the idle one waits
        self.assertEqual(e.exception.code, 303)
        with self.assertRaises(urllib.error.HTTPError) as e:
            op.open(base + "/other")
        self.assertEqual(e.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
