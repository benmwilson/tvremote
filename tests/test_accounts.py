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
import app as server  # noqa: E402
import throttle  # noqa: E402
import kick  # noqa: E402
from helpers import founder_client  # noqa: E402


class AccountsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        throttle.fails.clear()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.dict(os.environ, {"TVREMOTE_DATA": self.dir}), mock.patch.object(server, "DATA", self.dir),
                  mock.patch.object(server, "FAV_FILE", os.path.join(self.dir, "favourites.json")),
                  mock.patch.object(kick, "DATA", self.dir),
                  mock.patch.object(server.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)

    def login(self, name, password):
        c = server.app.test_client()
        r = c.post("/login", data={"username": name, "password": password})
        return c, r.status_code

    def test_passwords_are_hashed_and_names_checked(self):
        accounts.add("Sam", "secret1")
        self.assertNotIn("secret1", open(os.path.join(self.dir, "users.json")).read())
        self.assertTrue(accounts.verify("sam", "secret1"))
        self.assertFalse(accounts.verify("sam", "wrong"))
        self.assertFalse(accounts.verify("nobody", "secret1"))
        for bad in ("owner", "a", "has space", "x" * 21):
            with self.assertRaises(ValueError):
                accounts.add(bad, "secret1")
        with self.assertRaises(ValueError):
            accounts.add("tim", "short")

    def test_each_account_has_its_own_favourites_and_the_founder_took_over_the_old_ones(self):
        accounts.add("sam", "secret1")
        owner = founder_client(server)
        sam, code = self.login("sam", "secret1")
        self.assertEqual(code, 303)
        self.assertEqual(sam.get("/api/auth").get_json()["user"], "sam")
        owner.post("/api/favourites", json={"url": "https://kick.com/capped", "name": "capped", "source": "Kick"})
        self.assertEqual([f["name"] for f in owner.get("/api/favourites").get_json()], ["capped"])
        self.assertEqual(sam.get("/api/favourites").get_json(), [])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "favourites.boss.json")))
        sam.post("/api/favourites", json={"url": "https://kick.com/xqc", "name": "xqc", "source": "Kick"})
        self.assertTrue(os.path.exists(os.path.join(self.dir, "favourites.sam.json")))
        self.assertEqual(sorted(server.watched_urls()), ["https://kick.com/capped", "https://kick.com/xqc"])  # live checks cover everyone

    def test_login_rules(self):
        accounts.add("sam", "secret1")
        founder_client(server)
        self.assertEqual(self.login("sam", "boss-pass1")[1], 401)  # an account needs its own password, not someone else's
        self.assertEqual(self.login("sam", "wrong")[1], 401)
        self.assertEqual(self.login("whoever", "boss-pass1")[1], 401)  # no catch-all name any more
        self.assertEqual(self.login("", "boss-pass1")[1], 401)
        self.assertEqual(self.login("sam", "secret1")[1], 303)

    def test_only_the_owner_manages_accounts(self):
        owner = founder_client(server)
        r = owner.post("/api/accounts", json={"name": "sam", "password": "secret1"})
        self.assertEqual([u["name"] for u in r.get_json()["users"]], ["boss", "sam"])
        self.assertEqual(owner.post("/api/accounts", json={"name": "x", "password": "secret1"}).status_code, 400)
        sam, _ = self.login("sam", "secret1")
        self.assertEqual(sam.get("/api/accounts").status_code, 403)
        self.assertEqual(sam.post("/api/accounts", json={"name": "tim", "password": "secret1"}).status_code, 403)
        sam.post("/api/favourites", json={"url": "https://kick.com/xqc", "name": "xqc", "source": "Kick"})
        owner.delete("/api/accounts", json={"name": "sam"})
        self.assertEqual(sam.get("/api/favourites").status_code, 401)  # its session ended with it
        self.assertFalse(os.path.exists(os.path.join(self.dir, "favourites.sam.json")))


    def test_roles(self):
        owner = founder_client(server)
        self.assertEqual(owner.get("/api/auth").get_json()["role"], "admin")  # the founder is an admin
        owner.post("/api/accounts", json={"name": "sam", "password": "secret1"})
        owner.post("/api/accounts", json={"name": "amy", "password": "secret1", "role": "admin"})
        self.assertEqual([(u["name"], u["role"]) for u in owner.get("/api/accounts").get_json()["users"]], [("amy", "admin"), ("boss", "admin"), ("sam", "user")])
        sam, _ = self.login("sam", "secret1")
        for path in ("/api/accounts", "/api/debug", "/api/screen.png"):
            self.assertEqual(sam.get(path).status_code, 403, path)
        self.assertEqual(sam.post("/api/stats", json={"enabled": True}).status_code, 403)
        self.assertEqual(sam.patch("/api/accounts", json={"name": "sam", "role": "admin"}).status_code, 403)  # no promoting yourself
        self.assertEqual(sam.get("/api/auth").get_json()["admin"], False)
        owner.patch("/api/accounts", json={"name": "sam", "role": "admin"})
        self.assertEqual(sam.get("/api/accounts").status_code, 200)  # takes effect on the next request, no new login
        self.assertEqual(owner.patch("/api/accounts", json={"name": "sam", "role": "root"}).status_code, 400)
        owner.post("/api/accounts", json={"name": "sam", "password": "newpass1"})  # a password change keeps the role
        self.assertEqual(accounts.role("sam"), "admin")
        amy, _ = self.login("amy", "secret1")
        self.assertEqual(amy.delete("/api/accounts", json={"name": "amy"}).status_code, 400)  # not the one you're signed in with
        self.assertEqual(amy.delete("/api/accounts", json={"name": "sam"}).status_code, 403)  # sam is an admin: only the owner may
        self.assertEqual(amy.post("/api/accounts", json={"name": "sam", "password": "hijack1"}).status_code, 403)  # no password reset either
        self.assertEqual(amy.patch("/api/accounts", json={"name": "sam", "role": "user"}).status_code, 403)  # nor demotion
        self.assertTrue(accounts.verify("sam", "newpass1"))
        owner.post("/api/accounts", json={"name": "tim", "password": "secret1"})
        self.assertEqual(amy.post("/api/accounts", json={"name": "tim", "password": "reset-pw1"}).status_code, 200)  # ordinary users: any admin
        self.assertEqual(amy.delete("/api/accounts", json={"name": "tim"}).status_code, 200)
        self.assertEqual(owner.delete("/api/accounts", json={"name": "sam"}).status_code, 200)  # the owner can do all of it
        self.assertEqual(amy.post("/api/accounts", json={"name": "amy", "password": "mynewpw1"}).status_code, 200)  # your own password is yours (and signs you out)

    def test_a_new_password_or_a_removed_and_recreated_name_ends_old_logins(self):
        owner = founder_client(server)
        owner.post("/api/accounts", json={"name": "sam", "password": "secret1"})
        sam, _ = self.login("sam", "secret1")
        self.assertEqual(sam.get("/api/favourites").status_code, 200)
        owner.post("/api/accounts", json={"name": "sam", "password": "another1"})  # password changed
        self.assertEqual(sam.get("/api/favourites").status_code, 401)
        sam, _ = self.login("sam", "another1")
        owner.delete("/api/accounts", json={"name": "sam"})
        owner.post("/api/accounts", json={"name": "sam", "password": "another1"})  # same name again
        self.assertEqual(sam.get("/api/favourites").status_code, 401)

    def test_an_unknown_session_is_nobody_not_the_owner(self):
        self.assertIsNone(server.session_user("not-a-token"))
        self.assertIsNone(server.session_user(""))
        founder_client(server)  # once an account exists, login is required
        c = server.app.test_client()
        c.set_cookie("tv_auth", "not-a-token")
        self.assertEqual(c.get("/api/favourites").status_code, 401)


class ChatTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        p = mock.patch.multiple(kick, ID=kick.ID, SECRET=kick.SECRET, REDIRECT=kick.REDIRECT, DATA=kick.DATA, _open=kick._open)
        p.start()
        self.addCleanup(p.stop)  # put the module back as it was for the other tests
        kick.ID, kick.SECRET, kick.DATA = "id", "secret", self.dir
        kick._ids.clear()
        kick._token.update(value=None, exp=0.0)
        self.sent = []

        def fake(req, timeout=10):
            self.sent.append(req)
            if "oauth/token" in req.full_url:
                return json.dumps({"access_token": "t", "refresh_token": "r2", "expires_in": 3600}).encode()
            if "/channels" in req.full_url:
                return json.dumps({"data": [{"broadcaster_user_id": 99}]}).encode()
            return json.dumps({"data": {"is_sent": True, "message_id": "m"}}).encode()
        kick._open = fake

    def link(self, scope="user:read chat:write", exp=9e12):
        with open(os.path.join(self.dir, "kick_link.json"), "w") as f:
            json.dump({"access": "user-token", "refresh": "r", "exp": exp, "scope": scope, "name": "ben"}, f)

    def test_sends_as_the_linked_user_to_the_channels_id(self):
        self.link()
        kick.send_chat("owner", "capped", " hi ")
        req = self.sent[-1]
        self.assertEqual(req.get_header("Authorization"), "Bearer user-token")
        self.assertEqual(json.loads(req.data), {"broadcaster_user_id": 99, "content": "hi", "type": "user"})

    def test_an_old_link_without_the_chat_scope_asks_to_link_again(self):
        self.link(scope="user:read channel:read")
        self.assertFalse(kick.can_chat("owner"))
        with self.assertRaisesRegex(ValueError, "link Kick again"):
            kick.send_chat("owner", "capped", "hi")

    def test_an_expired_token_is_refreshed_and_kept(self):
        self.link(exp=0)
        kick.send_chat("owner", "capped", "hi")
        self.assertEqual(self.sent[-1].get_header("Authorization"), "Bearer t")
        self.assertEqual(json.load(open(os.path.join(self.dir, "kick_link.json")))["refresh"], "r2")

    def test_empty_or_long_messages_are_refused(self):
        self.link()
        for text in ("  ", "x" * 501):
            with self.assertRaises(ValueError):
                kick.send_chat("owner", "capped", text)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
