import unittest

from varmeopt.cop import (
    EXTRAPOLATION_COUNT_CAP,
    FULL_TRUST_COUNT,
    Cell,
    CopTable,
    plausible_cop_range,
    ta_curve_cop,
)


def table(**rows):
    """Byg en tabel: table(**{"40": {5: (4.0, 10)}}) -> celler med (cop, count)."""
    built = {}
    for flow, cells in rows.items():
        built[int(flow.lstrip("f"))] = {
            out: Cell(cop=cop, count=count) for out, (cop, count) in cells.items()
        }
    return CopTable(built)


class LoadRawTest(unittest.TestCase):
    def test_drops_nan_flow_row_and_keeps_the_rest(self):
        # Præcis formen fra det kørende anlæg: en NaN-række blandt gyldige.
        raw = {
            "40": {"5": {"cop": 4.0, "count": 12}},
            "NaN": {"6": {"cop": 4.3, "count": 1}, "17": {"cop": 4.44, "count": 1}},
        }
        t, dropped = CopTable.from_raw(raw)

        self.assertEqual(t.flow_temps, [40])
        self.assertEqual(t.cell_count, 1)
        self.assertEqual(len(dropped), 1)
        self.assertIn("NaN", dropped[0])

    def test_drops_nan_outdoor_key_but_keeps_siblings(self):
        raw = {"40": {"5": {"cop": 4.0, "count": 12}, "NaN": {"cop": 3.0, "count": 2}}}
        t, dropped = CopTable.from_raw(raw)

        self.assertEqual(t.cell_count, 1)
        self.assertEqual(len(dropped), 1)

    def test_rejects_malformed_cells(self):
        raw = {
            "40": {
                "0": {"cop": 4.0, "count": 5},
                "1": {"cop": None, "count": 5},
                "2": {"cop": 0, "count": 5},
                "3": "ikke et objekt",
                "4": {"cop": 4.0, "count": -1},
            }
        }
        t, dropped = CopTable.from_raw(raw)

        self.assertEqual(t.cell_count, 1)
        self.assertEqual(len(dropped), 4)

    def test_round_trip(self):
        raw = {"40": {"5": {"cop": 4.0, "count": 12.0}}}
        t, _ = CopTable.from_raw(raw)
        again, dropped = CopTable.from_raw(t.to_raw())

        self.assertEqual(again.to_raw(), t.to_raw())
        self.assertEqual(dropped, [])

    def test_negative_outdoor_keys_survive(self):
        raw = {"45": {"-10": {"cop": 2.6, "count": 8}}}
        t, dropped = CopTable.from_raw(raw)

        self.assertEqual(dropped, [])
        self.assertEqual(t.row(45)[-10].cop, 2.6)


class LookupTest(unittest.TestCase):
    def test_exact_hit_uses_learned_value(self):
        t = table(f40={5: (4.0, 20)})
        got = t.lookup(40, 5)

        self.assertEqual(got.source, "exact")
        self.assertAlmostEqual(got.cop, 4.0)

    def test_interpolated_cell_is_used_not_discarded(self):
        # Regressionstest for Node-RED-fejlen: interpolerede opslag fik
        # count = 0 og faldt derfor altid tilbage på TA-kurven.
        t = table(f40={0: (4.0, 50), 10: (5.0, 50)})
        got = t.lookup(40, 5)

        self.assertEqual(got.source, "interp")
        self.assertAlmostEqual(got.cop, 4.5)
        self.assertNotAlmostEqual(got.cop, ta_curve_cop(40, 5))

    def test_interpolates_in_both_dimensions(self):
        t = table(
            f40={0: (4.0, 50), 10: (5.0, 50)},
            f50={0: (2.0, 50), 10: (3.0, 50)},
        )
        got = t.lookup(45, 5)

        self.assertEqual(got.source, "interp")
        # F40 giver 4,5 og F50 giver 2,5 ved U5; midtvejs er 3,5.
        self.assertAlmostEqual(got.cop, 3.5)

    def test_weak_neighbour_drags_confidence_down(self):
        # Harmonisk middel: en stærk nabo må ikke redde en tynd.
        t = table(f40={0: (4.0, 100), 10: (5.0, 1)})
        got = t.lookup(40, 5)

        self.assertLess(got.learned_count, 2.0)
        self.assertEqual(got.source, "blend")

    def test_thin_cell_blends_towards_curve(self):
        t = table(f40={5: (2.0, 1)})
        curve = ta_curve_cop(40, 5)
        got = t.lookup(40, 5)

        weight = 1 / FULL_TRUST_COUNT
        self.assertEqual(got.source, "blend")
        self.assertAlmostEqual(got.cop, curve * (1 - weight) + 2.0 * weight)
        self.assertEqual(got.learned_cop, 2.0)

    def test_well_covered_cell_ignores_the_curve(self):
        t = table(f40={5: (2.0, FULL_TRUST_COUNT)})
        got = t.lookup(40, 5)

        self.assertEqual(got.source, "exact")
        self.assertAlmostEqual(got.cop, 2.0)

    def test_empty_table_falls_back_to_curve(self):
        got = CopTable().lookup(40, 5)

        self.assertEqual(got.source, "curve")
        self.assertAlmostEqual(got.cop, ta_curve_cop(40, 5))
        self.assertIsNone(got.learned_cop)

    def test_extrapolation_beyond_flow_range_is_capped(self):
        t = table(f40={5: (4.0, 500)})
        got = t.lookup(58, 5)

        # Vi har aldrig målt ved 58 grader, så den værdi må ikke stå alene.
        self.assertLessEqual(got.learned_count, EXTRAPOLATION_COUNT_CAP)
        self.assertEqual(got.source, "blend")

    def test_extrapolation_beyond_outdoor_range_is_capped(self):
        t = table(f40={5: (4.0, 500), 6: (4.1, 500)})
        got = t.lookup(40, 30)

        self.assertLessEqual(got.learned_count, EXTRAPOLATION_COUNT_CAP)

    def test_real_shape_high_flow_beats_the_curve(self):
        # F56/U8 er målt til COP 4,03 med n=740 på det rigtige anlæg, mens
        # TA-kurven gætter markant lavere. Det er hele pointen med at lære.
        t = table(f56={8: (4.03, 740)})
        got = t.lookup(56, 8)

        self.assertEqual(got.source, "exact")
        self.assertAlmostEqual(got.cop, 4.03)
        self.assertGreater(got.cop, ta_curve_cop(56, 8) + 0.5)


class LearnTest(unittest.TestCase):
    def test_first_sample_creates_cell(self):
        t = CopTable()
        t.learn(40.2, 5.4, 4.0)

        self.assertEqual(t.row(40)[5], Cell(cop=4.0, count=1.0))

    def test_second_sample_uses_fast_alpha(self):
        t = table(f40={5: (4.0, 1)})
        t.learn(40, 5, 5.0)

        self.assertAlmostEqual(t.row(40)[5].cop, 4.0 * 0.85 + 5.0 * 0.15)
        self.assertEqual(t.row(40)[5].count, 2)

    def test_alpha_slows_after_ten_samples(self):
        t = table(f40={5: (4.0, 9)})
        t.learn(40, 5, 5.0)

        self.assertAlmostEqual(t.row(40)[5].cop, 4.0 * 0.95 + 5.0 * 0.05)

    def test_stopped_pump_is_ignored(self):
        t = CopTable()
        msg = t.learn(40, 5, 0)

        self.assertEqual(t.cell_count, 0)
        self.assertIn("stille", msg)

    def test_flow_outside_range_is_ignored(self):
        t = CopTable()
        t.learn(70, 5, 3.0)
        t.learn(10, 5, 3.0)

        self.assertEqual(t.cell_count, 0)

    def test_implausible_cop_for_the_lift_is_rejected(self):
        # 58 graders fremløb ved -5 ude er et løft på 63 K; COP 5 er umuligt
        # og er i praksis afrimning eller målestøj.
        t = CopTable()
        msg = t.learn(58, -5, 5.0)

        self.assertEqual(t.cell_count, 0)
        self.assertIn("COP", msg)

    def test_same_cop_is_plausible_at_a_small_lift(self):
        t = CopTable()
        t.learn(30, 15, 5.0)

        self.assertEqual(t.cell_count, 1)

    def test_missing_temperature_is_ignored_not_bucketed_as_nan(self):
        # Det var sådan NaN-rækken opstod i Node-RED.
        t = CopTable()
        t.learn(float("nan"), 5, 4.0)

        self.assertEqual(t.cell_count, 0)
        self.assertEqual(t.flow_temps, [])


class RangeTest(unittest.TestCase):
    def test_bands_tighten_as_the_lift_grows(self):
        self.assertEqual(plausible_cop_range(30, 15), (2.0, 6.5))
        self.assertEqual(plausible_cop_range(40, 5), (1.5, 5.5))
        self.assertEqual(plausible_cop_range(50, 0), (1.2, 4.0))
        self.assertEqual(plausible_cop_range(58, -5), (1.0, 4.0))


class CurveTest(unittest.TestCase):
    def test_anchor_points_match_the_ta_tables(self):
        self.assertAlmostEqual(ta_curve_cop(35, 0), 4.0)
        self.assertAlmostEqual(ta_curve_cop(45, 0), 3.9)
        self.assertAlmostEqual(ta_curve_cop(55, 0), 3.1)
        self.assertAlmostEqual(ta_curve_cop(60, 0), 2.6)

    def test_higher_flow_never_helps(self):
        for outdoor in (-10, -5, 0, 5, 10, 15):
            values = [ta_curve_cop(f, outdoor) for f in range(35, 61, 5)]
            self.assertEqual(values, sorted(values, reverse=True), f"ude {outdoor}")

    def test_clamps_outside_the_tabulated_range(self):
        self.assertAlmostEqual(ta_curve_cop(35, -40), ta_curve_cop(35, -15))
        self.assertAlmostEqual(ta_curve_cop(35, 40), ta_curve_cop(35, 25))
        self.assertAlmostEqual(ta_curve_cop(20, 0), ta_curve_cop(35, 0))
        self.assertAlmostEqual(ta_curve_cop(70, 0), ta_curve_cop(60, 0))


if __name__ == "__main__":
    unittest.main()
