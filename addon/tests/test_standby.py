import unittest

from varmeopt.standby import MIN_HOURS, StandbyTest, Window

HOUR = 3600.0
LITERS = 1000.0
ROOM = 20.0


def cooling(test, hours, start=55.0, watts=200.0, room=ROOM, step=0.5):
    """Koer et vindue hvor lageret taber praecis ``watts``.

    Temperaturen regnes af tiden og ikke ved at traekke fra i hvert skridt,
    saa den sidste aflaesning ligger praecis paa ``hours``.
    """
    steps = int(round(hours / step))
    temp = start
    for i in range(steps + 1):
        t = i * step
        temp = start - watts * t / (LITERS * 1.149)
        test.observe(t * HOUR, temp, room, LITERS, sources={})
    return temp


class ArmingTest(unittest.TestCase):
    def test_nothing_is_measured_until_it_is_armed(self):
        test = StandbyTest()

        note = test.observe(0.0, 55.0, ROOM, LITERS, sources={})

        self.assertEqual(note, "ikke i gang")
        self.assertIsNone(test.started_at)

    def test_arming_starts_the_window_on_the_first_reading(self):
        test = StandbyTest()
        test.arm(0.0)

        self.assertIsNone(test.started_at)
        test.observe(0.0, 55.0, ROOM, LITERS, sources={})

        self.assertEqual(test.started_at, 0.0)

    def test_a_short_window_is_not_a_measurement(self):
        test = StandbyTest()
        test.arm(0.0)
        cooling(test, MIN_HOURS - 0.5)

        note = test.disarm(MIN_HOURS * HOUR)

        self.assertEqual(test.windows, [])
        self.assertIn("for kort", note)


class MeasurementTest(unittest.TestCase):
    def test_a_night_gives_the_loss_and_the_coefficient(self):
        # 1000 L, 200 W tab, 8 timer: faldet er 1,39 K.
        test = StandbyTest()
        test.arm(0.0)
        end = cooling(test, 8.0, start=55.0, watts=200.0)
        test.disarm(8 * HOUR)

        self.assertAlmostEqual(55.0 - end, 1.39, places=2)
        self.assertEqual(len(test.windows), 1)
        window = test.windows[0]
        self.assertAlmostEqual(window.loss_kw, 0.200, places=3)
        self.assertAlmostEqual(window.hours, 8.0, places=6)
        # Middel-delta ligger lidt under de 35 ved start, fordi tanken koeler.
        self.assertLess(window.delta_k, 35.0)
        self.assertGreater(window.delta_k, 34.0)
        # 200 W over ~34,3 K.
        self.assertAlmostEqual(test.ua_w_per_k, 200 / window.delta_k, places=6)

    def test_the_coefficient_scales_the_loss_to_another_temperature(self):
        # Det er hele pointen i at maale W/K og ikke bare kW: tabet ved 55
        # grader siger ogsaa hvad det er ved 40.
        test = StandbyTest()
        test.arm(0.0)
        cooling(test, 8.0, start=55.0, watts=200.0)
        test.disarm(8 * HOUR)

        warm = test.loss_kw_at(55.0, ROOM)
        cool = test.loss_kw_at(40.0, ROOM)

        self.assertGreater(warm, cool)
        self.assertAlmostEqual(cool / warm, 20.0 / 35.0, places=2)

    def test_two_nights_are_weighted_by_their_length(self):
        test = StandbyTest()
        for hours, watts in ((8.0, 200.0), (2.0, 400.0)):
            test.arm(0.0)
            cooling(test, hours, start=55.0, watts=watts)
            test.disarm(hours * HOUR)

        self.assertEqual(len(test.windows), 2)
        # Den lange nat vejer fire gange saa meget som den korte.
        long_ua, short_ua = (w.ua_w_per_k for w in test.windows)
        expected = (long_ua * 8 + short_ua * 2) / 10
        self.assertAlmostEqual(test.ua_w_per_k, expected, places=6)


class ContaminationTest(unittest.TestCase):
    def test_a_running_source_restarts_the_window(self):
        # Et tab paa 200 W kan ikke skilles fra en tilfoersel paa 8 kW.
        test = StandbyTest()
        test.arm(0.0)
        cooling(test, 4.0)

        note = test.observe(4 * HOUR, 53.0, ROOM, LITERS, sources={"varmepumpe": 8.0})

        self.assertIsNone(test.started_at)
        self.assertIn("varmepumpe", note)

    def test_a_trickle_below_the_noise_floor_does_not_count_as_running(self):
        test = StandbyTest()
        test.arm(0.0)
        test.observe(0.0, 55.0, ROOM, LITERS, sources={"solvarme": 0.01})

        self.assertEqual(test.started_at, 0.0)

    def test_a_tank_that_gets_warmer_is_not_a_loss(self):
        # Saa gik der noget ind vi ikke saa.
        test = StandbyTest()
        test.arm(0.0)
        for i in range(20):
            test.observe(i * HOUR / 2, 50.0 + i * 0.1, ROOM, LITERS, sources={})

        test.disarm(10 * HOUR)

        self.assertEqual(test.windows, [])

    def test_a_lukewarm_store_has_nothing_to_measure(self):
        test = StandbyTest()
        test.arm(0.0)

        note = test.observe(0.0, 22.0, ROOM, LITERS, sources={})

        self.assertIsNone(test.started_at)
        self.assertIn("over rummet", note)

    def test_a_missing_room_temperature_stops_it(self):
        test = StandbyTest()
        test.arm(0.0)

        note = test.observe(0.0, 55.0, None, LITERS, sources={})

        self.assertIsNone(test.started_at)
        self.assertIn("rumtemperatur", note)


class PersistenceTest(unittest.TestCase):
    def test_finished_windows_survive_a_restart(self):
        test = StandbyTest()
        test.arm(0.0)
        cooling(test, 8.0)
        test.disarm(8 * HOUR)

        again = StandbyTest.from_raw(test.to_raw())

        self.assertEqual(len(again.windows), 1)
        self.assertAlmostEqual(again.ua_w_per_k, test.ua_w_per_k, places=3)

    def test_a_window_in_progress_does_not(self):
        # En genstart betyder minutter uden aflaesninger. Saa er det aerligere
        # at begynde forfra end at regne hen over hullet.
        test = StandbyTest()
        test.arm(0.0)
        cooling(test, 4.0)

        again = StandbyTest.from_raw(test.to_raw())

        self.assertFalse(again.armed)
        self.assertIsNone(again.started_at)

    def test_rubbish_on_disk_is_ignored(self):
        for raw in (None, "nej", {"windows": "nej"}, {"windows": [{"date": "x"}]}):
            with self.subTest(raw=raw):
                self.assertEqual(StandbyTest.from_raw(raw).windows, [])

    def test_a_window_with_no_temperature_difference_is_rejected(self):
        self.assertIsNone(
            Window.from_raw({"date": "d", "hours": 8, "delta_k": 0, "loss_kw": 0.2})
        )


if __name__ == "__main__":
    unittest.main()
