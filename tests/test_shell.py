import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import shell  # noqa: E402


class FakeProc:
    def __init__(self, alive=True):
        self.alive, self.pid = alive, 1
        self.terminated = False

    def poll(self):
        return None if self.alive else 1

    def terminate(self):
        self.alive, self.terminated = False, True

    def wait(self, timeout=None):
        return 1


def make_shell(procs, sent=None, ready=True):
    sent = [] if sent is None else sent
    popen = mock.Mock(side_effect=lambda cmd, **kw: procs.pop(0))
    s = shell.Shell(hint="Open http://pi.example:8765 on your phone", spawn=popen, ipc=lambda sock, cmd, timeout=1.0: sent.append(cmd) or {"error": "success"},
                    ready=lambda: ready, sleep=lambda x: None)
    return s, popen, sent


class Screens(unittest.TestCase):
    def test_idle_screen_shows_the_phone_hint(self):
        t = shell.screen("idle", hint="Open http://pi.example:8765 on your phone")
        self.assertIn("Ready", t)
        self.assertIn("Open http://pi.example:8765 on your phone", t)

    def test_each_screen_says_what_happened(self):
        self.assertIn("Loading", shell.screen("loading", "chan"))
        self.assertIn("chan", shell.screen("loading", "chan"))
        self.assertIn("Not live right now", shell.screen("offline", "chan"))
        self.assertIn("Stream ended", shell.screen("ended", "chan"))
        t = shell.screen("error", "HTTP error 404 Not Found")
        self.assertIn("Couldn't play", t)
        self.assertIn("HTTP error 404 Not Found", t)

    def test_text_cannot_inject_ass_tags(self):
        t = shell.screen("loading", r"x {\p1} \N y")
        self.assertNotIn(r"{\p1}", t)

    def test_loading_dots_change(self):
        self.assertNotEqual(shell.screen("loading", "c", dots=1), shell.screen("loading", "c", dots=2))


class Process(unittest.TestCase):
    def test_start_uses_the_first_output_that_works(self):
        s, popen, sent = make_shell([FakeProc()])
        self.assertTrue(s.start())
        self.assertEqual(s.vo, shell.VO_ORDER[0])
        self.assertEqual(popen.call_count, 1)
        self.assertTrue(any(c[0] == "osd-overlay" for c in sent))  # the idle screen was drawn

    def test_start_falls_back_when_the_first_output_exits(self):
        s, popen, sent = make_shell([FakeProc(alive=False), FakeProc()])
        self.assertTrue(s.start())
        self.assertEqual(s.vo, shell.VO_ORDER[1])
        self.assertEqual(popen.call_count, 2)

    def test_start_reports_when_nothing_works(self):
        s, popen, sent = make_shell([FakeProc(alive=False)] * len(shell.VO_ORDER))
        self.assertFalse(s.start())
        self.assertFalse(s.running())
        self.assertTrue(s.error)

    def test_stop_ends_the_window(self):
        p = FakeProc()
        s, popen, sent = make_shell([p])
        s.start()
        s.stop()
        self.assertTrue(p.terminated)
        self.assertFalse(s.running())


class Showing(unittest.TestCase):
    def test_playing_pauses_the_shell_and_clears_the_text(self):
        s, popen, sent = make_shell([FakeProc()])
        s.start()
        sent.clear()
        self.assertTrue(s.show("playing"))
        self.assertIn(["set_property", "pause", True], sent)
        self.assertIn(["osd-overlay", 1, "none"], sent)

    def test_other_screens_wake_the_shell_and_draw(self):
        s, popen, sent = make_shell([FakeProc()])
        s.start()
        s.show("playing")
        sent.clear()
        s.show("ended", "chan")
        self.assertIn(["set_property", "pause", False], sent)
        drawn = [c for c in sent if c[0] == "osd-overlay" and c[2] == "ass-events"]
        self.assertTrue(drawn and "Stream ended" in drawn[-1][3])

    def test_loading_animates_until_something_else_is_shown(self):
        s, popen, sent = make_shell([FakeProc()])
        s.interval = 0.02
        s.start()
        sent.clear()
        s.show("loading", "chan")
        time.sleep(0.2)
        frames = {c[3] for c in sent if c[0] == "osd-overlay" and c[2] == "ass-events"}
        self.assertGreaterEqual(len(frames), 2)
        s.show("playing")
        time.sleep(0.1)
        sent.clear()
        time.sleep(0.15)
        self.assertEqual(sent, [])  # nothing is drawn while a stream plays

    def test_show_does_nothing_when_the_shell_is_not_running(self):
        s, popen, sent = make_shell([])
        self.assertFalse(s.show("idle"))
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
