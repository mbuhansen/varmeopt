import unittest

from varmeopt.cop import Cell, CopTable
from varmeopt.curve import HeatCurve, Point


def curve(points=None, dhw=56.0):
    return HeatCurve({u: Point(s, n) for u, (s, n) in (points or {}).items()}, dhw_setpoint=dhw)


class LearnTest(unittest.TestCase):
    def test_first_observation_becomes_the_point(self):
        c = curve()

        note = c.learn(outdoor=5.0, setpoint=44.0)

        self.assertEqual(c.point(5).setpoint, 44.0)
        self.assertEqual(c.point(5).count, 1.0)
        self.assertIn("nyt punkt", note)

    def test_outdoor_is_rounded_to_whole_degrees(self):
        c = curve()
        c.learn(outdoor=4.7, setpoint=44.0)

        self.assertEqual(c.outdoor_temps, [5])

    def test_a_settled_point_barely_moves(self):
        # 500 målinger bag et punkt må ikke rykke sig på én afvigende aflæsning.
        c = curve({5: (44.0, 500.0)})
        c.learn(outdoor=5.0, setpoint=54.0)

        self.assertAlmostEqual(c.point(5).setpoint, 44.5, places=6)

    def test_a_fresh_point_moves_faster(self):
        c = curve({5: (44.0, 2.0)})
        c.learn(outdoor=5.0, setpoint=54.0)

        self.assertAlmostEqual(c.point(5).setpoint, 45.5, places=6)


class DomesticHotWaterTest(unittest.TestCase):
    def test_hot_water_setpoint_is_kept_out_of_the_curve(self):
        # Kalder beholderen eller spabadet, overstyres kurven med et fast
        # setpunkt. Det siger intet om vejret og hører ikke til her.
        c = curve()

        note = c.learn(outdoor=17.0, setpoint=56.0)

        self.assertEqual(c.point_count, 0)
        self.assertIn("varmtvand", note)

    def test_tolerance_catches_a_slightly_drifting_setpoint(self):
        c = curve()
        c.learn(outdoor=17.0, setpoint=55.7)

        self.assertEqual(c.point_count, 0)

    def test_heating_near_but_not_at_the_hot_water_setpoint_is_learned(self):
        # Ved streng frost kan kurven selv nå højt op, og de målinger tæller.
        c = curve()
        c.learn(outdoor=-15.0, setpoint=54.0)

        self.assertEqual(c.point_count, 1)


class PredictTest(unittest.TestCase):
    def setUp(self):
        self.c = curve({0: (45.0, 100.0), 10: (40.0, 100.0), 20: (28.0, 100.0)})

    def test_exact_point(self):
        self.assertAlmostEqual(self.c.predict(10), 40.0)

    def test_interpolates_between_points(self):
        self.assertAlmostEqual(self.c.predict(5), 42.5)
        self.assertAlmostEqual(self.c.predict(15), 34.0)

    def test_clamps_below_the_measured_range(self):
        # Kurven har en reel bund; en lineær forlængelse ville finde på tal
        # anlægget aldrig har vist os.
        self.assertAlmostEqual(self.c.predict(-20), 45.0)

    def test_clamps_above_the_measured_range(self):
        self.assertAlmostEqual(self.c.predict(40), 28.0)

    def test_empty_curve_predicts_nothing(self):
        self.assertIsNone(curve().predict(5))

    def test_confidence_reports_the_nearest_point(self):
        c = curve({0: (45.0, 12.0), 10: (40.0, 900.0)})

        self.assertEqual(c.confidence(9), 900.0)
        self.assertEqual(c.confidence(1), 12.0)


class BootstrapTest(unittest.TestCase):
    def test_curve_is_derived_from_the_cop_table(self):
        # Vægtet middel: (40×10 + 30×30) / 40 = 32,5
        table = CopTable(
            {
                40: {5: Cell(cop=4.0, count=10.0)},
                30: {5: Cell(cop=4.8, count=30.0)},
            }
        )
        c = HeatCurve.from_cop_table(table)

        self.assertAlmostEqual(c.point(5).setpoint, 32.5, places=6)
        self.assertEqual(c.point(5).count, 40.0)

    def test_hot_water_column_is_excluded_from_the_bootstrap(self):
        table = CopTable(
            {
                40: {5: Cell(cop=4.0, count=10.0)},
                56: {5: Cell(cop=3.5, count=900.0)},
            }
        )
        c = HeatCurve.from_cop_table(table, dhw_setpoint=56.0)

        self.assertAlmostEqual(c.point(5).setpoint, 40.0, places=6)
        self.assertEqual(c.point(5).count, 10.0)

    def test_empty_table_gives_an_empty_curve(self):
        self.assertEqual(HeatCurve.from_cop_table(CopTable()).point_count, 0)


class StorageTest(unittest.TestCase):
    def test_round_trip_keeps_the_curve(self):
        original = curve({0: (45.0, 100.0), 10: (40.25, 7.0)})

        restored = HeatCurve.from_raw(original.to_raw())

        self.assertEqual(restored.outdoor_temps, [0, 10])
        self.assertAlmostEqual(restored.point(10).setpoint, 40.25, places=3)
        self.assertEqual(restored.point(10).count, 7.0)

    def test_malformed_cells_are_dropped_not_fatal(self):
        restored = HeatCurve.from_raw(
            {"0": {"setpoint": 45.0, "count": 3}, "ikke-tal": {"setpoint": 1.0}, "5": {}}
        )

        self.assertEqual(restored.outdoor_temps, [0])

    def test_garbage_input_gives_an_empty_curve(self):
        self.assertEqual(HeatCurve.from_raw("ikke en tabel").point_count, 0)


if __name__ == "__main__":
    unittest.main()
