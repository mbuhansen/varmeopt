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
        c.learn(outdoor=5.0, setpoint=48.0)

        self.assertAlmostEqual(c.point(5).setpoint, 44.2, places=6)

    def test_a_fresh_point_moves_faster(self):
        c = curve({5: (44.0, 2.0)})
        c.learn(outdoor=5.0, setpoint=48.0)

        self.assertAlmostEqual(c.point(5).setpoint, 44.6, places=6)

    def test_a_leap_above_a_settled_point_is_hot_water_not_weather(self):
        # Ti grader over et punkt med 500 maalinger bag sig er ikke vejret.
        # Brugsvand og spa varmer altid hedere end huset har brug for, saa
        # skaevheden er ensidig - og det er den asymmetri testen her holder
        # fast i.
        c = curve({5: (44.0, 500.0)})

        note = c.learn(outdoor=5.0, setpoint=54.0)

        self.assertIn("varmtvand", note)
        self.assertAlmostEqual(c.point(5).setpoint, 44.0, places=6)

    def test_a_thin_point_has_no_veto(self):
        # Et punkt der selv er et gaet, skal ikke kunne afvise nye maalinger.
        c = curve({5: (44.0, 2.0)})

        note = c.learn(outdoor=5.0, setpoint=54.0)

        self.assertNotIn("ignoreret", note)

    def test_a_drop_is_always_the_weather(self):
        # Testen er ensidig med vilje: en justering *nedad* er aldrig
        # varmtvand, og den skal laeres med det samme.
        c = curve({5: (44.0, 500.0)})

        note = c.learn(outdoor=5.0, setpoint=30.0)

        self.assertNotIn("ignoreret", note)


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


class MonotoneTest(unittest.TestCase):
    """En varmekurve kan ikke stige naar det bliver varmere ude."""

    def test_the_derived_curve_never_rises_with_the_outdoor_temperature(self):
        # Det virkelige anlaegs tabel havde seks brud. Det her er et af dem:
        # U9 gav 38,8 og U10 gav 40,8 - en varmere prognose gav et hoejere
        # setpunkt og dermed lavere COP, 2 K den forkerte vej.
        table = CopTable({
            38: {9: Cell(4.0, 181.0)},
            41: {10: Cell(4.0, 85.0)},
            45: {0: Cell(3.5, 400.0)},
        })

        c = HeatCurve.from_cop_table(table)
        points = [c.predict(u) for u in sorted(c.outdoor_temps)]

        self.assertEqual(points, sorted(points, reverse=True))

    def test_pooling_follows_the_weight_not_the_midpoint(self):
        # 181 maalinger paa 38 mod 85 paa 41: det faelles svar skal ligge
        # naermest de 38, ikke midtvejs.
        table = CopTable({
            38: {9: Cell(4.0, 181.0)},
            41: {10: Cell(4.0, 85.0)},
        })

        c = HeatCurve.from_cop_table(table)

        self.assertAlmostEqual(c.predict(9), c.predict(10), places=9)
        self.assertLess(c.predict(9), 39.5)

    def test_an_already_falling_curve_is_left_alone(self):
        table = CopTable({
            50: {-5: Cell(3.0, 100.0)},
            40: {5: Cell(4.0, 100.0)},
            30: {15: Cell(5.0, 100.0)},
        })

        c = HeatCurve.from_cop_table(table)

        self.assertAlmostEqual(c.predict(-5), 50.0, places=6)
        self.assertAlmostEqual(c.predict(5), 40.0, places=6)
        self.assertAlmostEqual(c.predict(15), 30.0, places=6)


class ConfidenceTest(unittest.TestCase):
    def test_distance_costs_confidence(self):
        # Foer returnerede -20 de 70 maalinger der staar ved -10, som om der
        # var maalt dernede. Der er bare ingen maalinger inden for 10 K.
        c = curve({-10: (53.0, 70.0)})

        self.assertAlmostEqual(c.confidence(-10), 70.0, places=6)
        self.assertLess(c.confidence(-20), 10.0)

    def test_a_neighbour_a_degree_away_still_counts(self):
        c = curve({-10: (53.0, 70.0)})

        self.assertAlmostEqual(c.confidence(-11), 70.0, places=6)

