"""Varmepumpens målte ladehastighed.

Typeskiltet siger 16 kW. Maskinen bestemmer selv og lander omkring 12, højst
14 — og raten står fire steder i planlægningen, hvor den alle fire trækker
samme vej: sættes den for højt, tror planlæggeren at den har bedre tid end den
har, og starter for sent.
"""

import unittest

from varmeopt.capacity import ChargeRate


class MeasureTest(unittest.TestCase):
    def test_part_load_space_heating_does_not_count(self):
        # Pumpen kører 3 kW rumvarme i lange stræk. Det siger intet om hvor
        # hurtigt tankene kan fyldes, og et gennemsnit over alt ville trække
        # raten langt under det den kan.
        rate = ChargeRate(nameplate_kw=16.0)

        for _ in range(200):
            rate.observe(3.0)

        self.assertIsNone(rate.kw)
        self.assertEqual(rate.effective_kw, 16.0)

    def test_the_hard_minutes_are_what_is_learned(self):
        rate = ChargeRate(nameplate_kw=16.0)

        for _ in range(200):
            rate.observe(12.0)

        self.assertAlmostEqual(rate.effective_kw, 12.0, delta=0.2)

    def test_a_mixed_day_still_finds_the_charging_rate(self):
        # Rumvarme det meste af døgnet, en opladning ind imellem.
        rate = ChargeRate(nameplate_kw=16.0)

        for hour in range(24):
            for _ in range(60):
                rate.observe(12.5 if 13 <= hour < 15 else 2.5)

        self.assertAlmostEqual(rate.effective_kw, 12.5, delta=0.3)

    def test_the_peak_is_kept_as_well(self):
        rate = ChargeRate(nameplate_kw=16.0)
        for kw in (12.0, 14.0, 11.5, 12.5):
            rate.observe(kw)

        self.assertAlmostEqual(rate.peak_kw, 14.0, places=6)
        self.assertIn("højst 14.0", rate.note)

    def test_before_anything_is_measured_the_nameplate_stands(self):
        rate = ChargeRate(nameplate_kw=16.0)

        self.assertEqual(rate.effective_kw, 16.0)
        self.assertIn("ikke målt endnu", rate.note)

    def test_rubbish_is_ignored(self):
        rate = ChargeRate(nameplate_kw=16.0)
        for junk in (None, float("nan"), "12", -4.0):
            rate.observe(junk)

        self.assertIsNone(rate.kw)


class StorageTest(unittest.TestCase):
    def test_it_survives_a_restart(self):
        rate = ChargeRate(nameplate_kw=16.0)
        for _ in range(100):
            rate.observe(12.0)

        back = ChargeRate.from_raw(rate.to_raw(), 16.0)

        self.assertAlmostEqual(back.effective_kw, rate.effective_kw, places=6)
        self.assertEqual(back.count, rate.count)

    def test_garbage_falls_back_to_the_nameplate(self):
        for junk in (None, "aeh", {"kw": "nej"}, {"kw": 0}):
            self.assertEqual(ChargeRate.from_raw(junk, 16.0).effective_kw, 16.0)


if __name__ == "__main__":
    unittest.main()
