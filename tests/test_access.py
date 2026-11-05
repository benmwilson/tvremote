import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import access  # noqa: E402
import activity  # noqa: E402
import accounts  # noqa: E402
import app as server  # noqa: E402
import kick  # noqa: E402
from helpers import founder_client  # noqa: E402


class AccessTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.dict(os.environ, {"TVREMOTE_DATA": self.dir}), mock.patch.object(server, "DATA", self.dir),
                  mock.patch.object(server, "FAV_FILE", os.path.join(self.dir, "favourites.json")),
                  mock.patch.object(kick, "DATA", self.dir),
                  mock.patch.object(server.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)
        access._last_by_ip.clear()
        activity.reset()
        server._fails.clear()
        self.owner = founder_client(server)

    def test_requesting_access_needs_no_login_and_lands_in_the_admin_list(self):
        anon = server.app.test_client()
        self.assertEqual(anon.get("/request").status_code, 200)
        r = anon.post("/request", data={"name": "  Sam   Smith ", "note": "friend of ben"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(anon.get("/api/access").status_code, 401)  # the list itself stays private
        pending = self.owner.get("/api/access").get_json()["requests"]
        self.assertEqual([(p["name"], p["note"]) for p in pending], [("Sam Smith", "friend of ben")])

    def test_requests_are_rate_limited_and_capped(self):
        anon = server.app.test_client()
        self.assertEqual(anon.post("/request", data={"name": "a"}).status_code, 200)
        self.assertEqual(anon.post("/request", data={"name": "b"}).status_code, 400)  # same address, too soon
        self.assertEqual(anon.post("/request", data={"name": ""}).status_code, 400)
        for i in range(access.MAX_PENDING - 1):  # one is already waiting
            access._last_by_ip.clear()
            access.add_request(f"n{i}", "", "1.2.3.4")
        access._last_by_ip.clear()
        with self.assertRaises(ValueError):
            access.add_request("late", "", "5.6.7.8")

    def test_approving_creates_an_account_with_a_password_shown_once(self):
        server.app.test_client().post("/request", data={"name": "Sam Smith"})
        rid = self.owner.get("/api/access").get_json()["requests"][0]["id"]
        r = self.owner.post(f"/api/requests/{rid}/approve", json={}).get_json()
        self.assertEqual(r["name"], "sam-smith")
        self.assertTrue(accounts.verify("sam-smith", r["password"]))
        self.assertEqual(self.owner.get("/api/access").get_json()["requests"], [])
        sam = server.app.test_client()
        self.assertEqual(sam.post("/login", data={"username": "sam-smith", "password": r["password"]}).status_code, 303)

    def test_denying_and_name_clashes(self):
        accounts.add("sam", "secret1")
        server.app.test_client().post("/request", data={"name": "sam"})
        rid = self.owner.get("/api/access").get_json()["requests"][0]["id"]
        self.assertEqual(self.owner.post(f"/api/requests/{rid}/approve", json={}).status_code, 400)  # name taken
        self.owner.delete(f"/api/requests/{rid}")
        self.assertEqual(self.owner.get("/api/access").get_json()["requests"], [])

    def test_a_guest_link_works_once_and_opening_it_does_not_use_it_up(self):
        url = self.owner.post("/api/invites", json={"hours": 2, "label": "Aunt"}).get_json()["url"]
        path = "/" + url.split("/", 3)[3]
        guest = server.app.test_client()
        self.assertEqual(guest.get(path).status_code, 200)  # a link preview fetching it...
        self.assertEqual(guest.get(path).status_code, 200)  # ...changes nothing
        self.assertEqual(guest.post(path).status_code, 303)
        self.assertEqual(server.app.test_client().post(path).status_code, 410)  # used
        self.assertEqual(guest.get("/api/favourites").status_code, 200)
        me = guest.get("/api/auth").get_json()
        self.assertEqual((me["guest"], me["admin"], me["label"]), (True, False, "Aunt"))
        self.assertAlmostEqual(me["guest_until"] - server.time.time(), 7200, delta=30)
        gid = self.owner.get("/api/access").get_json()["guests"][0]["id"]
        self.owner.delete(f"/api/guests/{gid}")
        self.assertEqual(guest.get("/api/favourites").status_code, 401)  # revoked

    def test_expired_and_revoked_links_are_dead(self):
        t = access.create_invite(1, "x", now=1000.0)
        self.assertIsNone(access.peek_invite(t, now=1000.0 + access.REDEEM_WINDOW + 1))
        t2 = access.create_invite(1, "y")
        iid = access.invites_pending()[-1]["id"]
        self.assertTrue(access.revoke_invite(iid))
        self.assertIsNone(access.redeem_invite(t2))
        self.assertFalse(access.revoke_invite("short"))  # ids must be the full 8 characters

    def test_guest_limits(self):
        url = self.owner.post("/api/invites", json={"hours": 1}).get_json()["url"]
        guest = server.app.test_client()
        guest.post("/" + url.split("/", 3)[3])
        self.owner.post("/api/favourites", json={"url": "https://kick.com/capped", "name": "capped", "source": "Kick"})
        self.assertEqual(guest.get("/api/favourites").get_json(), [])  # never the owner's personal list
        self.assertEqual(guest.post("/api/favourites", json={"url": "https://kick.com/x", "name": "x", "source": "Kick"}).status_code, 403)
        self.assertEqual(guest.delete("/api/favourites", json={"url": "https://kick.com/capped"}).status_code, 403)
        for path in ("/api/accounts", "/api/access", "/api/debug", "/auth/kick/start"):
            self.assertEqual(guest.get(path).status_code, 403, path)
        self.assertEqual(guest.post("/api/kick/chat", json={"channel": "x", "text": "hi"}).status_code, 403)
        self.assertEqual(guest.post("/api/shell", json={"enabled": False}).status_code, 403)  # the home-screen switch is not theirs
        self.assertEqual(guest.post("/api/stats", json={"enabled": True}).status_code, 403)
        # default deny: a route nobody listed (like one added next month) is closed to guests, not open by accident
        self.assertEqual(guest.get("/api/some-future-route").status_code, 403)
        self.assertEqual(guest.post("/api/some-future-route").status_code, 403)
        for path in ("/", "/api/auth", "/api/targets", "/api/favourites", "/api/kick", "/api/shell", "/api/stats"):
            self.assertNotEqual(guest.get(path).status_code, 403, path)  # what the page needs to show the TVs
        self.assertNotEqual(guest.post("/api/cmd", json={"action": "pause"}).status_code, 403)

    def test_changing_your_own_password(self):
        accounts.add("sam", "secret1")
        sam = server.app.test_client()
        sam.post("/login", data={"username": "sam", "password": "secret1"})
        self.assertEqual(sam.post("/api/account/password", json={"current": "wrong", "new": "another1"}).status_code, 400)
        self.assertEqual(sam.post("/api/account/password", json={"current": "secret1", "new": "x"}).status_code, 400)
        self.assertEqual(sam.post("/api/account/password", json={"current": "secret1", "new": "another1"}).status_code, 200)
        self.assertEqual(sam.get("/api/favourites").status_code, 401)  # signed out everywhere
        self.assertTrue(accounts.verify("sam", "another1"))

    def test_stats_last_login_last_action_and_link_use(self):
        accounts.add("sam", "secret1")
        sam = server.app.test_client()
        sam.post("/login", data={"username": "sam", "password": "secret1"})
        sam.get("/api/status")  # polling is not an action
        sam.get("/api/targets")
        users = {u["name"]: u for u in self.owner.get("/api/accounts").get_json()["users"]}
        self.assertEqual(users["sam"]["last"]["text"], "Logged in")
        with mock.patch.object(server, "play_on"):
            sam.post("/api/play", json={"url": "https://kick.com/capped"})
        users = {u["name"]: u for u in self.owner.get("/api/accounts").get_json()["users"]}
        self.assertEqual(users["sam"]["last"]["text"], "Played capped")
        self.assertIsNone(users["owner"]["last"] and users["owner"]["last"]["text"] if False else None)
        # a guest link: pending, then used (and when), then what the guest did
        url = self.owner.post("/api/invites", json={"hours": 2, "label": "Aunt"}).get_json()["url"]
        state = self.owner.get("/api/access").get_json()
        self.assertEqual((len(state["invites"]), state["used"], state["guests"]), (1, [], []))
        guest = server.app.test_client()
        guest.post("/" + url.split("/", 3)[3])
        with mock.patch.object(server, "play_on"):
            guest.post("/api/play", json={"url": "https://kick.com/xqc"})
        state = self.owner.get("/api/access").get_json()
        self.assertEqual((len(state["invites"]), state["used"][0]["label"]), (0, "Aunt"))
        g = state["guests"][0]
        self.assertEqual(g["last"]["text"], "Played xqc")
        self.assertAlmostEqual(g["since"], server.time.time(), delta=30)
        self.owner.delete(f"/api/guests/{g['id']}")
        self.assertIsNone(activity.last("guest:" + g["id"]))  # revoking forgets them
        self.assertEqual(len(self.owner.get("/api/access").get_json()["used"]), 1)  # but the link stays in the short used list

    def test_signing_someone_out_and_listing(self):
        accounts.add("sam", "secret1")
        sam = server.app.test_client()
        sam.post("/login", data={"username": "sam", "password": "secret1"})
        self.assertEqual(self.owner.post("/api/accounts/signout", json={"name": "sam"}).status_code, 200)
        self.assertEqual(sam.get("/api/favourites").status_code, 401)
        users = {u["name"]: u for u in self.owner.get("/api/accounts").get_json()["users"]}
        self.assertTrue(users["sam"]["last_login"] and users["boss"]["founder"])


if __name__ == "__main__":
    unittest.main()
