import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import app as server  # noqa: E402
import throttle  # noqa: E402
from helpers import founder_client  # noqa: E402


class ShellSettingsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.dict(os.environ, {"TVREMOTE_DATA": self.dir}), mock.patch.object(server, "DATA", self.dir), mock.patch.object(server, "SETTINGS_FILE", os.path.join(self.dir, "settings.json"))):
            p.start(); self.addCleanup(p.stop)
        throttle.fails.clear()
        self.admin = founder_client(server)

    def test_the_preview_switches_leave_the_home_screen_alone(self):
        with mock.patch.dict(server.settings, {"shell": True}), mock.patch.object(server, "HOME", mock.Mock(running=lambda: True, vo=None, error=None, props=lambda: {})) as home:
            r = self.admin.post("/api/shell", json={"launcher_preview_sound": True})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(server.settings["launcher_preview_sound"])
            self.assertTrue(server.settings["shell"])  # it used to be switched off by this
            home.stop.assert_not_called()
            self.admin.post("/api/shell", json={"launcher_preview": False, "launcher_preview_sound": False})
            self.assertFalse(server.settings["launcher_preview"])
            self.assertTrue(server.settings["shell"])


if __name__ == "__main__":
    unittest.main()
