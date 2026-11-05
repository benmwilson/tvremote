import os
import sys
import tempfile
import time
import shutil
import os
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import app as server  # noqa: E402


ROKU = {"name": "TV One", "brand": "TCL", "model": "55C803", "size": 55, "power": "PowerOn", "app": "Roku"}
PLAYING = {"running": True, "title": "chan: a very good stream", "source": "Kick", "live": True, "paused": False,
           "position": 42.0, "quality": "1080p60,best", "drops": 1, "url": "https://kick.com/chan"}


def fresh_cards(**providers):
    from targets import Cards
    server.cards = Cards(providers or {"living": server.living_card, "bedroom": server.bedroom_card},
                         order=["living", "bedroom"], timeout=0.3)
    return server.cards


class Targets(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        server.ROKU_IPS.update(living="203.0.113.1", bedroom="203.0.113.2")
        self.patches = [mock.patch.object(server, "pi_status", return_value=dict(PLAYING)),
                        mock.patch.object(server.roku, "info", return_value=dict(ROKU)),
                        mock.patch.object(server, "CAST", None)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        server._roku.clear()
        server._roku_busy.clear()
        server.fetch_roku("living")  # warm the cache the way the background refresh would
        server.fetch_roku("bedroom")

    def test_targets_returns_both_cards_in_order(self):
        fresh_cards().refresh_now()
        cards = self.client.get("/api/targets").get_json()
        self.assertEqual([c["id"] for c in cards], ["living", "bedroom"])
        self.assertEqual(cards[0]["name"], "Living room")
        self.assertEqual(cards[1]["name"], "Bedroom")
        self.assertFalse(cards[1]["reachable"])  # no Chromecast in this test, but the card still exists

    def test_busy_tv_card_shows_now_playing(self):
        fresh_cards().refresh_now()
        living = self.client.get("/api/targets").get_json()[0]
        self.assertTrue(living["reachable"])
        self.assertEqual((living["brand"], living["size"], living["model"]), ("TCL", 55, "55C803"))
        self.assertEqual(living["now"]["title"], "chan: a very good stream")
        self.assertEqual((living["now"]["source"], living["now"]["live"], living["now"]["stream"]), ("Kick", True, "1080p60"))
        self.assertEqual(living["health"]["state"], "smooth")

    def test_starting_tv_card_is_starting_not_idle(self):
        server.pi_status.return_value = {"running": False, "starting": True, "url": "https://kick.com/chan", "source": "Kick"}
        fresh_cards().refresh_now()
        living = self.client.get("/api/targets").get_json()[0]
        self.assertTrue(living["now"]["starting"])
        self.assertEqual(living["health"]["state"], "starting")

    def test_idle_tv_card_has_no_now(self):
        server.pi_status.return_value = {"running": False}
        fresh_cards().refresh_now()
        self.assertIsNone(self.client.get("/api/targets").get_json()[0]["now"])

    def test_failed_playback_shows_its_error_on_the_card(self):
        server.pi_status.return_value = {"running": False, "error": "HTTP error 404 Not Found", "url": "https://kick.com/chan", "source": "Kick"}
        fresh_cards().refresh_now()
        living = self.client.get("/api/targets").get_json()[0]
        self.assertEqual(living["error"], "HTTP error 404 Not Found")
        self.assertIsNone(living["now"])

    def test_slow_roku_does_not_make_the_card_unreachable(self):
        getattr(server, "_roku", {}).clear()
        server.roku.info.side_effect = lambda ip, **kw: time.sleep(5) or dict(ROKU)
        fresh_cards().refresh_now()
        living = self.client.get("/api/targets").get_json()[0]
        self.assertTrue(living["reachable"])  # the Pi player answered; only the TV's own details are late
        self.assertEqual(living["now"]["title"], "chan: a very good stream")

    def test_roku_is_not_polled_on_every_refresh(self):
        server._roku.clear()
        server._roku_busy.clear()
        server.roku.info.reset_mock()  # setUp warmed the cache with the same mock
        cards = fresh_cards()
        for _ in range(5):
            cards.refresh_now()
        time.sleep(0.3)
        self.assertLessEqual(server.roku.info.call_count, 2)  # at most one fetch per TV, not one per refresh

    def test_targets_never_waits_on_a_slow_tv(self):
        fresh_cards(living=server.living_card, bedroom=lambda: time.sleep(5) or {}).refresh_now()
        t0 = time.time()
        cards = self.client.get("/api/targets").get_json()
        self.assertLess(time.time() - t0, 1.0)
        self.assertTrue(cards[0]["reachable"])
        self.assertFalse(cards[1]["reachable"])


class Routes(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    def test_play_redirect_opens_the_pis_remote(self):
        with mock.patch.object(server, "start_player"):
            r = self.client.get("/play?u=https%3A%2F%2Fkick.com%2Fchan")
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["Location"].endswith("/?target=living"), r.headers["Location"])

    def test_unknown_target_is_refused_not_sent_to_the_pi(self):
        with mock.patch.object(server, "stop_player") as sp:
            r = self.client.post("/api/cmd?target=foo", json={"action": "stop"})
        self.assertEqual(r.status_code, 400)
        sp.assert_not_called()
        self.assertEqual(self.client.get("/api/status?target=foo").status_code, 400)

    def test_share_serves_the_page(self):
        r = self.client.get("/share?u=https%3A%2F%2Fkick.com%2Fx")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"<title>", r.data)

    def test_play_to_missing_chromecast_does_not_use_the_pi(self):
        with mock.patch.object(server, "CAST", None), mock.patch.object(server, "start_player") as sp:
            r = self.client.post("/api/play?target=bedroom", json={"url": "https://kick.com/chan"})
        self.assertEqual(r.status_code, 503)
        self.assertFalse(r.get_json()["ok"])
        self.assertIn("Chromecast", r.get_json()["error"])  # readable, not "503 Service Unavailable: ..."
        sp.assert_not_called()


class PausedOverlay(unittest.TestCase):
    """Pausing leaves a 'Paused' overlay on the TV until you resume. It uses overlay id 2; the chat overlay uses id 1."""

    def setUp(self):
        self.client = server.app.test_client()
        self.sent = []

    def pause(self, now_paused):
        def fake_mpv(cmd, timeout=1.0):
            self.sent.append(cmd)
            return {"error": "success"}
        props = {"pause": now_paused, "media-title": "chan: a stream"}
        with mock.patch.object(server, "mpv", fake_mpv), mock.patch.object(server, "prop", lambda n: props.get(n)):
            return self.client.post("/api/cmd", json={"action": "pause"})

    def test_pausing_draws_a_persistent_overlay(self):
        self.assertEqual(self.pause(True).status_code, 200)
        drawn = [c for c in self.sent if c[0] == "osd-overlay"]
        self.assertEqual(len(drawn), 1)
        self.assertEqual(drawn[0][1:3], [2, "ass-events"])
        self.assertIn("Paused", drawn[0][3])
        self.assertIn("chan: a stream", drawn[0][3])

    def test_resuming_clears_the_overlay(self):
        self.pause(False)
        self.assertIn(["osd-overlay", 2, "none"], self.sent)

    def test_the_overlay_text_cannot_inject_ass_tags(self):
        props = {"pause": True, "media-title": r"bad {\p1}title \Nmore"}
        with mock.patch.object(server, "mpv", lambda c, timeout=1.0: self.sent.append(c) or {"error": "success"}), \
                mock.patch.object(server, "prop", lambda n: props.get(n)):
            self.client.post("/api/cmd", json={"action": "pause"})
        text = [c for c in self.sent if c[0] == "osd-overlay"][0][3]
        self.assertNotIn(r"{\p1}", text.split("title")[0].split("bad")[-1])  # the title's own braces and backslashes are stripped
        self.assertIn("bad p1title Nmore", text)


class LiveFacts(unittest.TestCase):
    """Is a channel live, since when, and playing what: for the favourites and the player."""

    def test_only_channels_are_checked(self):
        self.assertTrue(server.is_channel("https://kick.com/chan"))
        self.assertTrue(server.is_channel("https://www.twitch.tv/xqc"))
        self.assertFalse(server.is_channel("https://www.twitch.tv/videos/123"))
        self.assertFalse(server.is_channel("https://www.youtube.com/watch?v=abc"))
        self.assertFalse(server.is_channel("https://kick.com/"))

    def test_a_live_channel_reports_game_title_and_uptime(self):
        j = {"streams": {"best": {"url": "https://cdn.example/p.m3u8"}}, "metadata": {"category": "GTA V", "title": "RP"}}
        with mock.patch.object(server, "run_streamlink_json", return_value=j), mock.patch.object(server, "live_seconds", return_value=4000.0):
            self.assertEqual(server.check_live("https://kick.com/chan"), {"live": True, "game": "GTA V", "title": "RP", "uptime_s": 4000.0})

    def test_an_offline_channel_is_reported_offline(self):
        with mock.patch.object(server, "run_streamlink_json", return_value={"error": "No playable streams found on this URL: x"}):
            self.assertEqual(server.check_live("https://kick.com/chan"), {"live": False})

    def test_a_check_that_could_not_run_is_unknown(self):
        with mock.patch.object(server, "run_streamlink_json", return_value=None):
            self.assertIsNone(server.check_live("https://kick.com/chan"))
        self.assertIsNone(server.check_live("https://www.youtube.com/watch?v=abc"))

    def test_the_uptime_comes_from_the_playlists_own_elapsed_time(self):
        j = {"streams": {"best": {"url": "https://cdn.example/p.m3u8"}}}
        kick = b"#EXTM3U\n#EXT-X-NET-LIVE-VIDEO-ELAPSED-SECS:48157.900\n#EXTINF:4,\nseg.ts\n"
        with mock.patch.object(server.hlsproxy, "_get", return_value=kick):
            self.assertAlmostEqual(server.live_seconds(j), 48157.9)
        with mock.patch.object(server.hlsproxy, "_get", return_value=b"#EXTM3U\n#EXT-X-TWITCH-ELAPSED-SECS:90.0\n"):
            self.assertEqual(server.live_seconds(j), 90.0)
        with mock.patch.object(server.hlsproxy, "_get", return_value=b"#EXTM3U\n"):
            self.assertIsNone(server.live_seconds(j))

    def test_favourites_carry_their_live_status(self):
        class W:
            def poke(self): return None
            def snapshot(self): return {"https://kick.com/chan": {"live": True, "checked": 100, "last_live": 100, "game": "GTA V"}}
            def live_for(self, url): return 3700
        favs = [{"name": "chan", "url": "https://kick.com/chan", "source": "Kick"}, {"name": "x", "url": "https://kick.com/x", "source": "Kick"}]
        with mock.patch.object(server, "livewatch", W()), mock.patch.object(server, "load_favs", return_value=favs):
            out = server.app.test_client().get("/api/favourites").get_json()
        self.assertEqual((out[0]["live"], out[0]["game"], out[0]["live_for"], out[0]["last_live"]), (True, "GTA V", 3700, 100))
        self.assertIsNone(out[1]["live"])

    def test_the_playing_stream_reports_its_game_and_uptime(self):
        class W:
            def snapshot(self): return {"https://kick.com/chan": {"game": "GTA V"}}
            def live_for(self, url): return 3700
        with mock.patch.object(server, "livewatch", W()), mock.patch.dict(server.state, {"url": "https://kick.com/chan", "proc": None}),                 mock.patch.object(server, "prop", lambda n: 1 if n == "pid" else None):
            data = server.pi_status()
        self.assertEqual((data["game"], data["live_for"]), ("GTA V", 3700))


class FakeHome:
    def __init__(self):
        self.shown, self.started, self.stopped, self.vo, self.error, self._running = [], 0, 0, "wlshm", None, True

    def show(self, kind, detail=""):
        self.shown.append((kind, detail))
        return True

    def running(self):
        return self._running

    def start(self):
        self.started += 1
        self._running = True
        return True

    def stop(self):
        self.stopped += 1
        self._running = False


class ShellFlow(unittest.TestCase):
    """The TV's home screen follows what the player does."""

    def setUp(self):
        self.client = server.app.test_client()
        self.home = FakeHome()
        tmp = tempfile.mkdtemp()
        for p in (mock.patch.object(server, "HOME", self.home),
                  mock.patch.object(server, "SETTINGS_FILE", os.path.join(tmp, "settings.json")),
                  mock.patch.object(server, "LOG_FILE", os.path.join(tmp, "player.log")),
                  mock.patch.dict(server.settings, {"shell": True}),
                  mock.patch.dict(server.state, {"proc": None, "url": "https://kick.com/chan"})):
            p.start()
            self.addCleanup(p.stop)

    def watch(self, returncode, error, replaced=False):
        proc = mock.Mock()
        proc.poll.return_value = returncode
        proc.returncode = returncode
        server.state["proc"] = mock.Mock() if replaced else proc
        with mock.patch.object(server, "last_error", return_value=error):
            server._watch(proc)

    def test_a_new_stream_shows_loading_before_the_old_one_stops(self):
        order = []
        self.home.show = lambda kind, detail="": order.append("screen:" + kind) or True
        with mock.patch.object(server, "stop_player", side_effect=lambda: order.append("stop")),                 mock.patch.object(server.subprocess, "Popen"), mock.patch.object(server, "_watch"),                 mock.patch.object(server, "chat_sync"), mock.patch.object(server, "switch_tv_to_pi"):
            server.start_player("https://kick.com/chan")
        self.assertEqual(order[:2], ["screen:loading", "stop"])

    def test_changing_quality_shows_the_quality_screen_first(self):
        order = []
        self.home.show = lambda kind, detail="": order.append((kind, detail)) or True
        with mock.patch.object(server, "stop_player", side_effect=lambda: order.append("stop")),                 mock.patch.object(server.subprocess, "Popen"), mock.patch.object(server, "_watch"),                 mock.patch.object(server, "_stall_monitor"), mock.patch.object(server, "chat_sync"),                 mock.patch.object(server, "switch_tv_to_pi"):
            server.start_player("https://kick.com/chan", "720p60", "quality")
        self.assertEqual(order[:2], [("quality", "chan  ·  720p60"), "stop"])

    def test_the_restart_command_asks_for_the_quality_screen(self):
        server.state.update(url="https://kick.com/chan", quality="1080p60,best")
        with mock.patch.object(server, "start_player") as sp:
            self.client.post("/api/cmd", json={"action": "restart", "quality": "720p60"})
        sp.assert_called_once_with("https://kick.com/chan", "720p60", "quality")

    def test_a_stall_shows_buffering_on_the_tv_and_is_counted(self):
        proc = mock.Mock()
        proc.poll.side_effect = [None, None, None, None, None, 0]
        stalled = iter([False, True, True, False, False])
        sent = []
        server.state.update(stalls=0, stall_s=0)
        with mock.patch.object(server.time, "sleep"), mock.patch.object(server, "prop", lambda n: next(stalled) if n == "paused-for-cache" else None),                 mock.patch.object(server, "mpv", lambda cmd, timeout=1.0: sent.append(cmd) or {"error": "success"}):
            server._stall_monitor(proc)
        drawn = [c for c in sent if c[0] == "osd-overlay"]
        self.assertEqual(drawn[0][1], 3)
        self.assertIn("Buffering", drawn[0][3])
        self.assertEqual(drawn[-1], ["osd-overlay", 3, "none"])
        self.assertEqual(server.state["stalls"], 1)

    def test_a_stutter_shows_a_lagging_note_that_clears_when_calm(self):
        proc = mock.Mock()
        proc.poll.side_effect = [None] * 8 + [0]
        drops = iter([0, 0, 30, 30, 30, 30, 30, 30])  # a burst of 30 dropped frames, then smooth
        sent = []
        props = lambda n: {"paused-for-cache": False, "pause": False, "demuxer-cache-duration": 6.0}.get(n) if n != "frame-drop-count" else next(drops)
        with mock.patch.object(server.time, "sleep"), mock.patch.object(server, "prop", props),                 mock.patch.object(server, "mpv", lambda cmd, timeout=1.0: sent.append(cmd) or {"error": "success"}):
            server._stall_monitor(proc)
        drawn = [c for c in sent if c[0] == "osd-overlay"]
        self.assertEqual(drawn[0][1], 4)
        self.assertIn("lagging", drawn[0][3])
        self.assertEqual(drawn[-1], ["osd-overlay", 4, "none"])
        self.assertEqual(len(drawn), 2)  # drawn once, cleared once: no redraw while it stays up

    def test_is_lagging_only_on_a_burst_or_a_dry_buffer(self):
        self.assertFalse(server.is_lagging(1, 6.0))
        self.assertTrue(server.is_lagging(8, 6.0))
        self.assertTrue(server.is_lagging(0, 0.3))
        self.assertFalse(server.is_lagging(0, None))

    def test_volume_bar_is_drawn_on_the_tv_and_cleared_after_a_moment(self):
        sent = []
        props = {"volume": 40, "mute": False}
        timers = []
        class T:
            def __init__(self, s, fn): self.fn, self.daemon = fn, False; timers.append(self)
            def start(self): pass
            def cancel(self): pass
        with mock.patch.object(server, "prop", lambda n: props.get(n)), mock.patch.object(server.threading, "Timer", T),                 mock.patch.object(server, "mpv", lambda cmd, timeout=1.0: sent.append(cmd) or {"error": "success"}):
            server.osd_volume()
            self.assertEqual((sent[0][0], sent[0][1]), ("osd-overlay", 5))
            self.assertIn("40", sent[0][3])
            timers[-1].fn()
        self.assertEqual(sent[-1], ["osd-overlay", 5, "none"])

    def test_volume_overlay_shows_muted_and_no_fill_when_muted(self):
        self.assertIn("Muted", server.volume_overlay(40, True))
        self.assertNotIn("FFB26B", server.volume_overlay(40, True))
        self.assertIn("FFB26B", server.volume_overlay(40, False))

    def test_stats_switch_is_saved_and_the_overlay_has_the_numbers(self):
        with mock.patch.object(server, "save_settings") as sv, mock.patch.dict(server.settings, {"stats": False}):
            self.assertFalse(self.client.get("/api/stats").get_json()["enabled"])
            self.assertTrue(self.client.post("/api/stats", json={"enabled": True}).get_json()["enabled"])
            self.assertTrue(server.settings["stats"])
            sv.assert_called()
        text = server.stats_overlay(12, 340, 1.3, 60, 2)
        for bit in ("12/s", "340", "1.3s", "60 fps", "stalls 2"):
            self.assertIn(bit, text)

    def test_stats_overlay_is_only_drawn_when_switched_on(self):
        proc = mock.Mock()
        proc.poll.side_effect = [None] * 4 + [0]
        sent = []
        props = lambda n: {"paused-for-cache": False, "pause": False, "demuxer-cache-duration": 6.0, "frame-drop-count": 5, "estimated-vf-fps": 60}.get(n)
        for on, expect in ((False, 0), (True, 2)):
            sent.clear()
            proc.poll.side_effect = [None] * 4 + [0]
            with mock.patch.dict(server.settings, {"stats": on}), mock.patch.object(server.time, "sleep"), mock.patch.object(server, "prop", props),                     mock.patch.object(server, "mpv", lambda cmd, timeout=1.0: sent.append(cmd) or {"error": "success"}):
                server._stall_monitor(proc)
            self.assertEqual(len([c for c in sent if c[0] == "show-text"]), expect)

    def test_waking_the_tv_selects_its_input_without_waiting(self):
        with mock.patch.object(server, "switch_input") as sw, mock.patch.dict(server.INPUTS, {"living": "HDMI1"}), mock.patch.dict(server.ROKU_IPS, {"living": "203.0.113.1"}):
            r = self.client.post("/api/tv/wake?target=living")
            time.sleep(0.1)
        self.assertEqual(r.get_json(), {"ok": True})
        sw.assert_called_once_with("living")
        self.assertEqual(self.client.post("/api/tv/wake?target=nope").status_code, 400)

    def test_tv_off_stops_a_live_pi_stream_and_pauses_a_video(self):
        sent = []
        mp = lambda cmd, timeout=1.0: sent.append(cmd) or {"error": "success"}
        with mock.patch.object(server, "pi_playing", return_value=True), mock.patch.object(server, "mpv", mp), \
                mock.patch.object(server, "prop", lambda n: False), mock.patch.object(server, "osd_paused"), \
                mock.patch.object(server, "stop_player") as stop, mock.patch.object(server, "screen") as scr:
            with mock.patch.object(server, "pi_status", return_value={"live": True}):
                server.on_tv_off("living")
            stop.assert_called_once()
            scr.assert_called_with("idle")
            stop.reset_mock()
            with mock.patch.object(server, "pi_status", return_value={"live": False}):
                server.on_tv_off("living")
            stop.assert_not_called()
            self.assertIn(["set_property", "pause", True], sent)

    def test_tv_off_pauses_the_chromecast_once(self):
        class C:
            def __init__(s, paused): s.paused, s.calls = paused, []
            def status(s): return {"running": True, "paused": s.paused}
            def cmd(s, a, *x): s.calls.append(a)
        for paused, want in ((False, ["pause"]), (True, [])):
            c = C(paused)
            with mock.patch.object(server, "CAST", c):
                server.on_tv_off("bedroom")
            self.assertEqual(c.calls, want)

    def test_the_off_key_and_a_standby_change_both_trigger_it(self):
        with mock.patch.object(server, "on_tv_off") as off, mock.patch.dict(server.ROKU_IPS, {"living": "203.0.113.1"}), \
                mock.patch.object(server.roku, "key"):
            self.client.post("/api/roku/key?target=living", json={"key": "PowerOff"})
            time.sleep(0.1)
            off.assert_called_once_with("living")
            off.reset_mock()
            server._roku["living"] = (0, {"power": "PowerOn"})
            with mock.patch.object(server.roku, "info", return_value={"power": "Ready"}):
                server.fetch_roku("living")
            time.sleep(0.1)
            off.assert_called_once_with("living")

    def test_login_page_cookie_and_logout(self):
        c = server.app.test_client()
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with mock.patch.object(server, "PASSWORD", "pw123"), mock.patch.object(server.time, "sleep"), mock.patch.object(server, "DATA", d):
            r = c.get("/", headers={"Accept": "text/html"})
            self.assertEqual((r.status_code, r.headers["Location"].split("?")[0]), (303, "/login"))
            self.assertEqual(c.get("/api/favourites").status_code, 401)  # scripts still get a plain 401
            self.assertEqual(c.get("/api/favourites", headers={"Authorization": "Basic " + __import__("base64").b64encode(b":pw123").decode()}).status_code, 200)
            self.assertEqual(c.post("/login", data={"password": "nope"}).status_code, 401)
            self.assertEqual(c.get("/api/favourites").status_code, 401)
            r = c.post("/login", data={"password": "pw123", "next": "//evil.example"})
            self.assertEqual((r.status_code, r.headers["Location"]), (303, "/"))  # no open redirect
            self.assertIn("HttpOnly", r.headers["Set-Cookie"])
            self.assertEqual(c.get("/api/auth").get_json(), {"required": True})  # cookie is now set
            old = c.get_cookie("tv_auth").value
            c.get("/logout")
            self.assertEqual(c.get("/api/favourites").status_code, 401)
            c.set_cookie("tv_auth", old)  # a copied cookie is dead after logout
            self.assertEqual(c.get("/api/favourites").status_code, 401)
            self.assertNotIn(old, open(os.path.join(d, "sessions.json")).read())  # only hashes on disk
            tok = server.new_session()
            with mock.patch.object(server, "_sess_save"):  # the disk write fails: logout must still work
                server.drop_session(tok)
            self.assertFalse(server.valid_session(tok))
            before = len(server._revoked)
            for junk in ("a", "b", "c"):
                server.drop_session(junk)
            self.assertEqual(len(server._revoked), before)  # unknown tokens are not remembered
            tok2 = server.new_session()
            self.assertTrue(server.valid_session(tok2))
            with mock.patch.object(server, "PASSWORD", "changed"):  # a new password voids every session
                self.assertFalse(server.valid_session(tok2))

    def test_refresh_has_its_own_screen_and_quality_keeps_its_own(self):
        server.state.update(url="https://kick.com/chan", quality="1080p60,best")
        with mock.patch.object(server, "start_player") as sp:
            self.client.post("/api/cmd", json={"action": "restart"})
            sp.assert_called_with("https://kick.com/chan", "1080p60,best", "refresh")
            self.client.post("/api/cmd", json={"action": "restart", "quality": "720p60"})
            sp.assert_called_with("https://kick.com/chan", "720p60", "quality")

    def test_favourites_can_be_reordered(self):
        favs = [{"name": n, "url": "https://kick.com/" + n, "source": "Kick"} for n in "abc"]
        saved = []
        with mock.patch.object(server, "load_favs", return_value=list(favs)), mock.patch.object(server, "save_favs", side_effect=saved.append):
            r = self.client.post("/api/favourites/order", json={"urls": ["https://kick.com/c", "https://kick.com/a"]})
            self.assertEqual([f["name"] for f in saved[-1]], ["c", "a", "b"])  # listed first, the rest keep their place
            self.assertEqual(self.client.post("/api/favourites/order", json={"urls": "x"}).status_code, 400)
        self.assertEqual(r.status_code, 200)

    def test_status_reports_how_often_the_stream_stalled(self):
        server.state.update(stalls=2, stall_s=7.4, proc=None)
        with mock.patch.object(server, "prop", lambda n: 1 if n == "pid" else None):
            data = server.pi_status()
        self.assertEqual((data["stalls"], data["stall_s"]), (2, 7))

    def test_stopping_shows_the_idle_screen(self):
        with mock.patch.object(server, "stop_player"):
            self.client.post("/api/cmd", json={"action": "stop"})
        self.assertEqual(self.home.shown[-1], ("idle", ""))

    def test_stopping_forgets_the_last_stream_and_its_error(self):
        server.state.update(url="https://kick.com/chan", source="Kick")
        with mock.patch.object(server, "stop_player"):
            self.client.post("/api/cmd", json={"action": "stop"})
        self.assertIsNone(server.state["url"])
        self.assertIsNone(server.state["source"])

    def test_a_player_that_dies_shows_why(self):
        self.watch(1, "HTTP error 404 Not Found")
        self.assertEqual(self.home.shown[-1], ("error", "That link wasn't found (404)"))

    def test_a_channel_that_is_not_live_says_so(self):
        server.state["url"] = "https://kick.com/capped"
        self.watch(1, "error: No playable streams found on this URL: https://kick.com/capped")
        self.assertEqual(self.home.shown[-1], ("offline", "capped"))

    def test_a_stream_that_finishes_shows_ended(self):
        self.watch(0, None)
        self.assertEqual(self.home.shown[-1], ("ended", "chan"))

    def test_a_replaced_player_shows_nothing(self):
        self.watch(1, "boom", replaced=True)
        self.assertEqual(self.home.shown, [])

    def test_playing_hides_the_loading_screen(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        server.state["proc"] = mock.Mock()  # replaced as soon as it ends, so only "playing" is shown
        with mock.patch.object(server, "prop", return_value=1234):
            server._watch(proc)
        self.assertEqual(self.home.shown, [("playing", "")])

    def test_error_text_is_friendly(self):
        d = server.describe_error
        self.assertEqual(d("error: No playable streams found on this URL: x"), "Isn't live right now")
        self.assertEqual(d("HTTP error 404 Not Found"), "That link wasn't found (404)")
        self.assertEqual(d("The web client only works when logged-in"), "That needs a login, which isn't supported")
        self.assertEqual(d("something odd"), "something odd")
        self.assertIsNone(d(None))

    def test_the_switch_persists_and_starts_or_stops_the_screen(self):
        r = self.client.post("/api/shell", json={"enabled": False})
        self.assertEqual((r.status_code, r.get_json()["enabled"]), (200, False))
        self.assertEqual(self.home.stopped, 1)
        self.assertFalse(server.load_settings()["shell"])
        r = self.client.post("/api/shell", json={"enabled": True})
        self.assertTrue(r.get_json()["enabled"])
        self.assertEqual(self.home.started, 1)
        self.assertTrue(self.client.get("/api/shell").get_json()["running"])

    def test_shell_state_reports_what_the_window_is_doing(self):
        self.home.props = lambda: {"fullscreen": True, "pause": False}
        self.assertEqual(self.client.get("/api/shell").get_json()["props"], {"fullscreen": True, "pause": False})

    def test_screenshot_of_the_home_screen_is_refused_when_it_is_not_running(self):
        with mock.patch.object(server.mpvipc, "send", return_value=None):
            self.assertEqual(self.client.get("/api/shell/screenshot").status_code, 502)

    def test_screenshot_of_the_home_screen_is_served(self):
        shot = os.path.join(tempfile.mkdtemp(), "shell.png")
        open(shot, "wb").write(b"PNG fake")
        with mock.patch.object(server, "SHELL_SHOT", shot), mock.patch.object(server.mpvipc, "send", return_value={"error": "success"}):
            r = self.client.get("/api/shell/screenshot")
        self.assertEqual((r.status_code, r.mimetype), (200, "image/png"))

    def test_the_real_screen_can_be_captured_for_debugging(self):
        shot = os.path.join(tempfile.mkdtemp(), "screen.png")
        open(shot, "wb").write(b"PNG fake")
        with mock.patch.object(server, "SCREEN_SHOT", shot), mock.patch.object(server.subprocess, "run") as run:
            r = self.client.get("/api/screen.png")
        self.assertEqual((r.status_code, r.mimetype), (200, "image/png"))
        self.assertEqual(run.call_args[0][0][0], "grim")

    def test_a_failed_screen_capture_is_a_clear_error(self):
        with mock.patch.object(server.subprocess, "run", side_effect=FileNotFoundError("grim")):
            r = self.client.get("/api/screen.png")
        self.assertEqual(r.status_code, 502)
        self.assertFalse(r.get_json()["ok"])

    def test_with_the_screen_off_nothing_is_drawn(self):
        server.settings["shell"] = False
        server.screen("loading", "x")
        self.assertEqual(self.home.shown, [])


class InputSwitch(unittest.TestCase):
    """Playing must not re-select an input the TV is already showing (it relaunches the input and flashes the screen)."""

    def setUp(self):
        for p in (mock.patch.dict(server.ROKU_IPS, {"living": "203.0.113.1"}), mock.patch.dict(server.INPUTS, {"living": "HDMI1"}),
                  mock.patch.object(server.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)

    def run_switch(self, power, app_id):
        with mock.patch.object(server.roku, "info", return_value={"power": power, "app_id": app_id}),                 mock.patch.object(server.roku, "key") as key:
            server.switch_input("living")
        return [c.args[1] for c in key.call_args_list]

    def test_no_switch_when_the_tv_is_already_on_the_input(self):
        self.assertEqual(self.run_switch("PowerOn", "tvinput.hdmi1"), [])

    def test_switches_when_the_tv_is_on_another_input(self):
        self.assertEqual(self.run_switch("PowerOn", "tvinput.hdmi2"), ["InputHDMI1"])

    def test_wakes_a_tv_that_is_off_then_switches(self):
        self.assertEqual(self.run_switch("Ready", "tvinput.hdmi1"), ["PowerOn", "InputHDMI1"])

    def test_cec_is_skipped_when_the_tv_is_already_on_the_input(self):
        with mock.patch.object(server.roku, "info", return_value={"power": "PowerOn", "app_id": "tvinput.hdmi1"}),                 mock.patch.object(server, "cec") as cec:
            server.switch_tv_to_pi()
        cec.assert_not_called()

    def test_cec_is_sent_when_the_tv_is_on_something_else(self):
        with mock.patch.object(server.roku, "info", return_value={"power": "PowerOn", "app_id": "tvinput.hdmi3"}),                 mock.patch.object(server, "cec") as cec:
            server.switch_tv_to_pi()
        self.assertTrue(cec.called)


if __name__ == "__main__":
    unittest.main()
