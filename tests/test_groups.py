import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import groups  # noqa: E402

LINKS = {"https://kick.com/a": ("a", "Kick"), "https://kick.com/b": ("b", "Kick"), "https://www.twitch.tv/c": ("c", "Twitch"),
         "https://kick.com/d": ("d", "Kick"), "https://kick.com/e": ("e", "Kick")}
describe = LINKS.get


class Pure(unittest.TestCase):
    def test_a_group_is_two_to_four_different_allowed_links(self):
        self.assertIsNone(groups.clean_members(["https://kick.com/a"], describe))
        self.assertIsNone(groups.clean_members(["https://kick.com/a", "https://kick.com/A"], describe))  # the same stream twice is one
        self.assertIsNone(groups.clean_members(["https://kick.com/a", "http://192.168.1.5/x"], describe))  # one is not allowed: only one left
        m = groups.clean_members(list(LINKS), describe)
        self.assertEqual([x["name"] for x in m], ["a", "b", "c", "d"])  # at most four

    def test_adding_the_same_set_twice_gives_the_same_group_and_names_it_from_its_members(self):
        d = {"groups": [], "history": {}, "dismissed": []}
        m = groups.clean_members(["https://kick.com/a", "https://www.twitch.tv/c"], describe)
        g1 = groups.add(d, m)
        g2 = groups.add(d, list(reversed(m)))
        self.assertEqual(g1["id"], g2["id"])
        self.assertEqual((len(d["groups"]), g1["name"]), (1, "a, c"))
        self.assertTrue(groups.rename(d, g1["id"], "  Friday  "))
        self.assertEqual(d["groups"][0]["name"], "Friday")
        self.assertTrue(groups.remove(d, g1["id"]))
        self.assertFalse(groups.remove(d, g1["id"]))

    def test_a_set_is_suggested_after_two_sessions_until_it_is_saved_or_turned_down(self):
        d = {"groups": [], "history": {}, "dismissed": []}
        urls = ["https://kick.com/a", "https://kick.com/b"]
        self.assertEqual(groups.record(d, urls[:1]), 0)  # one stream is not a group
        groups.record(d, urls)
        self.assertEqual(groups.suggestions(d, describe), [])  # once is not a habit
        groups.record(d, list(reversed(urls)))  # the order does not matter
        sug = groups.suggestions(d, describe)
        self.assertEqual((len(sug), sug[0]["count"], sug[0]["name"]), (1, 2, "a, b"))
        groups.dismiss(d, sug[0]["key"])
        self.assertEqual(groups.suggestions(d, describe), [])
        groups.add(d, sug[0]["members"])  # saving it on purpose also lifts the 'turned down'
        self.assertEqual(groups.suggestions(d, describe), [])  # and it is saved, so not suggested either
        self.assertEqual(d["dismissed"], [])

    def test_the_history_does_not_grow_without_end(self):
        d = {"groups": [], "history": {}, "dismissed": []}
        for i in range(70):
            groups.record(d, [f"https://kick.com/x{i}", "https://kick.com/y"], now=i)
        self.assertLessEqual(len(d["history"]), 60)
        self.assertIn(groups.key_of(["https://kick.com/x69", "https://kick.com/y"]), d["history"])  # the newest are kept


class Api(unittest.TestCase):
    def setUp(self):
        import accounts
        import app as server
        from helpers import founder_client
        self.server = server
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.dict(os.environ, {"TVREMOTE_DATA": self.dir}), mock.patch.object(server, "DATA", self.dir),
                  mock.patch.object(server, "FAV_FILE", os.path.join(self.dir, "favourites.json")),
                  mock.patch.object(server, "SETTINGS_FILE", os.path.join(self.dir, "settings.json")), mock.patch.object(server, "CAST", None),
                  mock.patch.object(server, "HOME", None), mock.patch.object(server.livewatch, "poke")):
            p.start()
            self.addCleanup(p.stop)
        self.admin = founder_client(server)
        url = self.admin.post("/api/invites", json={"hours": 1}).get_json()["url"]
        self.guest = server.app.test_client()
        self.guest.post("/" + url.split("/", 3)[3])

    def test_save_list_rename_and_delete(self):
        r = self.admin.post("/api/groups", json={"urls": ["https://kick.com/a", "https://www.twitch.tv/c"]})
        gid = r.get_json()["group"]["id"]
        got = self.admin.get("/api/groups").get_json()
        self.assertEqual([x["name"] for x in got["groups"]], ["a, c"])
        self.assertEqual([m["source"] for m in got["groups"][0]["members"]], ["Kick", "Twitch"])
        self.assertEqual(self.admin.post("/api/groups/rename", json={"id": gid, "name": "Night shift"}).status_code, 200)
        self.assertEqual(self.admin.get("/api/groups").get_json()["groups"][0]["name"], "Night shift")
        self.assertEqual(self.admin.post("/api/groups", json={"urls": ["https://kick.com/a"]}).status_code, 400)
        self.assertEqual(self.admin.delete("/api/groups", json={"id": gid}).status_code, 200)
        self.assertEqual(self.admin.get("/api/groups").get_json()["groups"], [])

    def test_guests_cannot_see_save_or_start_groups(self):
        self.admin.post("/api/groups", json={"urls": ["https://kick.com/a", "https://kick.com/b"]})
        for method, path in (("get", "/api/groups"), ("post", "/api/groups"), ("delete", "/api/groups"), ("post", "/api/groups/play"), ("post", "/api/groups/dismiss")):
            self.assertEqual(getattr(self.guest, method)(path, json={"urls": ["https://kick.com/a", "https://kick.com/b"]}).status_code, 403, path)

    def test_playing_a_group_answers_at_once_and_starts_every_member(self):
        server, started, done = self.server, [], threading.Event()
        gid = self.admin.post("/api/groups", json={"urls": ["https://kick.com/a", "https://kick.com/b", "https://www.twitch.tv/c"]}).get_json()["group"]["id"]
        with mock.patch.object(server, "start_group", side_effect=lambda urls: (started.append(urls), done.set())):
            r = self.admin.post("/api/groups/play", json={"id": gid})
        self.assertEqual((r.status_code, r.get_json()["count"]), (200, 3))
        self.assertTrue(done.wait(3))
        self.assertEqual(started[0], ["https://kick.com/a", "https://kick.com/b", "https://www.twitch.tv/c"])
        self.assertEqual(self.admin.post("/api/groups/play", json={"id": "nope"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
