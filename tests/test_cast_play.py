import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import cast  # noqa: E402


def target():
    """A CastTarget without its network discovery: only what play() touches."""
    t = object.__new__(cast.CastTarget)
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


if __name__ == "__main__":
    unittest.main()
