import unittest
from datetime import datetime, timedelta, timezone

from varmeopt.usage import KEEP_DAYS, MINUTES_PER_DAY, Usage

# Fast tidszone, så testen ikke afhænger af hvor maskinen står. Timestampene
# regnes om til lokal tid i modulet; her bygges de af en kendt lokal tid.
TZ = timezone(timedelta(hours=2))


def at(day, hour=0, minute=0):
    return datetime(2026, 9, day, hour, minute, tzinfo=TZ).timestamp()


def run(usage, start, minutes, house=1.0, vvb=None, spa=None, step=60):
    """Kør ``minutes`` minutter igennem i skridt på ``step`` sekunder."""
    now = start
    usage.observe(now, house, vvb, spa)
    for _ in range(int(minutes * 60 / step)):
        now += step
        usage.observe(now, house, vvb, spa)
    return now


class CountingTest(unittest.TestCase):
    def test_an_hour_at_one_kilowatt_is_one_kilowatt_hour(self):
        u = Usage()

        run(u, at(16, 8), 60, house=1.0)

        self.assertAlmostEqual(u.today.varme, 1.0, places=3)

    def test_the_three_are_counted_apart(self):
        u = Usage()

        run(u, at(16, 8), 60, house=2.0, vvb=4.0, spa=3.0)

        self.assertAlmostEqual(u.today.varme, 2.0, places=3)
        self.assertAlmostEqual(u.today.vvb, 4.0, places=3)
        self.assertAlmostEqual(u.today.spa, 3.0, places=3)
        self.assertAlmostEqual(u.today.total, 9.0, places=3)

    def test_a_vessel_that_is_not_running_adds_nothing(self):
        # ``None`` er "kører ikke", og det må ikke blive til nul kW der
        # alligevel tæller et skridt med.
        u = Usage()

        run(u, at(16, 8), 60, house=1.0, vvb=None, spa=None)

        self.assertEqual(u.today.vvb, 0.0)
        self.assertEqual(u.today.spa, 0.0)

    def test_an_unmeasurable_house_load_stands_still(self):
        # Kan huset ikke måles, står varmetallet stille. Her stod der før et
        # fald tilbage på kurven; det hører til i HouseLoad, ikke her.
        u = Usage()

        run(u, at(16, 8), 60, house=None, spa=2.0)

        self.assertEqual(u.today.varme, 0.0)
        self.assertAlmostEqual(u.today.spa, 2.0, places=3)

    def test_a_gap_is_not_counted(self):
        # Add-on'en stod stille i en time. Vi ved ikke hvad der skete, og en
        # times 3 kW må ikke lægge sig i dagens tal hvor ingen ser det igen.
        u = Usage()
        u.observe(at(16, 8), 3.0, None, None)
        u.observe(at(16, 9), 3.0, None, None)

        self.assertEqual(u.today.varme, 0.0)

    def test_time_running_backwards_is_not_counted(self):
        u = Usage()
        u.observe(at(16, 8), 3.0, None, None)
        u.observe(at(16, 7), 3.0, None, None)

        self.assertEqual(u.today.varme, 0.0)


class MidnightTest(unittest.TestCase):
    def test_a_new_day_starts_from_zero(self):
        u = Usage()
        run(u, at(16, 23, 30), 60, house=2.0)

        self.assertEqual(u.today.date, "2026-09-17")
        self.assertLess(u.today.varme, 1.1, "kun halvdelen hører til i morgen")
        self.assertAlmostEqual(u.days[-2].varme, 1.0, places=1)

    def test_the_closing_day_gets_a_point_at_midnight(self):
        # Uden det holder kurven op 23:45, og savtakken når aldrig sin top.
        u = Usage()
        run(u, at(16, 23, 30), 60, house=2.0)
        i_går = u.days[-2]

        self.assertEqual(i_går.samples[-1][0], MINUTES_PER_DAY)
        self.assertAlmostEqual(i_går.samples[-1][1], i_går.varme, places=6)

    def test_only_the_last_four_days_are_kept(self):
        u = Usage()
        for day in range(12, 19):
            run(u, at(day, 8), 30, house=2.0)

        self.assertEqual(len(u.days), KEEP_DAYS)
        self.assertEqual(u.days[-1].date, "2026-09-18")
        self.assertEqual(u.days[0].date, "2026-09-15")

    def test_yesterday_is_only_yesterday_when_it_really_is(self):
        # Stod add-on'en stille et døgn, er den næstsidste dag ikke i går.
        # Et tal med en forkert dato bliver sammenlignet alligevel.
        u = Usage()
        run(u, at(14, 8), 30, house=2.0)
        run(u, at(16, 8), 30, house=2.0)

        self.assertIsNone(u.yesterday)

        run(u, at(17, 8), 30, house=2.0)

        self.assertEqual(u.yesterday.date, "2026-09-16")


class CurveTest(unittest.TestCase):
    def test_the_samples_climb_through_the_day(self):
        u = Usage()
        run(u, at(16, 8), 120, house=3.0)
        minutes = [point[0] for point in u.today.samples]
        varme = [point[1] for point in u.today.samples]

        self.assertEqual(minutes[0], 8 * 60)
        self.assertEqual(minutes[-1], 10 * 60)
        self.assertEqual(varme, sorted(varme), "kurven må ikke gå nedad")
        self.assertAlmostEqual(varme[-1], 6.0, places=1)

    def test_a_quarter_is_one_point(self):
        u = Usage()
        run(u, at(16, 8), 60, house=1.0)

        self.assertEqual([p[0] for p in u.today.samples], [480, 495, 510, 525, 540])


class RoundTripTest(unittest.TestCase):
    def test_a_saved_day_comes_back(self):
        u = Usage()
        run(u, at(16, 8), 60, house=2.0, vvb=1.0)

        back = Usage.from_raw(u.to_raw())

        self.assertEqual(back.today.date, u.today.date)
        self.assertAlmostEqual(back.today.varme, u.today.varme, places=3)
        self.assertAlmostEqual(back.today.vvb, u.today.vvb, places=3)
        # Punkterne rundes til tre decimaler på vej ned på disken. Det er
        # 1 Wh, og det er kurvens opløsning - ikke et tab værd at gemme på.
        self.assertEqual(
            [p[0] for p in back.today.samples], [p[0] for p in u.today.samples]
        )
        for før, efter in zip(u.today.samples, back.today.samples):
            self.assertAlmostEqual(efter[1], før[1], places=3)

    def test_counting_continues_after_a_restart(self):
        # Genstart er ikke et hul: dagens tal skal tælle videre, ikke forfra.
        u = Usage()
        now = run(u, at(16, 8), 60, house=2.0)

        back = Usage.from_raw(u.to_raw())
        run(back, now + 60, 60, house=2.0)

        self.assertAlmostEqual(back.today.varme, 4.0, places=1)

    def test_rubbish_on_disk_does_not_crash(self):
        for raw in (None, {}, [], {"days": "nej"}, {"days": [1, 2]}):
            with self.subTest(raw=raw):
                self.assertEqual(Usage.from_raw(raw).days, [])

    def test_a_day_with_a_broken_sample_keeps_its_totals(self):
        raw = {
            "days": [
                {"date": "2026-09-16", "varme": 4.0, "vvb": 1.0, "spa": 0.5,
                 "samples": [[0, 0, 0, 0], ["nej"], [60, 1.0, 0.0, 0.0]]}
            ]
        }

        day = Usage.from_raw(raw).today

        self.assertAlmostEqual(day.varme, 4.0, places=6)
        self.assertEqual([p[0] for p in day.samples], [0, 60])
