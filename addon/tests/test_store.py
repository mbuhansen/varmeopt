import json
import tempfile
import unittest
from pathlib import Path

from varmeopt.options import Options
from varmeopt.store import Store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="varmeopt-test-"))
        self.store = Store(self.tmp)

    def test_save_and_load_round_trip(self):
        data = {"40": {"5": {"cop": 4.0, "count": 12}}}
        self.store.save("t.json", data)

        self.assertTrue(self.store.exists("t.json"))
        self.assertEqual(self.store.load("t.json"), data)

    def test_load_missing_returns_default(self):
        self.assertIsNone(self.store.load("nope.json"))
        self.assertEqual(self.store.load("nope.json", {}), {})

    def test_load_corrupt_returns_default_instead_of_raising(self):
        self.store.path("bad.json").write_text("{ ikke json", encoding="utf-8")

        self.assertEqual(self.store.load("bad.json", {"fallback": True}), {"fallback": True})

    def test_danish_characters_survive(self):
        self.store.save("d.json", {"note": "fremløb målinger på tanken"})

        raw = self.store.path("d.json").read_text(encoding="utf-8")
        self.assertIn("fremløb", raw)
        self.assertEqual(self.store.load("d.json")["note"], "fremløb målinger på tanken")

    def test_save_leaves_no_temp_files_behind(self):
        self.store.save("t.json", {"a": 1})
        self.store.save("t.json", {"a": 2})

        names = sorted(p.name for p in self.tmp.iterdir())
        self.assertEqual(names, ["t.json"])

    def test_overwrite_is_atomic_enough_to_keep_valid_json(self):
        self.store.save("t.json", {"a": 1})
        self.store.save("t.json", {"a": 2})

        self.assertEqual(json.loads(self.store.path("t.json").read_text("utf-8")), {"a": 2})

    def test_backup_copies_current_content(self):
        self.store.save("t.json", {"a": 1})
        dst = self.store.backup("t.json", "foer")

        self.assertIsNotNone(dst)
        self.assertEqual(json.loads(dst.read_text("utf-8")), {"a": 1})

    def test_backup_of_missing_file_is_none(self):
        self.assertIsNone(self.store.backup("nope.json", "foer"))


class OptionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="varmeopt-test-"))

    def test_defaults_when_no_file(self):
        opts = Options.load(self.tmp / "ingen.json")

        self.assertEqual(opts.cycle_seconds, 60)
        self.assertEqual(opts.log_level, "info")

    def test_file_overrides_defaults(self):
        path = self.tmp / "options.json"
        path.write_text(json.dumps({"cycle_seconds": 120}), encoding="utf-8")
        opts = Options.load(path)

        self.assertEqual(opts.cycle_seconds, 120)
        self.assertEqual(opts.log_level, "info")

    def test_corrupt_file_falls_back_to_defaults(self):
        path = self.tmp / "options.json"
        path.write_text("{ ikke json", encoding="utf-8")

        self.assertEqual(Options.load(path).cycle_seconds, 60)

    def test_trailing_slash_stripped_from_url(self):
        path = self.tmp / "options.json"
        path.write_text(json.dumps({"nodered_url": "http://x:1880/"}), encoding="utf-8")

        self.assertEqual(Options.load(path).nodered_url, "http://x:1880")


if __name__ == "__main__":
    unittest.main()
