import os
import shutil
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import cast  # noqa: E402


def target():
    """A CastTarget without its network discovery: only what play() touches."""
    t = object.__new__(cast.CastTarget)
    t.data_dir = tempfile.mkdtemp()
    t.lock = threading.RLock()
    t.resolve = lambda url: ("Kick", "streamlink")
    t.title_fn = lambda url: url
    t.heights, t.sl_default, t.via_mode, t.port = ("1080", "720"), "1080p60,best", "direct", "8765"
    t.cast = mock.MagicMock(app_id=cast.APP_MEDIA_RECEIVER)
    t.state = {"url": "https://kick.com/4head", "source": "Kick", "player": "streamlink", "quality": "1080p60,best", "error": None,
               "started": 100.0, "via": "direct", "proxy_tried": False, "first_play": 105.0}
    t._connect = lambda: t.cast
    return t


class FailedStartTest(unittest.TestCase):
    def test_an_offline_channel_leaves_what_is_playing_alone(self):
        t = target()
        before = dict(t.state)
        with mock.patch.object(t, "_media_url", side_effect=cast.CastError("No playable streams found on this URL")), \
                mock.patch.object(cast.hlsproxy, "clear") as clear:
            with self.assertRaisesRegex(cast.CastError, "No playable streams"):
                t.play("https://kick.com/capped")
        self.assertEqual(t.state, before)  # still describes the stream that is still on the TV
        clear.assert_not_called()  # and its relay session (if any) keeps running
        t.cast.media_controller.play_media.assert_not_called()

    def test_a_good_start_replaces_the_state(self):
        t = target()
        with mock.patch.object(t, "_media_url", return_value=("http://x/stream.m3u8", "application/x-mpegURL", "LIVE")), \
                mock.patch.object(cast.hlsproxy, "clear"):
            t.play("https://kick.com/capped")
        self.assertEqual(t.state["url"], "https://kick.com/capped")
        t.cast.media_controller.play_media.assert_called_once()


MEDIA = "http://example.test/live.m3u8"


class SurvivesARestart(unittest.TestCase):
    def setUp(self):
        self.t = target()
        self.addCleanup(shutil.rmtree, self.t.data_dir, True)
        self.t.state.update(url=None, source=None, player=None, quality=None, started=None, via=None, first_play=None)

    def save(self, **over):
        self.t.state.update(url="https://kick.com/4head", source="Kick", player="streamlink", quality="1080p60,best", started=100.0, mode="direct",
                            content=MEDIA, **over)
        self.t._save_playing()
        self.t.state.update(url=None, source=None, content=None)  # the app restarted: memory is empty

    def playing(self, content=MEDIA, state="PLAYING", app=cast.APP_MEDIA_RECEIVER):
        self.t.cast.app_id = app
        self.t.cast.media_controller.status = SimpleNamespace(player_state=state, content_id=content)

    def test_a_start_is_written_down_and_a_stop_removes_it(self):
        with mock.patch.object(self.t, "_media_url", return_value=(MEDIA, "application/x-mpegURL", "LIVE")), mock.patch.object(cast.hlsproxy, "clear"):
            self.t.play("https://kick.com/capped")
        self.assertTrue(os.path.exists(self.t._state_file()))
        with mock.patch.object(cast.hlsproxy, "clear"):
            self.t.cmd("stop")
        self.assertFalse(os.path.exists(self.t._state_file()))

    def test_still_playing_the_same_stream_is_taken_back(self):
        self.save()
        self.playing()
        self.assertTrue(self.t._adopt_once())
        self.assertEqual((self.t.state["url"], self.t.state["source"], self.t.state["quality"]), ("https://kick.com/4head", "Kick", "1080p60,best"))
        self.assertIsNone(self.t.state["error"])

    def test_paused_counts_as_still_playing(self):
        self.save()
        self.playing(state="PAUSED")
        self.assertTrue(self.t._adopt_once())

    def test_something_else_now_on_the_chromecast_is_not_claimed(self):
        self.save()
        self.playing(content="http://example.test/someone-elses-cast.mp4")  # a phone cast something since
        self.assertFalse(self.t._adopt_once())
        self.assertIsNone(self.t.state["url"])
        self.assertFalse(os.path.exists(self.t._state_file()))  # the old note is dropped, so it can't mislead later

    def test_a_chromecast_that_stopped_is_not_claimed(self):
        self.save()
        self.playing(state="IDLE")
        self.assertFalse(self.t._adopt_once())
        self.assertFalse(os.path.exists(self.t._state_file()))

    def test_youtube_is_recognised_by_its_app_being_open(self):
        self.t.state.update(url="https://youtu.be/abc", source="YouTube", player="ytdlp", quality="1080", started=100.0, mode="direct", content=None)
        self.t._save_playing()
        self.t.state.update(url=None, source=None)  # the app restarted
        self.playing(content=None, state="PLAYING", app=cast.APP_YOUTUBE)
        self.assertTrue(self.t._adopt_once())
        self.assertEqual((self.t.state["url"], self.t.state["source"]), ("https://youtu.be/abc", "YouTube"))
        self.playing(content=None, state="PLAYING", app=cast.APP_MEDIA_RECEIVER)  # but not if another app is in front now
        self.t.state.update(url=None, source=None)
        self.assertFalse(self.t._adopt_once())

    def test_nothing_saved_means_nothing_to_take_back(self):
        self.playing()
        self.assertFalse(self.t._adopt_once())

    def test_a_relayed_stream_is_not_taken_back_because_its_relay_is_gone(self):
        patches = [mock.patch.object(self.t, "_lan_ip", return_value="10.0.0.2"), mock.patch.object(self.t, "device", return_value=SimpleNamespace(host="10.0.0.9")),
                   mock.patch.object(cast.hlsproxy, "set_upstream"), mock.patch.object(cast.hlsproxy, "token", return_value="tok")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.t._start_proxy("https://kick.com/capped", "Kick", "1080p60,best", MEDIA)
        self.assertFalse(os.path.exists(self.t._state_file()))


if __name__ == "__main__":
    unittest.main()
