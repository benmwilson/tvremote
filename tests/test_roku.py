import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import roku  # noqa: E402

DEVICE = ("<device-info><vendor-name>TCL</vendor-name><model-name>43S425-CA</model-name>{size}"
          "<user-device-name>X</user-device-name><power-mode>PowerOn</power-mode></device-info>")
ACTIVE = '<active-app><app id="837">YouTube</app></active-app>'


def fake_call(size_tag):
    def call(ip, path, method="GET", timeout=4):
        return ACTIVE if "active-app" in path else DEVICE.format(size=size_tag)
    return call


class RokuInfo(unittest.TestCase):
    def test_info_parses_brand_model_size(self):
        with mock.patch.object(roku, "_call", fake_call("<screen-size>43</screen-size>")):
            i = roku.info("203.0.113.5")
        self.assertEqual(i["brand"], "TCL")
        self.assertEqual(i["model"], "43S425-CA")
        self.assertEqual(i["size"], 43)
        self.assertEqual(i["name"], "X")
        self.assertEqual(i["power"], "PowerOn")
        self.assertEqual(i["app"], "YouTube")

    def test_info_reports_the_id_of_what_is_on_screen(self):
        def call(ip, path, method="GET", timeout=4):
            return '<active-app><app id="tvinput.hdmi1" type="tvin">Nintendo Switch</app></active-app>' if "active-app" in path else DEVICE.format(size="")
        with mock.patch.object(roku, "_call", call):
            i = roku.info("203.0.113.5")
        self.assertEqual((i["app_id"], i["app"]), ("tvinput.hdmi1", "Nintendo Switch"))

    def test_info_missing_fields_are_none(self):
        with mock.patch.object(roku, "_call", fake_call("")):
            self.assertIsNone(roku.info("203.0.113.5")["size"])

    def test_info_non_numeric_size_is_none(self):
        with mock.patch.object(roku, "_call", fake_call("<screen-size>big</screen-size>")):
            self.assertIsNone(roku.info("203.0.113.5")["size"])


if __name__ == "__main__":
    unittest.main()
