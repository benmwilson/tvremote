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


class SetupBase(unittest.TestCase):
    PASSWORD = ""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.dict(os.environ, {"TVREMOTE_DATA": self.dir}), mock.patch.object(server, "DATA", self.dir),
                  mock.patch.object(server, "FAV_FILE", os.path.join(self.dir, "favourites.json")),
                  mock.patch.object(kick, "DATA", self.dir), mock.patch.object(server, "PASSWORD", self.PASSWORD),
                  mock.patch.dict(server._key, {"v": None}), mock.patch.object(server.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)
        activity.reset()


class FreshInstall(SetupBase):
    """No .env password and no accounts: open until the wizard makes the first admin."""

    def test_the_wizard_is_needed_and_the_app_is_open_until_an_admin_exists(self):
        c = server.app.test_client()
        st = c.get("/api/setup").get_json()
        self.assertEqual((st["needed"], st["fresh"], st["has_admin"]), (True, True, False))
        self.assertEqual(c.get("/api/favourites").status_code, 200)  # nothing to protect yet
        self.assertEqual(c.post("/api/setup/finish").status_code, 400)  # not before the admin account

    def test_creating_the_admin_logs_them_in_and_locks_the_door(self):
        c = server.app.test_client()
        r = c.post("/api/setup/admin", json={"name": "Ben", "password": "hunter22"})
        self.assertEqual(r.get_json(), {"ok": True, "name": "ben"})
        self.assertIn("tv_auth", r.headers["Set-Cookie"])
        self.assertEqual(c.get("/api/favourites").status_code, 200)  # the new admin is in
        self.assertEqual(c.get("/api/auth").get_json()["user"], "ben")
        stranger = server.app.test_client()
        self.assertEqual(stranger.get("/api/favourites").status_code, 401)  # everyone else now needs to log in
        self.assertEqual(stranger.post("/api/setup/admin", json={"name": "evil", "password": "hunter22"}).status_code, 401)
        self.assertEqual(stranger.get("/api/setup").status_code, 401)
        self.assertFalse(accounts.exists("evil"))
        self.assertEqual(c.post("/api/setup/finish").status_code, 200)
        self.assertFalse(c.get("/api/setup").get_json()["needed"])

    def test_a_script_cannot_walk_in_with_an_empty_password(self):
        server.app.test_client().post("/api/setup/admin", json={"name": "ben", "password": "hunter22"})
        import base64
        c = server.app.test_client()
        for creds in (b":", b"ben:", b"ben:wrong", b"owner:"):
            self.assertEqual(c.get("/api/favourites", headers={"Authorization": "Basic " + base64.b64encode(creds).decode()}).status_code, 401, creds)
        ok = c.get("/api/favourites", headers={"Authorization": "Basic " + base64.b64encode(b"ben:hunter22").decode()})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(c.post("/login", data={"username": "", "password": ""}).status_code, 401)  # the owner can't log in without a .env password

    def test_the_first_admin_is_the_owner_and_the_last_admin_cannot_be_removed(self):
        c = server.app.test_client()
        c.post("/api/setup/admin", json={"name": "ben", "password": "hunter22"})
        users = c.get("/api/accounts").get_json()["users"]
        self.assertEqual([(u["name"], u["founder"]) for u in users], [("ben", True)])  # no ".env owner" row without a .env password
        self.assertEqual(c.patch("/api/accounts", json={"name": "ben", "role": "user"}).status_code, 400)  # the only admin
        c.post("/api/accounts", json={"name": "amy", "password": "secret1", "role": "admin"})
        amy = server.app.test_client()
        amy.post("/login", data={"username": "amy", "password": "secret1"})
        self.assertEqual(amy.delete("/api/accounts", json={"name": "ben"}).status_code, 403)  # an admin can't remove the owner
        self.assertEqual(amy.post("/api/accounts", json={"name": "ben", "password": "hijack1"}).status_code, 403)
        self.assertTrue(accounts.verify("ben", "hunter22"))
        self.assertEqual(c.delete("/api/accounts", json={"name": "amy"}).status_code, 200)

    def test_bad_names_and_passwords_do_not_end_setup(self):
        c = server.app.test_client()
        self.assertEqual(c.post("/api/setup/admin", json={"name": "owner", "password": "hunter22"}).status_code, 400)
        self.assertEqual(c.post("/api/setup/admin", json={"name": "ben", "password": "x"}).status_code, 400)
        self.assertEqual(c.get("/api/setup").get_json()["needed"], True)  # still open: nobody was locked out by a typo


class WithEnvPassword(SetupBase):
    PASSWORD = "owner-pw"

    def test_the_owner_is_prompted_and_may_skip_or_add_a_named_admin(self):
        c = server.app.test_client()
        self.assertEqual(c.get("/api/setup").status_code, 401)  # not for strangers
        c.post("/login", data={"password": "owner-pw"})
        st = c.get("/api/setup").get_json()
        self.assertEqual((st["needed"], st["password_set"], st["fresh"]), (True, True, False))
        self.assertEqual(c.post("/api/setup/finish").status_code, 200)  # skipping is fine: the .env password remains
        self.assertFalse(c.get("/api/setup").get_json()["needed"])

    def test_adding_the_named_admin_ends_the_prompt_and_they_are_not_the_founder(self):
        c = server.app.test_client()
        c.post("/login", data={"password": "owner-pw"})
        self.assertEqual(c.post("/api/setup/admin", json={"name": "ben", "password": "hunter22"}).status_code, 200)
        self.assertFalse(c.get("/api/setup").get_json()["needed"])
        users = {u["name"]: u for u in c.get("/api/accounts").get_json()["users"]}
        self.assertEqual((users["ben"]["role"], users["ben"]["founder"], "owner" in users), ("admin", False, True))
        self.assertEqual(c.patch("/api/accounts", json={"name": "ben", "role": "user"}).status_code, 200)  # the .env owner is the fallback

    def test_setup_never_overwrites_an_account_or_lets_a_plain_admin_in(self):
        owner = server.app.test_client()
        owner.post("/login", data={"password": "owner-pw"})
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
        self.assertFalse([u for u in accounts.listing() if u["founder"]])  # founder only ever comes from a fresh install

    def test_a_normal_user_is_never_prompted(self):
        accounts.add("sam", "secret1")
        sam = server.app.test_client()
        sam.post("/login", data={"username": "sam", "password": "secret1"})
        self.assertEqual(sam.get("/api/setup").get_json(), {"needed": False})
        self.assertEqual(sam.post("/api/setup/admin", json={"name": "x1", "password": "hunter22"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
