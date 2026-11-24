"""Logbogen: én linje pr. halvtime med det planen så og det der skete."""

import json
import tempfile
import unittest
from pathlib import Path

from varmeopt.logbook import Logbook, Reading, compact_plan, read_rows

SLOT = 1_764_000_000.0 - 1_764_000_000.0 % 1800  # en halvtimes begyndelse


def reading(**kw):
    base = dict(vp_el=2.0, vp_varme=9.0, behov=3.0, lager_kwh=20.0, ude=5.0,
                kompressor=True, lad_op=False, kilde="varmepumpe")
    base.update(kw)
    return Reading(**base)


class LogbookTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "logbook.jsonl"
        self.seen_calls = 0

    def tearDown(self):
        self.dir.cleanup()

    def seen(self):
        self.seen_calls += 1
        return {"kilde": "varmepumpe", "plan": {"x": 1}, "frem": {"behov_kw": [1.0]}}

    def walk(self, book, minutes, start=0, **kw):
        rows = []
        for m in minutes:
            row = book.observe(SLOT + (start + m) * 60, reading(**kw), self.seen)
            if row is not None:
                rows.append(row)
        return rows

    def test_full_slot_integrates_energy(self):
        book = Logbook(self.path)
        rows = self.walk(book, range(0, 31))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["t"], int(SLOT))
        self.assertEqual(row["dækning"], 1.0)
        self.assertAlmostEqual(row["skete"]["vp_el_kwh"], 1.0, places=3)
        self.assertAlmostEqual(row["skete"]["vp_varme_kwh"], 4.5, places=3)
        self.assertAlmostEqual(row["skete"]["behov_kwh"], 1.5, places=3)
        self.assertEqual(row["skete"]["kompressor_min"], 30.0)
        self.assertEqual(row["skete"]["kilde_min"], {"varmepumpe": 30.0})
        self.assertEqual(row["så"]["kilde"], "varmepumpe")
        # Planen læses én gang pr. halvtime, ikke hver cyklus.
        self.assertEqual(self.seen_calls, 2)
        self.assertEqual(read_rows(self.path), [row])

    def test_uneven_cycles_split_at_the_boundary(self):
        # Cyklus kl. 29,5 og 30,5: halvdelen af springet hører til hver halvtime.
        book = Logbook(self.path)
        rows = []
        for m in [*range(0, 30), 29.5, 30.5, 60]:
            row = book.observe(SLOT + m * 60, reading(), self.seen)
            if row is not None:
                rows.append(row)
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]["skete"]["vp_el_kwh"], 1.0, places=3)
        self.assertEqual(rows[0]["dækning"], 1.0)
        # Anden halvtime: 30,5 → 60 er et spring på 29,5 min, over grænsen på 5.
        self.assertEqual(rows[1]["dækning"], 0.02)

    def test_gap_is_not_integrated(self):
        book = Logbook(self.path)
        rows = self.walk(book, [0, 1, 2, 3, 4, 5, 25, 26, 27, 28, 29, 30])
        row = rows[0]
        # 0-5 og 25-30: ti minutter set; springet på tyve tælles ikke.
        self.assertAlmostEqual(row["dækning"], 10 / 30, places=2)
        # Energien skaleres op til halvtimen ud fra det sete - men dækningen
        # siger at linjen ikke skal bruges til at regne.
        self.assertAlmostEqual(row["skete"]["vp_el_kwh"], 10 / 60 * 2, places=3)

    def test_sensor_missing_most_of_slot_gives_none(self):
        book = Logbook(self.path)
        rows = []
        for m in range(0, 31):
            r = reading(solvarme=1.0 if m < 5 else None)
            row = book.observe(SLOT + m * 60, r, self.seen)
            if row is not None:
                rows.append(row)
        self.assertIsNone(rows[0]["skete"]["solvarme_kwh"])
        self.assertIsNotNone(rows[0]["skete"]["vp_el_kwh"])

    def test_broken_line_is_skipped(self):
        book = Logbook(self.path)
        self.walk(book, range(0, 31))
        with self.path.open("a", encoding="utf-8") as f:
            f.write('{"t": 1, "halv')
        self.walk(book, range(31, 62))
        rows = read_rows(self.path)
        self.assertEqual(len(rows), 2)
        # Og en ny logbog læser de hele linjer ind igen efter en genstart.
        self.assertEqual(len(Logbook(self.path).recent), 2)

    def test_prune_drops_old_rows(self):
        book = Logbook(self.path, keep_days=1)
        old = {"t": SLOT - 3 * 86400, "dækning": 1.0, "så": {}, "skete": {}}
        self.path.write_text(json.dumps(old) + "\n", encoding="utf-8")
        self.walk(book, range(0, 31))
        self.assertEqual([r["t"] for r in read_rows(self.path)], [int(SLOT)])

    def test_light_strips_plan_and_forecast(self):
        book = Logbook(self.path)
        self.walk(book, range(0, 31))
        light = book.light()
        self.assertNotIn("plan", light[0]["så"])
        self.assertNotIn("frem", light[0]["så"])
        self.assertIn("plan", book.recent[0]["så"])

    def test_seen_failure_does_not_break(self):
        book = Logbook(self.path)

        def broken():
            raise RuntimeError("planen er væk")

        with self.assertLogs("varmeopt.logbook", level="ERROR"):
            book.observe(SLOT, reading(), broken)
        row = book.observe(SLOT + 1800, reading(), broken)
        self.assertEqual(row["så"], {})

    def test_no_path_writes_nothing(self):
        book = Logbook(None)
        rows = self.walk(book, range(0, 31))
        self.assertEqual(len(rows), 1)
        self.assertFalse(self.path.exists())

    def test_compact_plan(self):
        raw = {"round_trip": 0.9, "slots": [
            {"minutes": 0, "state": "Hold", "import": 1.23456, "export": 0.5},
            {"minutes": 30, "state": "Exp", "import": 2.0, "export": 1.0},
            {"minutes": 60, "state": "Exp", "import": 2.0, "export": 1.0},
        ]}
        out = compact_plan(raw, 2)
        self.assertEqual(out["round_trip"], 0.9)
        self.assertEqual(len(out["rækker"]), 2)
        self.assertEqual(out["rækker"][0][:4], [0, "Hold", 1.2346, 0.5])
        self.assertIsNone(compact_plan(None, 2))


if __name__ == "__main__":
    unittest.main()
