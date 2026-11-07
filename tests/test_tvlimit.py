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


class ReinstateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.dict(os.environ, {"TVREMOTE_DATA": self.dir}), mock.patch.object(server, "DATA", self.dir)):
            p.start(); self.addCleanup(p.stop)
        throttle.fails.clear()
        server.sessions.revoked.clear()
        self.admin = founder_client(server)

    def test_a_revoked_guest_can_be_reinstated_with_the_same_login(self):
        url = self.admin.post("/api/invites", json={"hours": 2, "label": "Pat"}).get_json()["url"]
        guest = server.app.test_client()
        guest.post("/join/" + url.rsplit("/join/", 1)[1])
        self.assertEqual(guest.get("/api/auth").get_json()["guest"], True)
        gid = self.admin.get("/api/access").get_json()["guests"][0]["id"]
        entry = self.admin.get("/api/access").get_json()["guests"][0]["entry"]
        self.admin.delete("/api/guests/" + gid)
        self.assertFalse(guest.get("/api/auth", headers={"Accept": "application/json"}).get_json(silent=True) and guest.get("/api/auth").get_json().get("guest"))  # signed out
        used = self.admin.get("/api/access").get_json()["used"]
        self.assertTrue(next(u for u in used if u["id"] == entry)["can_reinstate"])
        self.assertEqual(self.admin.post("/api/guests/" + entry + "/reinstate").status_code, 200)
        self.assertEqual(guest.get("/api/auth").get_json()["guest"], True)  # the same phone, no new link
        self.assertEqual(self.admin.post("/api/guests/" + entry + "/reinstate").status_code, 404)  # nothing left to bring back


class ResetAccountTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.dict(os.environ, {"TVREMOTE_DATA": self.dir}), mock.patch.object(server, "DATA", self.dir)):
            p.start(); self.addCleanup(p.stop)
        throttle.fails.clear()
        self.admin = founder_client(server)

    def test_a_reset_clears_their_data_and_keeps_the_login(self):
        accounts.add("sam", "pass-word1", "user", tv_ids=["bedroom"])
        accounts.mark_tour("sam")
        fav = Path(server.fav_file("sam"))
        with mock.patch.object(server, "fav_file", return_value=str(Path(self.dir) / "favourites.sam.json")):
            (Path(self.dir) / "favourites.sam.json").write_text("[{\"url\": \"https://kick.com/x\"}]")
            self.assertEqual(self.admin.post("/api/accounts/reset", json={"name": "sam"}).status_code, 200)
            self.assertFalse((Path(self.dir) / "favourites.sam.json").exists())
        self.assertFalse(accounts.tour_seen("sam"))  # the welcome comes back
        self.assertEqual(accounts.tvs("sam"), ["bedroom"])  # the limit stays
        self.assertTrue(accounts.verify("sam", "pass-word1"))  # and so does the password
        self.assertEqual(self.admin.post("/api/accounts/reset", json={"name": "boss"}).status_code, 400)  # not yourself
        self.assertEqual(self.admin.post("/api/accounts/reset", json={"name": "ghost"}).status_code, 404)


class SeparateAccountsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.dict(os.environ, {"TVREMOTE_DATA": self.dir}), mock.patch.object(server, "DATA", self.dir),
                  mock.patch.object(server, "RECENT_FILE", os.path.join(self.dir, "recents.json"))):
            p.start(); self.addCleanup(p.stop)
        throttle.fails.clear()
        self.admin = founder_client(server)

    def test_what_one_account_played_is_not_in_anothers_recents(self):
        (Path(self.dir) / "recents.json").write_text('[{"name": "old", "url": "https://kick.com/old", "source": "Kick"}]')  # the list that used to be shared
        accounts.add("sam", "pass-word1", "user")
        sam = server.app.test_client()
        sam.post("/login", data={"username": "sam", "password": "pass-word1"})
        self.assertEqual(sam.get("/api/recents").get_json(), [])  # nothing of the main account's
        self.assertEqual([r["url"] for r in self.admin.get("/api/recents").get_json()], ["https://kick.com/old"])  # it moved to the founder
        self.assertFalse((Path(self.dir) / "recents.json").exists())
        with server.app.test_request_context("/api/play", method="POST"):
            server.g.user = "sam"
            server.remember("https://kick.com/sampick")
        self.assertEqual([r["url"] for r in sam.get("/api/recents").get_json()], ["https://kick.com/sampick"])
        self.assertNotIn("sampick", str(self.admin.get("/api/recents").get_json()))
        self.admin.post("/api/accounts/reset", json={"name": "sam"})
        self.assertEqual(sam.get("/api/recents", headers={"Accept": "application/json"}).status_code in (200, 302, 401, 403), True)
        self.assertEqual(server.load_recents("sam"), [])  # a reset clears them
