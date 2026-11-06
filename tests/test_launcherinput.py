import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import launcher  # noqa: E402
import launcherinput  # noqa: E402


def ev(typ, code, value):
    return struct.pack("llHHi", 0, 0, typ, code, value)


class ParseTests(unittest.TestCase):
    def test_only_key_events_are_read(self):
        data = ev(1, 108, 1) + ev(0, 0, 0) + ev(4, 4, 458834) + ev(1, 108, 0)  # a key down, a sync, a scancode report, a key up
        self.assertEqual(launcherinput.parse_events(data), [(108, 1), (108, 0)])

    def test_a_partial_event_is_ignored(self):
        self.assertEqual(launcherinput.parse_events(ev(1, 28, 1)[:10]), [])

    def test_every_named_key_is_one_the_launcher_understands(self):
        for code, name in launcherinput.KEY_NAMES.items():
            self.assertIsNotNone(launcher.normal_key(name), (code, name))


if __name__ == "__main__":
    unittest.main()
