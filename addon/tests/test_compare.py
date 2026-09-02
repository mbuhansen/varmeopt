import unittest

from varmeopt.compare import Tally, normalise

PELLET = 0.706


def tally():
    return Tally()


class NormaliseTest(unittest.TestCase):
    def test_node_red_writes_in_capitals(self):
        self.assertEqual(normalise("VARMEPUMPE"), "varmepumpe")
        self.assertEqual(normalise("PILLEFYR"), "pillefyr")

    def test_whitespace_and_case_do_not_matter(self):
        self.assertEqual(normalise("  Varmepumpe \n"), "varmepumpe")

    def test_short_forms_are_understood(self):
        self.assertEqual(normalise("VP"), "varmepumpe")
        self.assertEqual(normalise("pille"), "pillefyr")

    def test_anything_else_is_no_answer(self):
        for junk in (None, "", "STANDBY", 42, "ukendt"):
            self.assertIsNone(normalise(junk))


class ObserveTest(unittest.TestCase):
    def test_agreement_is_counted(self):
        t = tally()

        t.observe("varmepumpe", "varmepumpe", 0.3, PELLET, 3.0, 1.0)

        self.assertEqual(t.compared, 1)
        self.assertEqual(t.agreed, 1)
        self.assertEqual(t.disagreed, 0)
        self.assertEqual(t.stake_kr, 0.0)

    def test_a_disagreement_is_counted_by_direction(self):
        t = tally()

        t.observe("varmepumpe", "pillefyr", 0.3, PELLET, 3.0, 1.0)
        t.observe("pillefyr", "varmepumpe", 0.9, PELLET, 3.0, 1.0)

        self.assertEqual(t.disagreed, 2)
        self.assertEqual(t.ours_heatpump, 1)
        self.assertEqual(t.ours_boiler, 1)

    def test_the_stake_is_the_gap_times_the_heat_delivered(self):
        # 3 kW i ét minut er 0,05 kWh. Forskellen er 0,706 - 0,30 = 0,406.
        t = tally()

        t.observe("varmepumpe", "pillefyr", 0.30, PELLET, 3.0, 1.0)

        self.assertAlmostEqual(t.heat_kwh, 0.05, places=9)
        self.assertAlmostEqual(t.stake_kr, 0.406 * 0.05, places=9)

    def test_nothing_is_counted_when_one_side_is_silent(self):
        t = tally()

        t.observe(None, "pillefyr", 0.3, PELLET, 3.0, 1.0)
        t.observe("varmepumpe", None, 0.3, PELLET, 3.0, 1.0)

        self.assertEqual(t.compared, 0)

    def test_no_demand_means_no_stake(self):
        # Uenige, men huset tog ikke imod varme. Saa var der intet paa spil.
        t = tally()

        t.observe("varmepumpe", "pillefyr", 0.30, PELLET, 0.0, 1.0)

        self.assertEqual(t.disagreed, 1)
        self.assertEqual(t.stake_kr, 0.0)

    def test_a_missing_heat_price_still_counts_the_energy(self):
        t = tally()

        t.observe("varmepumpe", "pillefyr", None, PELLET, 3.0, 1.0)

        self.assertAlmostEqual(t.heat_kwh, 0.05, places=9)
        self.assertEqual(t.stake_kr, 0.0)

    def test_the_start_date_is_kept_from_the_first_comparison(self):
        t = tally()

        t.observe("varmepumpe", "varmepumpe", 0.3, PELLET, 3.0, 1.0, today="2026-09-02")
        t.observe("varmepumpe", "varmepumpe", 0.3, PELLET, 3.0, 1.0, today="2026-09-05")

        self.assertEqual(t.since, "2026-09-02")


class ShapeTest(unittest.TestCase):
    def test_agreement_percent(self):
        t = tally()
        for _ in range(3):
            t.observe("varmepumpe", "varmepumpe", 0.3, PELLET, 0.0, 1.0)
        t.observe("varmepumpe", "pillefyr", 0.3, PELLET, 0.0, 1.0)

        self.assertAlmostEqual(t.agreement_percent, 75.0, places=9)

    def test_no_comparisons_gives_no_percentage(self):
        self.assertIsNone(tally().agreement_percent)
        self.assertIn("ingen sammenligninger", tally().summary())

    def test_the_summary_names_both_directions(self):
        t = tally()
        t.observe("varmepumpe", "pillefyr", 0.30, PELLET, 3.0, 1.0)

        line = t.summary()

        self.assertIn("uenige 1", line)
        self.assertIn("paa spil", line)


class StorageTest(unittest.TestCase):
    def test_round_trip(self):
        t = tally()
        t.observe("varmepumpe", "pillefyr", 0.30, PELLET, 3.0, 1.0, today="2026-09-02")

        back = Tally.from_raw(t.to_raw())

        self.assertEqual(back.since, "2026-09-02")
        self.assertEqual(back.disagreed, 1)
        self.assertAlmostEqual(back.stake_kr, t.stake_kr, places=3)

    def test_garbage_gives_an_empty_tally(self):
        for junk in (None, "ikke et regnskab", {"compared": "aeh"}):
            self.assertEqual(Tally.from_raw(junk).compared, 0)


if __name__ == "__main__":
    unittest.main()
