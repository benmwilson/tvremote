import sys
import time
import unittest
from datetime import datetime
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
    # writing the subtitle file is recorded as ["ass-file", text] among the window commands
    write = lambda path, data: sent.append(["ass-file", data.decode("latin-1") if isinstance(data, bytes) else data, path])
    s = shell.Shell(hint="Open http://pi.example:8765 on your phone", spawn=popen, ipc=lambda sock, cmd, timeout=1.0: sent.append(cmd) or {"error": "success"},
                    ready=lambda: ready, sleep=lambda x: None, write=write)
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


class Idle(unittest.TestCase):
    def test_the_ready_screen_shows_clock_date_address_and_favourites(self):
        t = shell.screen("idle", hint="Open http://pi.example:8765 on your phone", now=datetime(2026, 11, 4, 21, 5), favs=["capped", "xqc"])
        for want in ("21:05", "Wednesday", "November", "Ready", "Open http://pi.example:8765 on your phone", "capped", "xqc"):
            self.assertIn(want, t)

    def test_other_screens_have_no_clock(self):
        self.assertNotIn("21:05", shell.screen("ended", "chan", now=datetime(2026, 11, 4, 21, 5)))

    def test_the_clock_is_redrawn_while_idle(self):
        minutes = iter(range(10, 59))
        s, popen, sent = make_shell([FakeProc()])
        s._clock = lambda: datetime(2026, 11, 4, 21, next(minutes))
        s.idle_interval = 0.02
        s.start()
        time.sleep(0.25)
        frames = {c[1] for c in sent if c[0] == "ass-file" and c[2] == shell.ASS_FILE}
        self.assertGreaterEqual(len(frames), 3)
        s.show("playing")
        time.sleep(0.1)
        sent.clear()
        time.sleep(0.15)
        self.assertEqual(sent, [])

    def test_the_window_shows_one_still_picture_that_never_ends(self):
        s, popen, sent = make_shell([FakeProc()])
        s.start()
        cmd = popen.call_args[0][0]
        self.assertIn("--image-display-duration=inf", cmd)
        self.assertEqual(cmd[-1], shell.BG_FILE)
        bg = [c for c in sent if c[0] == "ass-file" and c[2] == shell.BG_FILE]
        self.assertTrue(bg and bg[0][1].startswith("\x89PNG"))

    def test_the_first_draw_adds_the_subtitle_and_later_draws_reload_it(self):
        s, popen, sent = make_shell([FakeProc()])
        s.start()
        s.show("loading", "chan")
        names = [c[0] for c in sent if c[0] in ("sub-add", "sub-reload")]
        self.assertEqual(names[0], "sub-add")
        self.assertEqual(names.count("sub-add"), 1)
        self.assertIn("sub-reload", names)

    def test_the_subtitle_file_is_a_complete_ass_script(self):
        f = shell.ass_file(shell.screen("idle"))
        for want in ("PlayResX: 1920", "PlayResY: 1080", "[Events]", "Dialogue: 0,0:00:00.00,9:59:59.99", "Ready"):
            self.assertIn(want, f)

    def test_every_screen_unpauses_the_window(self):
        s, popen, sent = make_shell([FakeProc()])
        s.start()
        s.show("playing")
        sent.clear()
        s.show("idle")
        self.assertIn(["set_property", "pause", False], sent)


class QualitySwap(unittest.TestCase):
    def test_changing_quality_has_its_own_screen(self):
        t = shell.screen("quality", "chan  ·  720p60")
        self.assertIn("Switching quality", t)
        self.assertIn("720p60", t)

    def test_the_quality_screen_animates_like_loading(self):
        s, popen, sent = make_shell([FakeProc()])
        s.interval = 0.02
        s.start()
        sent.clear()
        s.show("quality", "chan  ·  720p60")
        time.sleep(0.2)
        self.assertGreaterEqual(len({c[1] for c in sent if c[0] == "ass-file" and c[2] == shell.ASS_FILE}), 2)
        s.show("playing")


class Process(unittest.TestCase):
    def test_start_uses_the_first_output_that_works(self):
        s, popen, sent = make_shell([FakeProc()])
        self.assertTrue(s.start())
        self.assertEqual(s.vo, shell.VO_ORDER[0])
        self.assertEqual(popen.call_count, 1)
        self.assertTrue(any(c[0] == "sub-add" for c in sent))  # the idle screen was drawn as a subtitle

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
        self.assertIn(["set_property", "sub-visibility", False], sent)

    def test_other_screens_wake_the_shell_and_draw(self):
        s, popen, sent = make_shell([FakeProc()])
        s.start()
        s.show("playing")
        sent.clear()
        s.show("ended", "chan")
        self.assertIn(["set_property", "pause", False], sent)
        drawn = [c for c in sent if c[0] == "ass-file" and c[2] == shell.ASS_FILE]
        self.assertTrue(drawn and "Stream ended" in drawn[-1][1])

    def test_loading_animates_until_something_else_is_shown(self):
        s, popen, sent = make_shell([FakeProc()])
        s.interval = 0.02
        s.start()
        sent.clear()
        s.show("loading", "chan")
        time.sleep(0.2)
        frames = {c[1] for c in sent if c[0] == "ass-file" and c[2] == shell.ASS_FILE}
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
