import json
import struct
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import dockerctl  # noqa: E402

LIST = [{"Names": ["/uptime-kuma"], "Image": "kuma", "State": "running", "Status": "Up 1 h"},
        {"Names": ["/tvremote"], "Image": "tvremote:latest", "State": "running", "Status": "Up 2 min"}]
INSPECT = {"Id": "a" * 64, "Created": "2026-01-01T00:00:00Z", "RestartCount": 2,
           "State": {"Status": "running", "StartedAt": "2026-01-01T00:00:01Z", "ExitCode": 0, "Health": {"Status": "healthy"}},
           "Config": {"Image": "tvremote:latest", "User": "1000:1000", "Cmd": ["python3", "app.py"], "Entrypoint": ["/entrypoint.sh"], "Tty": False,
                      "Env": ["PORT=8765", "TVREMOTE_KICK_CLIENT_SECRET=hunter2", "TWITCH_CLIENT_ID=abc"],
                      "Labels": {"com.docker.compose.project": "tvremote"}},
           "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}, "NetworkMode": "host", "GroupAdd": ["44"],
                          "Devices": [{"PathOnHost": "/dev/dri"}], "PortBindings": {}},
           "Mounts": [{"Source": "/run/user/1000", "Destination": "/run/user/1000", "RW": True, "Type": "bind"}]}


def frame(stream, text):
    b = text.encode()
    return bytes([stream, 0, 0, 0]) + struct.pack(">I", len(b)) + b


def fake(routes, seen=None):
    def call(method, path, timeout=15):
        if seen is not None:
            seen.append((method, path))
        for prefix, (status, body) in routes.items():
            if path.startswith(prefix):
                return status, body if isinstance(body, bytes) else json.dumps(body).encode()
        return 404, b"{}"
    return call


class DockerTest(unittest.TestCase):
    def test_only_this_app_is_listed_and_other_containers_are_refused(self):
        seen = []
        with mock.patch.object(dockerctl, "_call", fake({"/containers/json": (200, LIST)}, seen)):
            rows = dockerctl.containers()
            for fn in (lambda: dockerctl.detail("uptime-kuma"), lambda: dockerctl.logs("uptime-kuma"), lambda: dockerctl.act("uptime-kuma", "stop")):
                with self.assertRaises(ValueError):
                    fn()
        self.assertEqual([r["name"] for r in rows], ["tvremote"])
        self.assertTrue(all(p.startswith("/containers/json") for _, p in seen[1:]))  # nothing was asked about the other container

    def test_unknown_or_odd_names_are_refused_before_any_call(self):
        seen = []
        with mock.patch.object(dockerctl, "_call", fake({"/containers/json": (200, LIST)}, seen)):
            for bad in ("nope", "../etc", "tvremote/../x", "", "a b"):
                with self.assertRaises(ValueError):
                    dockerctl.act(bad, "restart")
        self.assertTrue(all(p.startswith("/containers/json") for _, p in seen))  # nothing but the list was asked

    def test_only_the_four_actions(self):
        with self.assertRaises(ValueError):
            dockerctl.act("tvremote", "rm")

    def test_secrets_are_never_returned(self):
        with mock.patch.object(dockerctl, "_call", fake({"/containers/json": (200, LIST), "/containers/tvremote/json": (200, INSPECT)})):
            d = dockerctl.detail("tvremote")
        env = dict(d["env"])
        self.assertEqual(env["PORT"], "8765")
        self.assertIsNone(env["TVREMOTE_KICK_CLIENT_SECRET"])
        self.assertNotIn("hunter2", json.dumps(d))
        self.assertEqual((d["restart_policy"], d["network"], d["health"], d["restarts"]), ("unless-stopped", "host", "healthy", 2))

    def test_urls_lose_their_passwords(self):
        self.assertEqual(dockerctl.scrub("x postgres://bob:pw123@db/x y"), "x postgres://bob:****@db/x y")
        self.assertEqual(dict(dockerctl.redact(["A=https://u:p@h"]))["A"], "https://u:****@h")

    def test_logs_are_demultiplexed(self):
        raw = frame(1, "hello\n") + frame(2, "oops\n")
        with mock.patch.object(dockerctl, "_call", fake({"/containers/json": (200, LIST), "/containers/tvremote/logs": (200, raw),
                                                         "/containers/tvremote/json": (200, INSPECT)})):
            self.assertEqual(dockerctl.logs("tvremote", 50), "hello\noops\n")

    def test_action_results(self):
        seen = []
        with mock.patch.object(dockerctl, "_call", fake({"/containers/json": (200, LIST), "/containers/tvremote/restart": (204, b""),
                                                         "/containers/tvremote/start": (304, b""), "/containers/tvremote/kill": (500, {"message": "boom"})}, seen)):
            self.assertEqual(dockerctl.act("tvremote", "restart"), "Restarted.")
            self.assertIn("already running", dockerctl.act("tvremote", "start"))
            with self.assertRaises(OSError):
                dockerctl.act("tvremote", "kill")
        self.assertIn(("POST", "/containers/tvremote/restart?t=5"), seen)


if __name__ == "__main__":
    unittest.main()
