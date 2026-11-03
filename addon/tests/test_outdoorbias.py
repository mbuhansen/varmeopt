import unittest

from varmeopt.outdoorbias import FADE_MINUTES, OutdoorBias

MINUTE = 60.0


def learned(offset, minutes, start=0.0):
    """En korrektion der har set BT1 ligge ``offset`` fra udsigten hvert minut."""
    bias = OutdoorBias()
    for i in range(minutes):
        bias.observe(start + i * MINUTE, 10.0 + offset, 10.0)
    return bias


class CorrectionTest(unittest.TestCase):
    def test_the_deviation_now_fades_into_the_fixed_one(self):
        # 3. november kl. 12: fast -0,6 fra dagene før, og så 8,3 målt mod
        # 9,9 i udsigten.
        bias = learned(-0.6, 3 * 24 * 60)
        bias.observe(3 * 24 * 60 * MINUTE, 8.3, 9.9)

        self.assertAlmostEqual(bias.correction(0), -1.6, places=6)
        self.assertAlmostEqual(bias.correction(FADE_MINUTES / 2), -1.1, delta=0.01)
        self.assertAlmostEqual(bias.correction(FADE_MINUTES), bias.fixed, places=6)
        self.assertAlmostEqual(bias.correction(48 * 60), bias.fixed, places=6)
        self.assertAlmostEqual(bias.fixed, -0.6, delta=0.01)

    def test_the_fixed_one_learns_slowly(self):
        # Et par timer med en stor afvigelse må ikke flytte den faste meget,
        # når den har lært på flere døgn.
        bias = learned(-0.6, 3 * 24 * 60)
        for i in range(120):
            bias.observe((3 * 24 * 60 + i) * MINUTE, 7.0, 10.0)

        self.assertGreater(bias.fixed, -0.7)

    def test_the_first_reading_does_not_become_the_fixed_one_for_days(self):
        # Fra en tom fil er de første døgn et almindeligt gennemsnit.
        bias = OutdoorBias()
        bias.observe(0.0, 8.3, 9.9)
        for i in range(1, 600):
            bias.observe(i * MINUTE, 9.4, 10.0)

        self.assertAlmostEqual(bias.fixed, -0.6, delta=0.01)

    def test_without_a_reading_only_the_fixed_one_applies(self):
        # Først en afvigelse her og nu der er langt fra den faste - ellers
        # kan testen ikke se om den gamle bliver hængende.
        bias = learned(-0.6, 600)
        bias.observe(600 * MINUTE, 8.3, 9.9)
        bias.observe(601 * MINUTE, None, 9.9)

        self.assertAlmostEqual(bias.correction(0), bias.fixed, places=9)

        bias.observe(602 * MINUTE, 8.3, 9.9)
        bias.observe(603 * MINUTE, 8.3, None)
        self.assertAlmostEqual(bias.correction(0), bias.fixed, places=9)

    def test_nothing_learned_means_no_correction(self):
        self.assertEqual(OutdoorBias().correction(0), 0.0)

    def test_a_long_gap_counts_as_an_hour(self):
        # Et døgns pause må ikke lade den første måling bagefter flytte den
        # faste afvigelse en tredjedel.
        bias = learned(-0.6, 10 * 24 * 60)
        bias.observe(20 * 24 * 60 * MINUTE, 7.0, 10.0)

        self.assertGreater(bias.fixed, -0.7)


class CheckTest(unittest.TestCase):
    def test_the_check_scores_both_forecasts_against_bt1(self):
        bias = learned(-0.6, 600)
        wall = 600 * MINUTE
        bias.observe(wall, 9.4, 10.0)
        bias.note_forecast(wall, lambda minutes: 10.0)

        # En time senere viser BT1 9,4: udsigten ramte 0,6 ved siden af,
        # den korrigerede ramte.
        bias.observe(wall + 60 * MINUTE, 9.4, 10.0)

        raw, corrected, n = bias.errors[60]
        self.assertEqual(n, 1)
        self.assertAlmostEqual(raw, 0.6, places=6)
        self.assertAlmostEqual(corrected, 0.0, places=6)
        self.assertNotIn(180, bias.errors)

    def test_it_notes_at_most_every_half_hour(self):
        bias = learned(-0.6, 10)
        for i in range(29):
            bias.note_forecast(i * MINUTE, lambda minutes: 10.0)

        self.assertEqual(len(bias._pending), 3)


class StoreTest(unittest.TestCase):
    def test_the_fixed_one_and_the_check_survive_a_restart(self):
        bias = learned(-0.6, 600)
        bias.errors[60] = (0.6, 0.2, 12.0)

        again = OutdoorBias.from_raw(bias.to_raw())

        self.assertAlmostEqual(again.fixed, bias.fixed, places=3)
        self.assertEqual(again.count, bias.count)
        self.assertEqual(again.errors[60], (0.6, 0.2, 12.0))
        # Afvigelsen her og nu er en måling, ikke et minde.
        self.assertIsNone(again.now)

    def test_a_broken_file_starts_empty(self):
        self.assertIsNone(OutdoorBias.from_raw({"fixed": "x"}).fixed)
        self.assertIsNone(OutdoorBias.from_raw(None).fixed)
        self.assertEqual(OutdoorBias.from_raw({"errors": {"60": "x"}}).errors, {})


if __name__ == "__main__":
    unittest.main()
