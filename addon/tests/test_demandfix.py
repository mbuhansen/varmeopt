"""Behovsudsigten rettet efter logbogen - men først når der er data nok."""

import unittest

from varmeopt.demandfix import MIN_DAYS, DemandFactor

T0 = 1_764_000_000 - 1_764_000_000 % 1800


def rows(days, forecast_kw=2.0, actual_kw=2.3, used=1.0, cover=1.0):
    out = []
    for i in range(days * 48):
        out.append({
            "t": T0 + i * 1800,
            "dækning": cover,
            "så": {"frem": {"behov_kw": [forecast_kw * used] * 48}, "behov_faktor": used},
            "skete": {"hus_kwh": actual_kw / 2},
        })
    return out


class DemandFactorTest(unittest.TestCase):
    def test_nothing_happens_before_enough_days(self):
        f = DemandFactor()
        f.nightly(rows(5), "n1")
        self.assertFalse(f.active)
        self.assertEqual(f.factor, 1.0, "udsigten bruges urørt indtil da")
        self.assertGreater(f.learned, 1.0, "men den læres imens")

    def test_it_moves_a_fifth_of_the_way_and_then_applies(self):
        f = DemandFactor()
        f.nightly(rows(MIN_DAYS + 2), "n1")
        self.assertTrue(f.active)
        self.assertAlmostEqual(f.learned, 1.0 + 0.2 * 0.15, places=3)
        # Den brugte glider en femtedel mod den lærte - ikke hele vejen.
        self.assertAlmostEqual(f.factor, 1.0 + 0.2 * (f.learned - 1.0), places=3)

    def test_no_jump_when_it_becomes_active(self):
        # Lært i stilhed i 13 dage til 1,3 - den nat den bliver aktiv, må
        # udsigten ikke springe 30 %.
        f = DemandFactor()
        f.learned = 1.3
        f.nightly(rows(MIN_DAYS + 2, actual_kw=2.6), "n1")
        self.assertTrue(f.active)
        self.assertLess(f.factor, 1.07)
        for n in range(15):
            f.nightly(rows(MIN_DAYS + 2, actual_kw=2.6), f"m{n}")
        self.assertGreater(f.factor, 1.25, "men den når derop over nætterne")

    def test_it_glides_back_when_data_runs_out(self):
        f = DemandFactor()
        f.learned, f.applied = 1.2, 1.2
        f.nightly(rows(3), "n1")
        self.assertFalse(f.active)
        self.assertGreater(f.factor, 1.1, "ikke tilbage til 1 på én nat")
        self.assertLess(f.factor, 1.2)

    def test_it_is_not_learned_on_top_of_itself(self):
        # Udsigten i logbogen er allerede ganget med 1,15, og den rammer
        # derfor rigtigt. Faktoren skal blive på 1,15, ikke vokse videre.
        f = DemandFactor()
        f.learned = 1.15
        f.nightly(rows(MIN_DAYS + 2, used=1.15), "n1")
        self.assertAlmostEqual(f.learned, 1.15, places=3)

    def test_it_stays_within_its_bounds(self):
        f = DemandFactor()
        data = rows(MIN_DAYS + 2, actual_kw=10.0)
        for n in range(25):
            f.nightly(data, f"n{n}")
        self.assertLessEqual(f.learned, 1.3)
        self.assertGreater(f.learned, 1.29)

    def test_once_a_night(self):
        f = DemandFactor()
        self.assertTrue(f.nightly(rows(3), "n1"))
        self.assertFalse(f.nightly(rows(3), "n1"))

    def test_partial_slots_are_not_compared(self):
        f = DemandFactor()
        f.nightly(rows(MIN_DAYS + 2, cover=0.5), "n1")
        self.assertEqual(f.days, 0)
        self.assertEqual(f.learned, 1.0)

    def test_days_count_measured_slots_not_pairs(self):
        # To målte halvtimer i døgnet giver 46 par, men det er ikke en dag.
        data = rows(MIN_DAYS + 2)
        for row in data:
            # Begge felter, så testen rammer optællingen og ikke feltnavnet.
            row["skete"]["behov_kwh"] = row["skete"]["hus_kwh"]
            if (row["t"] // 1800) % 48 not in (0, 1):
                row["skete"]["hus_kwh"] = row["skete"]["behov_kwh"] = None
        f = DemandFactor()
        f.nightly(data, "n1")
        self.assertEqual(f.days, 0)
        self.assertFalse(f.active)

    def test_the_flowmeter_alone_is_not_used(self):
        data = rows(MIN_DAYS + 2)
        for row in data:
            row["skete"] = {"behov_kwh": 9.0}
        f = DemandFactor()
        f.nightly(data, "n1")
        self.assertEqual(f.learned, 1.0)

    def test_round_trip(self):
        f = DemandFactor()
        f.nightly(rows(MIN_DAYS + 2), "n1")
        back = DemandFactor.from_raw(f.to_raw())
        self.assertEqual(back.factor, f.factor)
        self.assertEqual(DemandFactor.from_raw({"learned": 9}).learned, 1.3)


if __name__ == "__main__":
    unittest.main()
