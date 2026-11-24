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
            "skete": {"behov_kwh": actual_kw / 2},
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
        self.assertEqual(f.factor, f.learned)

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

    def test_round_trip(self):
        f = DemandFactor()
        f.nightly(rows(MIN_DAYS + 2), "n1")
        back = DemandFactor.from_raw(f.to_raw())
        self.assertEqual(back.factor, f.factor)
        self.assertEqual(DemandFactor.from_raw({"learned": 9}).learned, 1.3)


if __name__ == "__main__":
    unittest.main()
