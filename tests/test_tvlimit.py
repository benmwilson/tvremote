import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import access  # noqa: E402
import accounts  # noqa: E402
import app as server  # noqa: E402
import throttle  # noqa: E402
from helpers import founder_client  # noqa: E402


class TvLimitTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.dict(os.environ, {"TVREMOTE_DATA": self.dir}), mock.patch.object(server, "DATA", self.dir)):
            p.start(); self.addCleanup(p.stop)
        throttle.fails.clear()
        self.admin = founder_client(server)

    def login(self, name, pw="pass-word1"):
        c = server.app.test_client()
        self.assertEqual(c.post("/login", data={"username": name, "password": pw}).status_code, 303)
        return c

    def test_a_limited_account_can_only_reach_its_tvs(self):
        accounts.add("sam", "pass-word1", "user", tv_ids=["bedroom"])
        c = self.login("sam")
        self.assertEqual(c.get("/api/sleep?target=living").status_code, 403)
        self.assertEqual(c.get("/api/sleep").status_code, 403)  # no target means the Pi's TV, which is not one of hers
        self.assertNotEqual(c.get("/api/sleep?target=bedroom").status_code, 403)
        self.assertEqual(c.post("/api/move", json={"from": "bedroom", "to": "living", "url": "https://kick.com/x"}).status_code, 403)
        self.assertEqual(c.post("/api/launcher/key", json={"key": "down"}).status_code, 403)

    def test_the_tv_list_shows_only_the_allowed_tvs(self):
        accounts.add("sam", "pass-word1", "user", tv_ids=["bedroom"])
        ids = lambda c: {t["id"] for t in c.get("/api/targets").get_json()}
        self.assertNotIn("living", ids(self.login("sam")))
        self.assertIn("living", ids(self.admin))

    def test_admins_and_unlimited_accounts_are_not_limited_and_a_password_change_keeps_the_limit(self):
        accounts.add("lee", "pass-word1", "user")
        self.assertNotEqual(self.login("lee").get("/api/sleep?target=living").status_code, 403)
        accounts.add("sam", "pass-word1", "user", tv_ids=["bedroom"])
        accounts.add("sam", "other-pass1")  # a new password
        self.assertEqual(accounts.tvs("sam"), ["bedroom"])
        accounts.add("ann", "pass-word1", "admin", tv_ids=["bedroom"])
        self.assertIsNone(accounts.tvs("ann"))  # an admin is never limited

    def test_an_admin_can_change_an_accounts_tvs(self):
        accounts.add("sam", "pass-word1", "user")
        r = self.admin.post("/api/accounts/tvs", json={"name": "sam", "tvs": ["bedroom", "nonsense"]}).get_json()
        self.assertEqual(accounts.tvs("sam"), ["bedroom"])
        self.admin.post("/api/accounts/tvs", json={"name": "sam", "tvs": []})
        self.assertIsNone(accounts.tvs("sam"))
        self.assertEqual(self.admin.post("/api/accounts/tvs", json={"name": "boss", "tvs": ["bedroom"]}).status_code, 400)
        self.assertEqual(self.login("sam").post("/api/accounts/tvs", json={"name": "sam", "tvs": []}).status_code, 403)  # not for the account itself

    def test_a_guest_link_limited_to_a_tv_makes_a_session_limited_to_it(self):
        url = self.admin.post("/api/invites", json={"hours": 2, "label": "Pat", "tvs": ["bedroom"]}).get_json()["url"]
        token = url.rsplit("/join/", 1)[1]
        g = server.app.test_client()
        self.assertEqual(g.post("/join/" + token).status_code, 303)
        self.assertEqual(g.get("/api/sleep?target=living").status_code, 403)
        self.assertNotEqual(g.get("/api/sleep?target=bedroom").status_code, 403)
        self.assertEqual(access.invites_pending(), [])  # (used up: one device)

    def test_creating_an_account_limited_to_a_tv_keeps_the_limit_and_a_bad_list_is_refused(self):
        r = self.admin.post("/api/accounts", json={"name": "ivy", "password": "pass-word1", "role": "user", "tvs": ["bedroom"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(accounts.tvs("ivy"), ["bedroom"])
        self.assertEqual(self.admin.post("/api/accounts", json={"name": "bob", "password": "pass-word1", "tvs": ["nonsense"]}).status_code, 400)
        self.assertFalse(accounts.exists("bob"))  # refused, not made with every TV
        self.assertEqual(self.admin.post("/api/invites", json={"hours": 1, "tvs": ["nonsense"]}).status_code, 400)

    def test_a_tv_lookup_outside_the_path_list_is_still_refused(self):
        accounts.add("sam", "pass-word1", "user", tv_ids=["bedroom"])
        c = self.login("sam")
        self.assertEqual(c.get("/api/chat/feed?target=living").status_code, 403)
        self.assertEqual(c.get("/api/roku?target=living").status_code, 403)

    def test_an_approved_request_can_be_limited(self):
        rid = access.add_request("Kim", "hi", "1.2.3.4")
        r = self.admin.post(f"/api/requests/{rid}/approve", json={"tvs": ["bedroom"], "password": "kim-pass1"}).get_json()
        self.assertEqual(accounts.tvs(r["name"]), ["bedroom"])


if __name__ == "__main__":
    unittest.main()


class NoticesTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.dict(os.environ, {"TVREMOTE_DATA": self.dir}), mock.patch.object(server, "DATA", self.dir)):
            p.start(); self.addCleanup(p.stop)
        throttle.fails.clear()
        self.admin = founder_client(server)

    def test_the_bell_counts_requests_for_admins_only(self):
        self.assertEqual(self.admin.get("/api/notices").get_json(), [])
        access._last_by_ip.clear()
        access.add_request("Kim", "hi", "5.6.7.8")
        n = self.admin.get("/api/notices").get_json()
        self.assertEqual([(x["id"], x["tab"], x["count"]) for x in n], [("requests", "people", 1)])
        accounts.add("sam", "pass-word1", "user")
        c = server.app.test_client()
        c.post("/login", data={"username": "sam", "password": "pass-word1"})
        self.assertEqual(c.get("/api/notices").get_json(), [])  # not theirs to act on
