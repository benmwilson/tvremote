import os
import threading
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import accounts  # noqa: E402
import app as server  # noqa: E402
import kick  # noqa: E402
import sessions  # noqa: E402
import throttle  # noqa: E402
from helpers import founder_client  # noqa: E402


class Security(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.dict(os.environ, {"TVREMOTE_DATA": self.dir}), mock.patch.object(server, "DATA", self.dir),
                  mock.patch.object(server, "FAV_FILE", os.path.join(self.dir, "favourites.json")),
                  mock.patch.object(server, "SETTINGS_FILE", os.path.join(self.dir, "settings.json")),
                  mock.patch.object(kick, "DATA", self.dir), mock.patch.dict(sessions.key_cache, {"v": None}),
                  mock.patch.object(server, "CAST", None), mock.patch.object(server.time, "sleep"), mock.patch.object(server, "HOME", None)):
            p.start()
            self.addCleanup(p.stop)
        throttle.fails.clear()
        self.admin = founder_client(server)
        accounts.add("sam", "secret1")
        self.user = server.app.test_client()
        self.user.post("/login", data={"username": "sam", "password": "secret1"})
        url = self.admin.post("/api/invites", json={"hours": 1}).get_json()["url"]
        self.guest = server.app.test_client()
        self.guest.post("/" + url.split("/", 3)[3])

    # ---- headers and limits ----
    def test_every_response_carries_the_basic_security_headers(self):
        for client, path in ((self.admin, "/"), (self.admin, "/api/auth"), (server.app.test_client(), "/login")):
            h = client.get(path).headers
            self.assertEqual(h.get("X-Content-Type-Options"), "nosniff", path)
            self.assertEqual(h.get("X-Frame-Options"), "DENY", path)  # the remote can't be framed by another site
            self.assertEqual(h.get("Referrer-Policy"), "same-origin", path)

    def test_api_answers_are_not_cached(self):
        self.assertIn("no-store", self.admin.get("/api/auth").headers.get("Cache-Control", ""))

    def test_an_oversized_body_is_refused(self):
        r = self.admin.post("/api/config", data=b"x" * 2_000_000, content_type="application/json")
        self.assertEqual(r.status_code, 413)

    # ---- least privilege ----
    def test_only_an_admin_can_change_the_tvs_home_screen(self):
        self.assertEqual(self.user.post("/api/shell", json={"qr": False}).status_code, 403)
        self.assertEqual(self.guest.post("/api/shell", json={"qr": False}).status_code, 403)
        self.assertEqual(self.admin.post("/api/shell", json={"qr": False}).status_code, 200)

    def test_every_admin_endpoint_refuses_users_and_guests(self):
        admin_routes = [("get", "/api/access"), ("get", "/api/accounts"), ("get", "/api/config"), ("post", "/api/config"), ("get", "/api/debug"),
                        ("get", "/api/discover"), ("get", "/api/tvs"), ("post", "/api/tvs"), ("get", "/api/screen.png"), ("get", "/api/shell/screenshot"),
                        ("post", "/api/targets/test"), ("post", "/api/stats"), ("post", "/api/invites"), ("post", "/api/accounts")]
        for method, path in admin_routes:
            for who, c in (("user", self.user), ("guest", self.guest)):
                r = getattr(c, method)(path, json={}) if method == "post" else getattr(c, method)(path)
                self.assertEqual(r.status_code, 403, f"{who} {method} {path}")

    def test_nothing_but_the_chromecast_relay_and_the_login_pages_work_without_a_login(self):
        anon = server.app.test_client()
        for path in ("/api/status", "/api/targets", "/api/watch", "/api/detect?name=abc", "/api/config", "/api/tvs", "/api/wall", "/api/favourites"):
            r = anon.get(path, headers={"Accept": "application/json"}, environ_overrides={"REMOTE_ADDR": "192.168.1.50"})  # not the container's own healthcheck
            self.assertIn(r.status_code, (401, 403, 405), path)

    # ---- a guest's links can't reach into the home network ----
    def test_a_guest_can_only_play_kick_twitch_and_youtube(self):
        with mock.patch.object(server, "play_on") as play:
            for bad in ("http://127.0.0.1:8765/api/status", "http://192.168.1.128/video.mp4", "http://[::1]/x", "https://localhost/x",
                        "https://vimeo.com/123", "https://kick.com.evil.example/x", "https://evilkick.com/x", "ftp://kick.com/x", "file:///etc/passwd",
                        "http://kick.com@192.168.1.1/x"):
                r = self.guest.post("/api/play", json={"url": bad})
                self.assertIn(r.status_code, (400, 403), bad)  # 400: not even a web link; 403: a web link a guest may not play
            play.assert_not_called()
            for good in ("https://kick.com/capped", "https://www.twitch.tv/xqc", "https://m.youtube.com/watch?v=abc", "https://youtu.be/abc"):
                self.assertEqual(self.guest.post("/api/play", json={"url": good}).status_code, 200, good)
            for _ in range(60):  # the start runs in the background now
                if play.call_count >= 4:
                    break
                threading.Event().wait(0.05)
            self.assertEqual(play.call_count, 4)

    def test_a_guests_background_play_is_not_remembered_but_a_users_is(self):
        done = threading.Event()
        with mock.patch.object(server, "start_player", side_effect=lambda *a, **k: done.set()), mock.patch.object(server, "switch_input"),                 mock.patch.object(server, "resolve", return_value=("Kick", "streamlink")),                 mock.patch.object(server, "RECENT_FILE", os.path.join(self.dir, "recents.json")):
            self.guest.post("/api/play", json={"url": "https://kick.com/guestpick"})
            self.assertTrue(done.wait(3))
            self.assertEqual(server.load_recents(), [])  # the start runs in a thread, which must still know a guest asked
            done.clear()
            server._start_seq.clear()
            self.user.post("/api/play", json={"url": "https://kick.com/userpick"})
            self.assertTrue(done.wait(3))
            self.assertEqual([r["url"] for r in server.load_recents()], ["https://kick.com/userpick"])

    def test_a_guest_cannot_get_there_through_the_shortcut_or_move_either(self):
        with mock.patch.object(server, "play_on") as play:
            self.guest.get("/play?u=http://nas.local/video.mp4")
            self.assertEqual(self.guest.post("/api/move", json={"from": "living", "to": "bedroom", "url": "http://nas.local/v.mp4"}).status_code, 403)
        play.assert_not_called()

    def test_a_regular_user_may_still_play_from_the_home_network(self):
        with mock.patch.object(server, "play_on") as play:
            self.assertEqual(self.user.post("/api/play", json={"url": "http://192.168.1.128/video.mp4"}).status_code, 200)
        play.assert_called_once()  # a movie on the NAS is a fair thing to play


if __name__ == "__main__":
    unittest.main()


class ShellStateByRole(unittest.TestCase):
    """The TV screen's settings (shortcut links, which guest code is showing) are for admins; everyone else gets the bare state."""

    def test_a_non_admin_gets_only_the_bare_screen_state(self):
        import app as server
        full = {"enabled": True, "running": True, "qr": True, "error": None, "shortcut_url": "shortcuts://x", "install_url": "https://i", "qr_guest_id": "abcd1234", "vo": "v"}
        with mock.patch.object(server, "shell_state", return_value=full), mock.patch.object(server.accounts, "role", return_value="user"):
            with server.app.test_request_context("/api/shell"):
                server.g.user = "someone"
                body = server.api_shell().get_json()
        self.assertEqual(set(body), {"enabled", "running", "qr", "error"})

    def test_an_admin_gets_everything(self):
        import app as server
        full = {"enabled": True, "running": True, "qr": True, "shortcut_url": "shortcuts://x"}
        with mock.patch.object(server, "shell_state", return_value=full), mock.patch.object(server.accounts, "role", return_value="admin"):
            with server.app.test_request_context("/api/shell"):
                server.g.user = "someone"
                self.assertEqual(server.api_shell().get_json(), full)
