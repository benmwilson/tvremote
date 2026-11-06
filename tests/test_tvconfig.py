import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import tvconfig  # noqa: E402

OLD = {"living": {"name": "Living room", "roku": "192.168.1.141", "input": "HDMI1", "enabled": True},
       "bedroom": {"name": "Bedroom", "roku": "192.168.1.142", "input": "HDMI1", "enabled": True, "device": "Bedroom TV"}}


def tv(i, name, roku="", pi=None, cc=None):
    return {"id": f"tv{i}", "name": name, "roku": roku, "pi": pi, "chromecast": cc}


class Migrate(unittest.TestCase):
    def test_two_slots_with_different_rokus_make_two_tvs(self):
        tvs = tvconfig.migrate(OLD)
        self.assertEqual([t["name"] for t in tvs], ["Living room", "Bedroom"])
        self.assertEqual(tvs[0]["pi"], {"input": "HDMI1"})
        self.assertIsNone(tvs[0]["chromecast"])
        self.assertEqual(tvs[1]["chromecast"], {"device": "Bedroom TV", "input": "HDMI1"})
        self.assertIsNone(tvs[1]["pi"])

    def test_both_slots_on_one_roku_make_one_tv_holding_both(self):
        old = {**OLD, "living": {**OLD["living"], "roku": "192.168.1.142", "input": "HDMI3"}}
        tvs = tvconfig.migrate(old)
        self.assertEqual(len(tvs), 1)
        self.assertEqual((tvs[0]["pi"], tvs[0]["chromecast"]["device"]), ({"input": "HDMI3"}, "Bedroom TV"))

    def test_disabled_slots_contribute_nothing(self):
        old = {"living": {**OLD["living"], "enabled": False}, "bedroom": OLD["bedroom"]}
        self.assertEqual([t["name"] for t in tvconfig.migrate(old)], ["Bedroom"])
        self.assertEqual(tvconfig.migrate({}), [])


class Clean(unittest.TestCase):
    def test_a_good_list_is_normalized_and_new_tvs_get_ids(self):
        out = tvconfig.clean_tvs({"tvs": [{"name": "  Living   room ", "roku": "192.168.1.141", "pi": {"input": "HDMI2"}},
                                          {"id": "tv9", "name": "Bedroom", "chromecast": {"device": "Bedroom TV"}}]})
        self.assertEqual(out[0]["name"], "Living room")
        self.assertEqual(out[0]["pi"], {"input": "HDMI2"})
        self.assertEqual(out[1]["id"], "tv9")
        self.assertNotEqual(out[0]["id"], "tv9")
        self.assertEqual(out[1]["chromecast"], {"device": "Bedroom TV", "input": ""})

    def test_bad_lists_are_refused_with_a_sentence(self):
        cases = [
            {"tvs": [{"name": "A", "pi": {}}, {"name": "B", "pi": {}}]},                                   # two Pis
            {"tvs": [{"name": "A", "chromecast": {}}, {"name": "B", "chromecast": {}}]},                    # two Chromecasts
            {"tvs": [{"name": "Den"}, {"name": "den"}]},                                                    # same name
            {"tvs": [{"name": "A", "roku": "8.8.8.8"}]},                                                    # not on the home network
            {"tvs": [{"name": "A", "pi": {"input": "HDMI9"}}]},                                             # no such input
            {"tvs": [{"name": "   "}]},                                                                     # no name
            {"tvs": "nope"}, {},
        ]
        for raw in cases:
            with self.assertRaises(ValueError, msg=str(raw)):
                tvconfig.clean_tvs(raw)

    def test_long_names_are_trimmed(self):
        self.assertEqual(len(tvconfig.clean_tvs({"tvs": [{"name": "x" * 60}]})[0]["name"]), 24)


class Endpoints(unittest.TestCase):
    def test_labels_get_a_suffix_only_when_one_tv_has_both(self):
        both = tvconfig.endpoints([tv(1, "Bedroom", "192.168.1.142", {"input": "HDMI3"}, {"device": "Bedroom TV", "input": "HDMI1"})])
        self.assertEqual((both["living"]["label"], both["bedroom"]["label"]), ("Bedroom · Pi", "Bedroom · Chromecast"))
        self.assertEqual((both["living"]["input"], both["bedroom"]["input"], both["bedroom"]["device"]), ("HDMI3", "HDMI1", "Bedroom TV"))
        apart = tvconfig.endpoints([tv(1, "Den", "192.168.1.141", {"input": "HDMI1"}), tv(2, "Bedroom", "192.168.1.142", None, {"device": "X", "input": ""})])
        self.assertEqual((apart["living"]["label"], apart["bedroom"]["label"]), ("Den", "Bedroom"))

    def test_an_endpoint_with_no_tv_is_disabled(self):
        e = tvconfig.endpoints([tv(1, "Den", "192.168.1.141", {"input": "HDMI1"})])
        self.assertTrue(e["living"]["enabled"])
        self.assertEqual((e["bedroom"]["enabled"], e["bedroom"]["roku"], e["bedroom"]["label"]), (False, "", "Bedroom"))
        self.assertFalse(tvconfig.endpoints([])["living"]["enabled"])


class Files(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_load_migrates_when_tvs_json_is_missing_and_leaves_targets_json_alone(self):
        p = os.path.join(self.dir, "targets.json")
        with open(p, "w") as f:
            json.dump(OLD, f)
        self.assertEqual(len(tvconfig.load_tvs(self.dir)), 2)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "tvs.json")))  # loading alone writes nothing
        with open(p) as f:
            self.assertEqual(json.load(f), OLD)

    def test_save_then_load_round_trips_and_wins_over_the_old_file(self):
        with open(os.path.join(self.dir, "targets.json"), "w") as f:
            json.dump(OLD, f)
        saved = tvconfig.save_tvs(self.dir, tvconfig.clean_tvs({"tvs": [{"name": "Only", "pi": {"input": "HDMI2"}}]}))
        self.assertEqual(tvconfig.load_tvs(self.dir), saved)
        self.assertEqual(len(saved), 1)

    def test_a_fresh_install_has_no_tvs(self):
        self.assertEqual(tvconfig.load_tvs(self.dir), [])


if __name__ == "__main__":
    unittest.main()
