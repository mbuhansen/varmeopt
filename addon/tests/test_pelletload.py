import unittest
from datetime import datetime, timedelta, timezone

from varmeopt.pelletload import MAX_KG_STEP, PelletLoad

TZ = timezone(timedelta(hours=1))

# 4,8 kWh/kg, 85 % og COP 4,25 giver præcis 0,96 kWh strøm pr. kg.
KWH, EFF, COP = 4.8, 0.85, 4.25
PER_KG = KWH * EFF / COP


def at(day, hour=12, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ).timestamp()


def feed(load, start, readings, cop=COP, step=60):
    """Én aflæsning af døgntallet pr. cyklus. Giver tidspunktet efter sidste."""
    now = start
    for kg in readings:
        load.observe(now, kg, cop, KWH, EFF)
        now += step
    return now


class CountingTest(unittest.TestCase):
    def test_kg_become_the_electricity_the_heat_pump_would_have_used(self):
        load = PelletLoad()

        # Døgntallet kommer i spring, som fyret melder det.
        feed(load, at(29), [10.0, 10.0, 10.5, 10.5, 11.2, 12.0])

        self.assertAlmostEqual(load.today.kg, 2.0, places=6)
        self.assertAlmostEqual(load.today.kwh, 2.0 * PER_KG, places=6)
        self.assertAlmostEqual(load.total_kwh, 2.0 * PER_KG, places=6)

    def test_the_first_reading_is_only_a_starting_point(self):
        # Efter en start ved vi ikke hvornår dagens kg blev brændt. Lagt
        # til på ét minut ville de give Predbat et spring på flere kWh.
        load = PelletLoad()

        feed(load, at(29), [8.0])

        self.assertEqual(load.total_kwh, 0.0)

    def test_a_drop_is_a_new_starting_point_not_consumption(self):
        # Midnat, eller en føler der hikker. Et fald talt som forbrug kunne
        # lægge en hel dags piller til to gange.
        load = PelletLoad()

        feed(load, at(29), [2.0, 0.0, 0.5])
        self.assertAlmostEqual(load.today.kg, 0.5, places=6)

        # Et fald der ikke går helt i nul, må heller ikke tælles som ny dag.
        feed(load, at(29, 14), [3.0, 1.0, 1.5])
        self.assertAlmostEqual(load.today.kg, 1.0, places=6)

    def test_an_unreadable_value_keeps_the_starting_point(self):
        load = PelletLoad()

        feed(load, at(29), [1.0, None, 1.5])

        self.assertAlmostEqual(load.today.kg, 0.5, places=6)

    def test_a_gap_is_not_counted(self):
        load = PelletLoad()
        load.observe(at(29, 12), 1.0, COP, KWH, EFF)
        load.observe(at(29, 13), 2.0, COP, KWH, EFF)

        self.assertEqual(load.total_kwh, 0.0)
        # Men den nye værdi er udgangspunkt for det næste.
        load.observe(at(29, 13, 1), 2.5, COP, KWH, EFF)
        self.assertAlmostEqual(load.today.kg, 0.5, places=6)

    def test_an_unreasonable_jump_is_ignored(self):
        load = PelletLoad()

        feed(load, at(29), [1.0, 1.0 + MAX_KG_STEP + 0.5, 4.8])

        self.assertAlmostEqual(load.today.kg, 0.3, places=6)

    def test_the_last_good_cop_is_used_when_it_goes_missing(self):
        load = PelletLoad()
        now = feed(load, at(29), [1.0, 1.0])

        feed(load, now, [2.0], cop=None)

        self.assertAlmostEqual(load.total_kwh, PER_KG, places=6)

    def test_without_any_cop_the_kg_are_counted_but_no_electricity(self):
        load = PelletLoad()

        feed(load, at(29), [1.0, 2.0], cop=None)

        self.assertAlmostEqual(load.today.kg, 1.0, places=6)
        self.assertEqual(load.total_kwh, 0.0)

    def test_the_cop_of_the_moment_is_used(self):
        load = PelletLoad()

        feed(load, at(29), [0.0, 1.0], cop=4.0)
        feed(load, at(29, 14), [1.0, 2.0], cop=6.0)

        self.assertAlmostEqual(
            load.total_kwh, KWH * EFF / 4.0 + KWH * EFF / 6.0, places=6
        )


class TotalTest(unittest.TestCase):
    def test_the_total_never_falls_across_days(self):
        load = PelletLoad()
        feed(load, at(28), [0.0, 3.0])
        before = load.total_kwh

        feed(load, at(29), [0.0, 0.0])

        self.assertEqual(load.total_kwh, before)
        self.assertEqual(load.today.kwh, 0.0)
        self.assertAlmostEqual(load.yesterday.kg, 3.0, places=6)

    def test_survives_a_restart_and_starts_over_from_the_first_reading(self):
        load = PelletLoad()
        feed(load, at(29), [0.0, 2.0])

        again = PelletLoad.from_raw(load.to_raw())
        feed(again, at(29, 14), [5.0, 5.5])

        self.assertAlmostEqual(again.total_kwh, 2.5 * PER_KG, places=3)
        self.assertAlmostEqual(again.today.kg, 2.5, places=3)
        self.assertEqual(again.cop, COP)

    def test_the_published_total_never_falls_across_a_restart(self):
        # Udgives med tre decimaler. Var den gemt afrundet til fire, kunne
        # 0,1235001 udgives som 0,124 og efter genstarten som 0,123.
        load = PelletLoad(total_kwh=0.1235001)

        again = PelletLoad.from_raw(load.to_raw())

        self.assertGreaterEqual(round(again.total_kwh, 3), round(load.total_kwh, 3))

    def test_garbage_on_disk_gives_an_empty_counter(self):
        for raw in (None, [], {"total_kwh": "x", "days": [1, {"kg": 2}]}):
            load = PelletLoad.from_raw(raw)
            self.assertEqual(load.total_kwh, 0.0)
            self.assertIsNone(load.today)

    def test_yesterday_is_only_the_day_before(self):
        load = PelletLoad()
        feed(load, at(26), [0.0, 1.0])
        feed(load, at(29), [0.0, 1.0])

        self.assertIsNone(load.yesterday)


class PowerTest(unittest.TestCase):
    def test_power_is_the_last_quarter_hour_in_watts(self):
        load = PelletLoad()
        now = feed(load, at(29), [0.0, 0.25])

        # 0,25 kg = 0,24 kWh over et kvarter = 960 W.
        self.assertAlmostEqual(load.power_w(now), 960.0, places=3)

    def test_power_falls_to_zero_when_the_burner_stops(self):
        load = PelletLoad()
        now = feed(load, at(29), [0.0, 0.25])

        self.assertEqual(load.power_w(now + 16 * 60), 0.0)


if __name__ == "__main__":
    unittest.main()
