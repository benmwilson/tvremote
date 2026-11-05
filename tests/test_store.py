import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import app as server  # noqa: E402
import kick  # noqa: E402
import store  # noqa: E402
from helpers import founder_client  # noqa: E402


class WriteJson(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_writes_replaces_and_leaves_no_temporary_files(self):
        p = os.path.join(self.dir, "a.json")
        store.write_json(p, {"a": 1})
        store.write_json(p, {"a": 2}, indent=2)
        self.assertEqual(json.load(open(p)), {"a": 2})
        self.assertEqual(os.listdir(self.dir), ["a.json"])

    def test_a_failed_write_keeps_the_old_file_and_cleans_up(self):
        p = os.path.join(self.dir, "a.json")
        store.write_json(p, {"ok": True})
        with self.assertRaises(TypeError):
            store.write_json(p, {"bad": object()})  # not JSON
        self.assertEqual(json.load(open(p)), {"ok": True})
        self.assertEqual(os.listdir(self.dir), ["a.json"])

    @unittest.skipIf(os.name == "nt", "file modes are not meaningful on Windows")
    def test_secret_files_are_owner_only(self):
        p = os.path.join(self.dir, "s.json")
        store.write_json(p, {}, 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)


class Simultaneous(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.dict(os.environ, {"TVREMOTE_DATA": self.dir}), mock.patch.object(server, "DATA", self.dir),
                  mock.patch.object(server, "FAV_FILE", os.path.join(self.dir, "favourites.json")),
                  mock.patch.object(server, "RECENT_FILE", os.path.join(self.dir, "recents.json")),
                  mock.patch.object(kick, "DATA", self.dir), mock.patch.dict(server._key, {"v": None})):
            p.start()
            self.addCleanup(p.stop)
        server._fails.clear()

    def test_twenty_saves_at_once_all_survive(self):
        boss = founder_client(server)
        errors = []

        def save(i):
            c = server.app.test_client()
            c.set_cookie("tv_auth", boss.get_cookie("tv_auth").value)
            r = c.post("/api/favourites", json={"url": f"https://kick.com/chan{i:02d}", "name": f"chan{i:02d}", "source": "Kick"})
            if r.status_code != 200:
                errors.append(r.status_code)

        threads = [threading.Thread(target=save, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])  # no 500 from two saves trampling one temporary file
        self.assertEqual(len(boss.get("/api/favourites").get_json()), 20)  # and no save lost another

    def test_recents_keep_every_distinct_link_when_remembered_at_once(self):
        threads = [threading.Thread(target=server.remember, args=(f"https://kick.com/r{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(server.load_recents()), 8)


if __name__ == "__main__":
    unittest.main()
