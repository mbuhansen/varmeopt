import os
import tempfile
import time
import unittest
from pathlib import Path

import bootstrap


class BootstrapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="varmeopt-bootstrap-"))
        os.environ["VARMEOPT_CODE_DIR"] = str(self.tmp)

    def tearDown(self):
        os.environ.pop("VARMEOPT_CODE_DIR", None)

    # ------------------------------------------------------------- hjælpere

    def plant(self, folder: str, marker: str) -> None:
        path = self.tmp / folder
        path.mkdir(parents=True, exist_ok=True)
        (path / "__main__.py").write_text(f"# {marker}\n", encoding="utf-8")

    def mark(self, age_seconds: float = 0.0) -> None:
        path = self.tmp / bootstrap.MARKER_NAME
        path.write_text("1", encoding="utf-8")
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))

    def live_says(self) -> str:
        return (self.tmp / bootstrap.PACKAGE / "__main__.py").read_text("utf-8")

    # ---------------------------------------------------------------- tests

    def test_code_dir_follows_the_environment(self):
        self.assertEqual(bootstrap.code_dir(), self.tmp)

    def test_no_marker_leaves_everything_alone(self):
        self.plant(bootstrap.PACKAGE, "ny")

        bootstrap.recover_if_needed(self.tmp)

        self.assertIn("ny", self.live_says())

    def test_a_fresh_marker_leaves_everything_alone(self):
        # Vores egen genstart. Ruller vi tilbage her, kan en selvopdatering
        # aldrig lykkes.
        self.plant(bootstrap.PACKAGE, "ny")
        self.plant(bootstrap.PREVIOUS_NAME, "gammel")
        self.mark(age_seconds=2)

        bootstrap.recover_if_needed(self.tmp)

        self.assertIn("ny", self.live_says())
        self.assertTrue((self.tmp / bootstrap.MARKER_NAME).exists())

    def test_a_stale_marker_restores_the_previous_version(self):
        self.plant(bootstrap.PACKAGE, "ny")
        self.plant(bootstrap.PREVIOUS_NAME, "gammel")
        (self.tmp / bootstrap.REVISION_NAME).write_text("nysha", encoding="utf-8")
        self.mark(age_seconds=bootstrap.GRACE_SECONDS + 60)

        bootstrap.recover_if_needed(self.tmp)

        self.assertIn("gammel", self.live_says())
        self.assertFalse((self.tmp / bootstrap.MARKER_NAME).exists())
        self.assertFalse((self.tmp / bootstrap.REVISION_NAME).exists())

    def test_a_stale_marker_without_a_previous_falls_back_to_the_image(self):
        # Ingen forrige at gå tilbage til: så fjernes den hentede kode helt,
        # og pakken i imaget overtager. Den har vi altid.
        self.plant(bootstrap.PACKAGE, "ny")
        self.mark(age_seconds=bootstrap.GRACE_SECONDS + 60)

        bootstrap.recover_if_needed(self.tmp)

        self.assertFalse((self.tmp / bootstrap.PACKAGE).exists())
        self.assertFalse((self.tmp / bootstrap.MARKER_NAME).exists())

    def test_a_stale_marker_with_nothing_downloaded_is_harmless(self):
        self.mark(age_seconds=bootstrap.GRACE_SECONDS + 60)

        bootstrap.recover_if_needed(self.tmp)

        self.assertFalse((self.tmp / bootstrap.MARKER_NAME).exists())

    def test_marker_age_is_none_without_a_marker(self):
        self.assertIsNone(bootstrap.marker_age(self.tmp))

    def test_marker_age_reads_back_roughly(self):
        self.mark(age_seconds=300)

        self.assertGreater(bootstrap.marker_age(self.tmp), 290)

    def test_grace_matches_the_app(self):
        # De to skal følges ad, ellers kan de træffe modsatte konklusioner om
        # det samme mærke.
        from varmeopt import selfupdate

        self.assertEqual(bootstrap.GRACE_SECONDS, selfupdate.BOOT_GRACE_SECONDS)
        self.assertEqual(bootstrap.MARKER_NAME, selfupdate._BOOT_MARKER)
        self.assertEqual(bootstrap.REVISION_NAME, selfupdate.REVISION_FILE)
        self.assertEqual(bootstrap.PACKAGE, selfupdate.PACKAGE)
        # Navnet på sikkerhedskopien: skallen skal lede efter præcis den mappe
        # download() lagde til side, ellers finder den aldrig noget at rulle
        # tilbage til.
        self.assertEqual(bootstrap.PREVIOUS_NAME, f".{selfupdate.PACKAGE}.forrige")


if __name__ == "__main__":
    unittest.main()
