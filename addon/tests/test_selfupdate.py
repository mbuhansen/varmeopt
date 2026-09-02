import io
import os
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

from varmeopt import selfupdate

PREFIX = "varmeopt-abc123/addon/varmeopt/"


def archive(*names: str) -> tarfile.TarFile:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in names:
            data = b"x = 1\n"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return tarfile.open(fileobj=buf, mode="r:gz")


class CodeDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="varmeopt-selfupdate-"))
        os.environ["VARMEOPT_CODE_DIR"] = str(self.tmp)

    def tearDown(self):
        os.environ.pop("VARMEOPT_CODE_DIR", None)


class MemberFilterTest(CodeDirTest):
    def test_keeps_only_python_inside_the_package(self):
        tar = archive(
            PREFIX + "cop.py",
            PREFIX + "under/mere.py",
            PREFIX + "laesmig.txt",
            "varmeopt-abc123/README.md",
        )
        names = [m.name for m in selfupdate._members(tar, PREFIX)]

        self.assertEqual(names, [PREFIX + "cop.py", PREFIX + "under/mere.py"])

    def test_rejects_paths_that_escape_the_package(self):
        # Arkivet kommer fra vores eget repo, men det er stadig fremmed input,
        # og en sti med .. ville skrive uden for /data/code.
        tar = archive(PREFIX + "../../../etc/ondt.py", PREFIX + "fin.py")
        names = [m.name for m in selfupdate._members(tar, PREFIX)]

        self.assertEqual(names, [PREFIX + "fin.py"])


class CompileGateTest(CodeDirTest):
    def test_accepts_code_that_parses(self):
        (self.tmp / "godt.py").write_text("def f():\n    return 1\n", encoding="utf-8")

        self.assertTrue(selfupdate._compiles(self.tmp))

    def test_rejects_code_with_a_syntax_error(self):
        # Porten der forhindrer at en halv commit bliver den kode der starter.
        (self.tmp / "daarligt.py").write_text("def f(\n", encoding="utf-8")

        self.assertFalse(selfupdate._compiles(self.tmp))


class BootMarkerTest(CodeDirTest):
    def _age_marker(self, seconds: float) -> None:
        path = self.tmp / selfupdate._BOOT_MARKER
        stamp = time.time() - seconds
        os.utime(path, (stamp, stamp))

    def test_a_marker_we_just_set_is_not_a_failure(self):
        # Kernen i fejlen: maerket saettes lige foer genstarten, saa den nye
        # proces finder sit eget maerke et sekund senere. Uden henstand ville
        # den rulle den kode tilbage som den lige selv hentede.
        selfupdate.mark_boot()

        self.assertFalse(selfupdate.boot_failed())

    def test_a_marker_that_has_stood_too_long_is_a_failure(self):
        selfupdate.mark_boot()
        self._age_marker(selfupdate.BOOT_GRACE_SECONDS + 60)

        self.assertTrue(selfupdate.boot_failed())

    def test_no_marker_is_never_a_failure(self):
        self.assertFalse(selfupdate.boot_failed())
        self.assertIsNone(selfupdate.boot_marker_age())

    def test_marker_is_gone_after_clearing(self):
        selfupdate.mark_boot()
        selfupdate.clear_boot()

        self.assertIsNone(selfupdate.boot_marker_age())

    def test_clearing_a_marker_that_is_not_there_is_harmless(self):
        selfupdate.clear_boot()  # må ikke rejse


class RevisionTest(CodeDirTest):
    def test_no_revision_file_means_the_built_in_code(self):
        self.assertIsNone(selfupdate.current())

    def test_revision_is_read_back(self):
        (self.tmp / selfupdate.REVISION_FILE).write_text("abc123def456\n", encoding="utf-8")

        self.assertEqual(selfupdate.current(), "abc123def456")

    def test_empty_revision_file_counts_as_none(self):
        (self.tmp / selfupdate.REVISION_FILE).write_text("  \n", encoding="utf-8")

        self.assertIsNone(selfupdate.current())


class RollbackTest(CodeDirTest):
    def _plant(self, folder: str, marker: str) -> None:
        path = self.tmp / folder
        path.mkdir(parents=True, exist_ok=True)
        (path / "__main__.py").write_text(f"# {marker}\n", encoding="utf-8")

    def test_rollback_restores_the_previous_download(self):
        self._plant("varmeopt", "ny")
        self._plant(".varmeopt.forrige", "gammel")
        (self.tmp / selfupdate.REVISION_FILE).write_text("nysha", encoding="utf-8")

        self.assertTrue(selfupdate.rollback())
        self.assertIn("gammel", (self.tmp / "varmeopt" / "__main__.py").read_text("utf-8"))
        # Uden revisionsfil falder vi tilbage til "den indbyggede", hvilket er
        # sandt nok: vi ved ikke længere hvilken commit der ligger der.
        self.assertIsNone(selfupdate.current())

    def test_rollback_without_a_previous_version_does_nothing(self):
        self._plant("varmeopt", "ny")

        self.assertFalse(selfupdate.rollback())
        self.assertIn("ny", (self.tmp / "varmeopt" / "__main__.py").read_text("utf-8"))


if __name__ == "__main__":
    unittest.main()
