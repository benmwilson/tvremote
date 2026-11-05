import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import accounts  # noqa: E402
import activity  # noqa: E402
import app as server  # noqa: E402
import kick  # noqa: E402
from helpers import founder_client  # noqa: E402


class SetupBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.dict(os.environ, {"TVREMOTE_DATA": self.dir}), mock.patch.object(server, "DATA", self.dir),
                  mock.patch.object(server, "FAV_FILE", os.path.join(self.dir, "favourites.json")),
                  mock.patch.object(kick, "DATA", self.dir), mock.patch.dict(server._key, {"v": None}),
                  mock.patch.object(server.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)
        activity.reset()
        server._fails.clear()

    def create(self, c, name="ben", password="hunter22", **extra):
        return c.post("/api/setup/admin", json={"code": server.SETUP_CODE, "name": name, "password": password, **extra})


class FreshInstall(SetupBase):
    """Nothing set up: nobody can change anything until the wizard has made the first admin."""

    def test_the_wizard_is_needed_and_nothing_can_change_until_an_admin_exists(self):
        c = server.app.test_client()
        st = c.get("/api/setup").get_json()
        self.assertEqual((st["needed"], st["fresh"], st["has_admin"], st["needs_code"]), (True, True, False, True))
        for path in ("/", "/api/auth", "/api/setup"):
            self.assertEqual(c.get(path).status_code, 200, path)  # the wizard's own pages
        # everything else is shut, reading as well as changing (an upgrade's older data must not be readable meanwhile)
        for method, path in (("get", "/api/favourites"), ("get", "/api/screen.png"), ("get", "/api/targets"), ("get", "/auth/kick/start"),
                             ("get", "/play"), ("get", "/api/access"), ("post", "/api/accounts"), ("post", "/api/invites"),
                             ("post", "/api/favourites"), ("post", "/api/cmd"), ("post", "/api/setup/finish"), ("post", "/api/setup/claim")):
            self.assertEqual(getattr(c, method)(path, json={"name": "evil", "password": "hunter22", "role": "admin"}).status_code, 403, path)
        self.assertEqual(c.get("/api/status", environ_base={"REMOTE_ADDR": "127.0.0.1"}).status_code, 200)  # the container's health check
        self.assertEqual(c.get("/api/status", environ_base={"REMOTE_ADDR": "192.168.1.9"}).status_code, 403)
        self.assertEqual(c.get("/share", headers={"Accept": "text/html"}).status_code, 303)  # a page you can't see yet goes to the wizard
        self.assertFalse(accounts.exists("evil"))  # in particular, no way round the setup code

    def test_creating_the_admin_logs_them_in_and_locks_the_door(self):
        c = server.app.test_client()
        r = self.create(c, "Ben")
        self.assertEqual(r.get_json(), {"ok": True, "name": "ben"})
        self.assertIn("tv_auth", r.headers["Set-Cookie"])
        self.assertEqual(c.get("/api/favourites").status_code, 200)  # the new admin is in
        self.assertEqual(c.get("/api/auth").get_json()["user"], "ben")
        stranger = server.app.test_client()
        self.assertEqual(stranger.get("/api/favourites").status_code, 401)  # everyone else now needs to log in
        self.assertEqual(self.create(stranger, "evil").status_code, 401)
        self.assertEqual(stranger.get("/api/setup").status_code, 401)
        self.assertFalse(accounts.exists("evil"))
        st = c.get("/api/setup").get_json()
        self.assertEqual((st["needed"], st["has_admin"]), (True, True))  # the wizard goes on to step 2 until it is finished
        self.assertEqual(c.post("/api/setup/finish").status_code, 200)
        self.assertFalse(c.get("/api/setup").get_json()["needed"])

    def test_a_script_cannot_walk_in_with_an_empty_password(self):
        self.create(server.app.test_client())
        c = server.app.test_client()
        for creds in (b":", b"ben:", b"ben:wrong", b"owner:", b":hunter22"):
            self.assertEqual(c.get("/api/favourites", headers={"Authorization": "Basic " + base64.b64encode(creds).decode()}).status_code, 401, creds)
        ok = c.get("/api/favourites", headers={"Authorization": "Basic " + base64.b64encode(b"ben:hunter22").decode()})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(c.post("/login", data={"username": "", "password": ""}).status_code, 401)

    def test_the_first_admin_is_the_owner_and_the_last_admin_cannot_be_removed(self):
        c = server.app.test_client()
        self.create(c)
        users = c.get("/api/accounts").get_json()["users"]
        self.assertEqual([(u["name"], u["founder"]) for u in users], [("ben", True)])
        self.assertEqual(c.patch("/api/accounts", json={"name": "ben", "role": "user"}).status_code, 400)  # the only admin
        self.assertEqual(c.delete("/api/accounts", json={"name": "ben"}).status_code, 400)
        c.post("/api/accounts", json={"name": "amy", "password": "secret1", "role": "admin"})
        amy = server.app.test_client()
        amy.post("/login", data={"username": "amy", "password": "secret1"})
        self.assertEqual(amy.delete("/api/accounts", json={"name": "ben"}).status_code, 403)  # an admin can't remove the owner
        self.assertEqual(amy.post("/api/accounts", json={"name": "ben", "password": "hijack1"}).status_code, 403)
        self.assertTrue(accounts.verify("ben", "hunter22"))
        self.assertEqual(c.delete("/api/accounts", json={"name": "amy"}).status_code, 200)

    def test_the_setup_code_host_and_throttle_guard_the_first_admin(self):
        c = server.app.test_client()
        bad = c.post("/api/setup/admin", json={"name": "ben", "password": "hunter22", "code": "nope"})
        self.assertEqual(bad.status_code, 403)
        self.assertEqual(c.post("/api/setup/admin", json={"name": "ben", "password": "hunter22"}).status_code, 403)  # no code at all
        self.assertFalse(accounts.exists("ben"))
        body = {"name": "ben", "password": "hunter22", "code": server.SETUP_CODE}
        # a public name pointing at this machine (DNS rebinding) is refused even with the right code
        self.assertEqual(c.post("/api/setup/admin", json=body, headers={"Host": "evil.example.com"}).status_code, 403)
        self.assertFalse(accounts.exists("ben"))
        self.assertEqual(c.post("/api/setup/admin", json=body, headers={"Host": "192.168.1.138:8765"}).status_code, 200)

    def test_guessing_the_code_is_throttled_per_address_and_overall(self):
        c = server.app.test_client()
        for i in range(10):
            c.post("/api/setup/admin", json={"name": "ben", "password": "hunter22", "code": f"{i:06x}"})
        right = c.post("/api/setup/admin", json={"name": "ben", "password": "hunter22", "code": server.SETUP_CODE})
        self.assertEqual(right.status_code, 429)  # even the right code waits out the lockout
        self.assertFalse(accounts.exists("ben"))
        server._fails.pop("127.0.0.1", None)
        server._fails.pop("127.0.0.1|setup", None)  # a different address, same overall count: still held
        self.assertEqual(c.post("/api/setup/admin", json={"name": "ben", "password": "hunter22", "code": server.SETUP_CODE}).status_code, 429)

    def test_an_unreadable_accounts_file_never_reopens_the_install(self):
        self.create(server.app.test_client())
        with open(os.path.join(self.dir, "users.json"), "w") as f:
            f.write("{ this is not json")  # damaged
        c = server.app.test_client()
        self.assertEqual(c.get("/api/favourites").status_code, 401)  # still closed
        self.assertEqual(self.create(c, "evil").status_code, 401)

    def test_failed_script_logins_are_throttled_per_account_and_a_success_does_not_wipe_other_counts(self):
        self.create(server.app.test_client())
        accounts.add("sam", "secret1")
        c = server.app.test_client()
        bad = {"Authorization": "Basic " + base64.b64encode(b"ben:wrong").decode()}
        self.assertEqual([c.get("/api/favourites", headers=bad).status_code for _ in range(10)], [401] * 10)
        self.assertEqual(c.get("/api/favourites", headers=bad).status_code, 429)
        good = {"Authorization": "Basic " + base64.b64encode(b"ben:hunter22").decode()}
        self.assertEqual(c.get("/api/favourites", headers=good).status_code, 429)  # locked out for now, whatever is sent
        # logging into a different account doesn't reset the count against this one
        server.app.test_client().post("/login", data={"username": "sam", "password": "secret1"})
        self.assertEqual(server.app.test_client().get("/api/favourites", headers=bad).status_code, 429)

    def test_the_throttle_keeps_its_memory_small_and_buckets_ipv6(self):
        self.assertEqual(server._bucket("2001:db8::1"), server._bucket("2001:db8::ffff"))  # one /64 is one address
        self.assertNotEqual(server._bucket("2001:db8::1"), server._bucket("2001:db9::1"))
        self.assertEqual(server._bucket("192.168.1.5"), "192.168.1.5")
        self.assertFalse(server._throttled("10.0.0.9", "x"))
        self.assertNotIn("10.0.0.9", server._fails)  # asking about an address doesn't make an entry for it

    def test_bad_names_and_passwords_do_not_end_setup(self):
        c = server.app.test_client()
        self.assertEqual(self.create(c, "owner").status_code, 400)
        self.assertEqual(self.create(c, "ben", "x").status_code, 400)
        self.assertEqual(c.get("/api/setup").get_json()["fresh"], True)  # still open: nobody was locked out by a typo


class AfterSetup(SetupBase):
    def test_a_normal_user_is_never_prompted(self):
        boss = founder_client(server)
        boss.post("/api/accounts", json={"name": "sam", "password": "secret1"})
        sam = server.app.test_client()
        sam.post("/login", data={"username": "sam", "password": "secret1"})
        self.assertEqual(sam.get("/api/setup").get_json(), {"needed": False})
        self.assertEqual(sam.post("/api/setup/admin", json={"name": "x1", "password": "hunter22"}).status_code, 403)

    def test_setup_never_overwrites_an_account_or_lets_a_plain_admin_in(self):
        owner = founder_client(server)
        owner.post("/api/accounts", json={"name": "amy", "password": "secret1", "role": "admin"})
        owner.post("/api/accounts", json={"name": "bo", "password": "secret1", "role": "admin"})
        self.assertEqual(owner.post("/api/setup/admin", json={"name": "bo", "password": "hijack1"}).status_code, 400)  # exists
        self.assertTrue(accounts.verify("bo", "secret1"))
        amy = server.app.test_client()
        amy.post("/login", data={"username": "amy", "password": "secret1"})
        self.assertEqual(amy.post("/api/setup/admin", json={"name": "bo", "password": "hijack1"}).status_code, 403)  # admin, not owner
        self.assertEqual(amy.post("/api/setup/admin", json={"name": "newone", "password": "secret1"}).status_code, 403)
        self.assertTrue(accounts.verify("bo", "secret1"))
        self.assertFalse(accounts.exists("newone"))
        self.assertEqual(owner.post("/api/setup/admin", json={"name": "newone", "password": "secret1"}).status_code, 200)
        self.assertEqual([u["name"] for u in accounts.listing() if u["founder"]], ["boss"])  # only ever one founder

    def test_an_install_from_before_owners_is_claimed_explicitly_with_the_setup_code(self):
        # accounts made while the .env password was the owner: two admins, nobody is the founder, and the old owner's files are lying around
        accounts.add("ben", "hunter22", "admin")
        accounts.add("amy", "secret1", "admin")
        with open(os.path.join(self.dir, "favourites.json"), "w") as f:
            json.dump([{"name": "capped", "url": "https://kick.com/capped", "source": "Kick"}], f)
        with open(os.path.join(self.dir, "kick_link.json"), "w") as f:
            json.dump({"name": "bennyhunnid"}, f)
        self.assertFalse(accounts.founder_name())  # nothing is decided on its own
        amy = server.app.test_client()
        amy.post("/login", data={"username": "amy", "password": "secret1"})
        st = amy.get("/api/setup").get_json()
        self.assertEqual((st["needed"], st["needs_owner"], st["needs_code"]), (True, True, True))
        self.assertEqual(amy.post("/api/setup/claim", json={}).status_code, 403)  # no code
        self.assertEqual(amy.post("/api/setup/claim", json={"code": "nope"}).status_code, 403)
        self.assertFalse(accounts.founder_name())
        self.assertEqual(amy.post("/api/setup/claim", json={"code": server.SETUP_CODE}).status_code, 200)  # the operator chose amy
        self.assertEqual(accounts.founder_name(), "amy")
        self.assertEqual([f["name"] for f in server.load_favs("amy")], ["capped"])  # the old files go to whoever claimed
        self.assertEqual(kick.linked("amy"), "bennyhunnid")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "favourites.json")))
        ben = server.app.test_client()
        ben.post("/login", data={"username": "ben", "password": "hunter22"})
        self.assertEqual(ben.post("/api/setup/claim", json={"code": server.SETUP_CODE}).status_code, 400)  # already owned
        self.assertTrue(accounts.is_owner("amy") and not accounts.is_owner("ben"))
        self.assertFalse(amy.get("/api/setup").get_json()["needs_owner"])

    def test_a_plain_user_cannot_claim_the_install(self):
        accounts.add("ben", "hunter22", "admin")
        accounts.add("sam", "secret1")
        sam = server.app.test_client()
        sam.post("/login", data={"username": "sam", "password": "secret1"})
        self.assertEqual(sam.post("/api/setup/claim", json={"code": server.SETUP_CODE}).status_code, 403)
        self.assertFalse(accounts.founder_name())

    def test_the_old_style_owner_session_is_not_a_login(self):
        accounts.add("ben", "hunter22", "admin", founder=True)
        tok = "legacy-token"
        server._sess_save({server._h(tok): 9e12})  # a bare expiry, as the .env password used to make
        self.assertIsNone(server.session_user(tok))


if __name__ == "__main__":
    unittest.main()
