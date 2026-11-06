import re
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
    write = lambda path, data: sent.append(["ass-file", data, path])
    sids = iter(range(1, 1000))

    def ipc(sock, cmd, timeout=1.0):
        sent.append(cmd)
        if cmd == ["get_property", "sid"]:
            return {"error": "success", "data": next(sids)}
        if cmd == ["get_property", "time-pos"]:
            return {"error": "success", "data": 100.0}
        return {"error": "success"}
    s = shell.Shell(hint="Open http://pi.example:8765 on your phone", spawn=popen, ipc=ipc,
                    ready=lambda: ready, sleep=lambda x: None, write=write)
    return s, popen, sent


class QrCode(unittest.TestCase):
    URL = "http://192.168.1.149:8765"

    def test_the_code_is_a_card_dots_and_a_caption_in_the_lower_right(self):
        card, dots, cap = shell.qr_pieces(self.URL)
        self.assertIn(r"\p1", card)
        self.assertGreater(dots.count("m "), 100)  # a drawing with one shape per dark dot
        self.assertIn("Scan for the remote", cap)
        x = int(re.search(r"\\pos\((\d+),", card).group(1))
        self.assertGreater(x, shell.W // 2)  # right half of the screen

    def test_no_address_means_no_code(self):
        self.assertEqual(shell.qr_pieces(""), [])

    def test_only_the_ready_screen_has_it_and_the_switch_turns_it_off(self):
        def file_for(kind, qr):
            s, _, _ = make_shell([FakeProc()])
            written = {}
            s._write = lambda path, data: written.update(data=data)
            s._qr = lambda: qr
            s.start()
            written.clear()
            s.show(kind)
            return written["data"]
        self.assertIn("Scan for the remote", file_for("idle", self.URL))
        self.assertNotIn("Scan for the remote", file_for("idle", ""))
        self.assertNotIn("Scan for the remote", file_for("loading", self.URL))


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
        t = shell.screen("idle", hint="Open http://pi.example:8765 on your phone", now=datetime(2026, 11, 4, 21, 5, 2))
        for want in ("9:05 PM", "Wednesday", "November", "Ready", "Open http://pi.example:8765 on your phone"):
            self.assertIn(want, t)

    def test_the_ready_screen_no_longer_lists_favourites(self):
        self.assertNotIn("Favourites", shell.screen("idle", now=datetime(2026, 11, 4, 21, 5), favs=["capped"]))

    def test_the_colon_blinks_with_the_seconds_without_moving_the_digits(self):
        on = shell.clock12(datetime(2026, 11, 4, 0, 5, 2))
        off = shell.clock12(datetime(2026, 11, 4, 0, 5, 3))
        self.assertEqual(on, "12:05 AM")
        self.assertIn("alpha&HFF&", off)
        self.assertEqual(off.replace(r"{\alpha&HFF&}:{\alpha&H00&}", ":"), on)

    def test_loading_names_lose_emoji(self):
        self.assertNotIn("\U0001F408", shell.screen("loading", "xqc \U0001F408"))

    def test_other_screens_have_no_clock(self):
        self.assertNotIn("9:05", shell.screen("ended", "chan", now=datetime(2026, 11, 4, 21, 5)))

    def test_the_ready_file_has_an_event_for_every_second_with_a_blinking_colon(self):
        s, popen, sent = make_shell([FakeProc()])
        s._clock = lambda: datetime(2026, 11, 4, 21, 5, 58, 250000)
        s.start()
        f = [c for c in sent if c[0] == "ass-file"][-1][1]
        self.assertEqual(f.count("Dialogue:"), shell.IDLE_SPAN + 4)  # a second each, plus the logo pieces
        self.assertIn("9:05 PM", f)
        self.assertIn("9:06 PM", f)  # the minute rolls over inside the file
        self.assertIn("alpha&HFF&", f)  # the hidden-colon seconds
        first = f.split("Dialogue:")[1]
        self.assertIn("1:39.75,0:01:40.75", first.replace(" ", "")[:80] or first)  # timed to the window clock: 100 s minus the 0.25 s already into the second

    def test_the_ready_file_is_renewed_before_it_runs_out_and_not_while_playing(self):
        s, popen, sent = make_shell([FakeProc()])
        s.renew = 0.02
        s.start()
        time.sleep(0.2)
        self.assertGreaterEqual(len([c for c in sent if c[0] == "ass-file"]), 3)
        s.show("playing")
        time.sleep(0.1)
        sent.clear()
        time.sleep(0.15)
        self.assertEqual(sent, [])

    def test_the_window_plays_a_looped_colour_so_the_clock_runs(self):
        s, popen, sent = make_shell([FakeProc()])
        s.start()
        cmd = popen.call_args[0][0]
        self.assertIn("--loop-file=inf", cmd)
        self.assertTrue(cmd[-1].startswith("av://lavfi:color="))

    def test_each_screen_loads_a_new_subtitle_and_drops_the_old_one(self):
        s, popen, sent = make_shell([FakeProc()])
        s.start()
        s.show("loading", "chan")
        adds = [c for c in sent if c[0] == "sub-add"]
        self.assertEqual(len(adds), 2)
        self.assertNotEqual(adds[0][1], adds[1][1])  # alternating files
        self.assertIn(["sub-remove", 1], sent)

    def test_the_subtitle_file_is_a_complete_ass_script(self):
        f = shell.ass_file([(0, 70.5, shell.screen("idle"))])
        for want in ("PlayResX: 1920", "PlayResY: 1080", "[Events]", "Dialogue: 0,0:00:00.00,0:01:10.50", "Ready"):
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
        s.start()
        sent.clear()
        s.show("quality", "chan  ·  720p60")
        f = [c for c in sent if c[0] == "ass-file"][-1][1]
        self.assertGreater(len({l for l in f.split("\n") if "Switching quality" in l}), 2)  # a frame per dot step
        s.show("playing")


class Refresh(unittest.TestCase):
    def test_the_refresh_screen_says_refreshing_with_a_spinning_wheel(self):
        a, b = shell.screen("refresh", "chan", dots=0), shell.screen("refresh", "chan", dots=1)
        self.assertIn("Refreshing", a)
        self.assertNotEqual(a, b)


class Logo(unittest.TestCase):
    def test_the_logo_is_drawn_once_for_the_whole_file_and_the_text_sits_below_it(self):
        f = shell.ass_file([(0, 1, shell.screen("idle")), (1, 2, shell.screen("idle"))])
        self.assertEqual(f.count(r"\p1"), 4)  # each piece of the mark, once
        self.assertEqual(len([l for l in f.split("\n") if r"\p1" in l and "0:00:00.00,0:00:02.00" in l]), 4)
        self.assertIn(r"\an8\pos(960,@@Y@@)", shell.screen("loading", "c"))  # filled in when the file is written


class Centering(unittest.TestCase):
    def test_the_logo_and_text_are_centred_as_one_group_on_every_screen(self):
        for kind, detail, hint in (("idle", "", "Open http://pi.example:8765 on your phone"), ("loading", "chan", ""), ("loading", "", ""),
                                   ("error", "HTTP error 404", "Pick something else on your phone"), ("ended", "chan", "Pick something else")):
            text = shell.screen(kind, detail, hint)
            ly, ty = shell.layout(text)
            top = ly + shell.GLYPH_OFF
            h = sum(max(int(n) for n in re.findall(r"\\fs(\d+)", ln)) for ln in re.sub(r"^\{[^}]*\}", "", text).split(r"\N")) * shell.LINE
            bottom = ty + h
            self.assertAlmostEqual(top - 0, shell.H - bottom, delta=3, msg=kind)  # equal space above the mark and below the text
            self.assertGreater(ty, top + shell.GLYPH_H)  # the text sits under the mark

    def test_the_text_y_is_written_into_each_event_and_the_mark_follows_the_same_layout(self):
        text = shell.screen("idle", "", "hi")
        f = shell.ass_file([(0, 1, text)])
        ly, ty = shell.layout(text)
        self.assertIn(r"\an8\pos(960,%d)" % ty, f)
        self.assertNotIn("@@Y@@", f)
        self.assertIn(r"\pos(830,%d)" % ly, f)

    def test_everything_is_centred_on_the_screen_horizontally(self):
        self.assertIn(r"\pos(960,", shell.screen("idle"))  # text centred on x = 960
        # the mark is 100 units wide at 260% with its artwork centred on unit 50: drawn from x = 960 - 130
        self.assertIn(r"\pos(830,", shell.logo_pieces(100)[0])


class Tint(unittest.TestCase):
    def test_a_site_colour_tints_the_heading_and_the_logo(self):
        t = shell.screen("loading", "chan", accent="18FC53")
        self.assertIn("&H18FC53&", t)
        self.assertNotIn("&HFFB062&", t)
        self.assertIn("18FC53", "".join(shell.logo_pieces(0, "18FC53")))
        self.assertNotIn("FFB062", "".join(shell.logo_pieces(0, "18FC53")))
        self.assertIn("FFB062", "".join(shell.logo_pieces(0)))  # default blue

    def test_errors_keep_their_own_colour(self):
        self.assertNotIn("18FC53", shell.screen("error", "x", accent="18FC53"))

    def test_the_tint_reaches_the_file(self):
        s, popen, sent = make_shell([FakeProc()])
        s.start()
        s.show("loading", "chan", "FF70A9")
        f = [c for c in sent if c[0] == "ass-file"][-1][1]
        self.assertIn("&HFF70A9&", f)


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
        drawn = [c for c in sent if c[0] == "ass-file"]
        self.assertTrue(drawn and "Stream ended" in drawn[-1][1])

    def test_loading_dots_are_written_ahead_and_nothing_redraws(self):
        s, popen, sent = make_shell([FakeProc()])
        s.start()
        sent.clear()
        s.show("loading", "chan")
        time.sleep(0.15)
        self.assertEqual(len([c for c in sent if c[0] == "ass-file"]), 1)  # one file, no per-frame redraws
        f = sent[[c[0] for c in sent].index("ass-file")][1]
        self.assertIn("Loading...", f)
        self.assertIn("Loading", f)
        s.show("playing")

    def test_show_does_nothing_when_the_shell_is_not_running(self):
        s, popen, sent = make_shell([])
        self.assertFalse(s.show("idle"))
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
