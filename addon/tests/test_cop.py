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
        # En stærk nabo må ikke redde en tynd. Halv vægt på en enkelt
        # måling giver en fjerdedel af dens støj, altså n_eff 4 - stadig
        # langt fra fuld tillid, men ikke de 2 den gamle w/n-form gav.
        t = table(f40={0: (4.0, 100), 10: (5.0, 1)})
        got = t.lookup(40, 5)

        self.assertLess(got.learned_count, 5.0)
        self.assertEqual(got.source, "blend")

    def test_a_sliver_of_a_thin_cell_no_longer_halves_the_trust(self):
        # 99 % af en celle med 100 målinger, 1 % af en med een. Den gamle
        # w/n-form gav 50 - halveret af en hundrededel.
        t = table(f40={0: (4.0, 100), 100: (5.0, 1)})
        got = t.lookup(40, 1)

        self.assertGreater(got.learned_count, 90.0)

    def test_confidence_never_exceeds_the_best_measured_endpoint(self):
        # To uafhængige skøn kan variansmæssigt bære mere end hver for
        # sig, men de er skøn over hvert sit driftspunkt.
        t = table(f40={0: (4.0, 100), 10: (4.0, 100)})
        got = t.lookup(40, 5)

        self.assertLessEqual(got.learned_count, 100.0)

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
    def test_ceiling_falls_as_the_lift_grows(self):
        ceilings = [plausible_cop_range(f, u)[1] for f, u in
                    ((30, 15), (40, 5), (50, 0), (58, -5))]

        self.assertEqual(ceilings, sorted(ceilings, reverse=True))

    def test_ceiling_never_exceeds_carnot(self):
        # Loftet skal ligge *under* den termodynamiske grænse, ellers filtrerer
        # det ikke andet end det absolutte tal.
        for flow in range(20, 66, 5):
            for outdoor in range(-15, 21, 5):
                carnot = (flow + 273.15) / max(1.0, flow - outdoor)
                self.assertLessEqual(plausible_cop_range(flow, outdoor)[1], carnot)

    def test_floor_is_flat_so_defrost_is_not_discarded(self):
        # Det gamle gulv steg til 2,0 ved lille løft. En modulerende pumpe
        # under afrimning *har* lav COP, og den måling hører med.
        t = CopTable()
        t.learn(30, 15, 1.4)

        self.assertEqual(t.cell_count, 1)

    def test_the_bands_used_to_reject_half_the_plant(self):
        # Kernen i fejlen: loftet på 4,0 for delta-T 40-55 K lå på medianen
        # af netop det bånd hvor anlægget bruger halvdelen af sin tid.
        # F56/U8 med 740 målinger på COP 4,03 er den tungeste af dem.
        t = CopTable()
        msg = t.learn(56, 8, 4.03)

        self.assertEqual(t.cell_count, 1, msg)

    def test_sensor_nonsense_is_still_rejected(self):
        t = CopTable()
        # Over Carnot ved samme løft - fysisk umuligt, uanset maskine.
        self.assertIn("COP", t.learn(56, 8, 12.0))
        # Og under 1 leverer maskinen mindre varme end den bruger strøm.
        self.assertIn("COP", t.learn(56, 8, 0.4))

        self.assertEqual(t.cell_count, 0)


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




class ReinforceTest(unittest.TestCase):
    """En tynd række skal låne af naboerne, ikke af fabrikskurven."""

    def test_a_thin_exact_row_borrows_from_a_well_measured_neighbour(self):
        # F24 har to målinger ved U18; F26 har 37. Før blev de 37 ignoreret
        # fordi fremløbet ramte F24 præcis, og resten blev hentet i
        # fabrikskurven, som ikke ved noget om dette anlæg.
        t = table(f24={18: (4.73, 2)}, f26={18: (4.58, 37)})

        got = t.lookup(24, 18)

        # Lånet fylder op til fuld tillid og ikke længere.
        self.assertGreaterEqual(got.learned_count, FULL_TRUST_COUNT)
        self.assertNotEqual(got.source, "blend")
        self.assertLess(abs(got.cop - 4.60), 0.05)
        self.assertIn("styrket af F26", got.detail)

    def test_the_exact_row_still_weighs_most_per_measurement(self):
        # Naboen vejer med sin evidens delt med afstanden. Lige mange
        # målinger, så skal den eksakte række trække mest.
        t = table(f40={0: (3.0, 4)}, f45={0: (5.0, 4)})

        got = t.lookup(40, 0)

        self.assertLess(got.cop, 4.0)

    def test_a_distant_row_is_not_a_neighbour(self):
        t = table(f30={0: (4.73, 2)}, f45={0: (3.00, 500)})

        got = t.lookup(30, 0)

        self.assertAlmostEqual(got.learned_count, 2.0, places=9)
        self.assertNotIn("styrket", got.detail)

    def test_a_well_measured_row_is_left_alone(self):
        t = table(f40={0: (3.0, 50)}, f42={0: (5.0, 500)})

        got = t.lookup(40, 0)

        self.assertAlmostEqual(got.cop, 3.0, places=9)
        self.assertEqual(got.source, "exact")

    def test_a_thin_interpolation_borrows_too(self):
        # Mellem to tynde rækker skal opslaget ikke ende i fabrikskurven,
        # når en række lidt længere væk har rigelig evidens.
        t = table(f40={10: (4.7, 1)}, f41={10: (4.8, 1)}, f42={10: (5.2, 400)})

        got = t.lookup(40.8, 10)

        # Lånet fylder op til fuld tillid og ikke længere.
        self.assertGreaterEqual(got.learned_count, FULL_TRUST_COUNT)
        self.assertGreater(got.cop, 5.0)


# Cellerne omkring fremløb 33-35 som de lå på anlægget den 13. september
# kl. 22:24. F34/U13 er et udsving - 2,86 på tre målinger - og netop derfor
# er det her opslaget sprang.
SEPTEMBER_13 = dict(
    f32={13: (4.54, 27), 14: (4.97, 44), 15: (4.30, 20), 16: (4.45, 44)},
    f33={13: (4.88, 18), 14: (4.51, 13), 15: (4.34, 10), 16: (4.53, 19)},
    f34={11: (5.28, 1), 12: (4.25, 11), 13: (2.86, 3), 14: (4.24, 5), 15: (4.08, 9), 16: (4.29, 1)},
    f35={10: (4.18, 5), 11: (4.36, 9), 12: (3.89, 2), 13: (5.34, 6), 14: (4.11, 6), 15: (3.43, 3), 16: (4.10, 2)},
    f36={10: (4.71, 28), 12: (3.68, 4), 14: (3.91, 4), 15: (4.47, 3)},
)


class ContinuityTest(unittest.TestCase):
    """En tiendedel grad må ikke flytte COP'en en sjettedel.

    Kl. 22 den 13. september gik opslaget 3,47 -> 4,03 -> 3,37 på få minutter
    mens setpunktet krøb fra 33,7 til 33,8 og udetemperaturen vippede mellem
    13,0 og 13,1. Naborækkerne blev slået til og fra af en tærskel, et tidligt
    stop og en hård afstandsgrænse, og beslutningen vippede med.
    """

    STEP = 0.01
    # En ægte hældning i tabellen er et par kroner pr. kelvin i det værste
    # hjørne. Det er 0,02 pr. skridt; 0,05 giver luft og fanger et spring.
    MAX_JUMP = 0.05

    def _sweep(self, t, flows, outdoors):
        worst = (0.0, None)
        for flow in flows:
            prev = None
            for out in outdoors:
                cop = t.lookup(flow, out).cop
                if prev is not None and abs(cop - prev[1]) > worst[0]:
                    worst = (abs(cop - prev[1]), (flow, prev[0], out))
                prev = (out, cop)
        for out in outdoors:
            prev = None
            for flow in flows:
                cop = t.lookup(flow, out).cop
                if prev is not None and abs(cop - prev[1]) > worst[0]:
                    worst = (abs(cop - prev[1]), (out, prev[0], flow))
                prev = (flow, cop)
        return worst

    @staticmethod
    def _range(lo, hi, step):
        n = round((hi - lo) / step)
        return [round(lo + i * step, 4) for i in range(n + 1)]

    def test_the_evening_of_13_september_has_no_jumps(self):
        t = table(**SEPTEMBER_13)

        jump, where = self._sweep(
            t, self._range(33.0, 35.0, self.STEP), self._range(12.5, 13.5, self.STEP)
        )

        self.assertLess(jump, self.MAX_JUMP, f"spring på {jump:.2f} ved {where}")

    def test_a_neighbour_fades_out_at_the_edge_of_its_reach(self):
        # En tyk række lige ved grænsen må ikke falde ud i ét hug.
        t = table(f40={0: (3.0, 2)}, f45={0: (5.0, 500)})

        jump, where = self._sweep(t, self._range(39.0, 41.0, self.STEP), [0])

        self.assertLess(jump, self.MAX_JUMP, f"spring på {jump:.2f} ved {where}")

    def test_the_edge_of_a_row_does_not_cut_its_evidence_in_one_step(self):
        # Uden for en rækkes udetemperaturer gælder kun svag evidens. Men et
        # skridt fra 13,0 til 12,99 må ikke tage de atten målinger på én gang.
        t = table(f33={13: (4.88, 18), 14: (4.51, 13)})

        jump, where = self._sweep(t, [33], self._range(11.0, 14.0, self.STEP))

        self.assertLess(jump, self.MAX_JUMP, f"spring på {jump:.2f} ved {where}")


    def test_a_row_with_no_evidence_here_does_not_zero_its_neighbour(self):
        # F57 har kun celler ved -3, så ved U7,9 ligger den mere end fem
        # grader fra sin kant og bærer ingen evidens. Før blev hele
        # interpolationen F56-57 så sat til nul målinger allerede en
        # titusindedel over F56 - og COP'en faldt fra 4,03 til faldets tal.
        t = table(f56={8: (4.03, 740)}, f57={-3: (3.0, 50)})

        # Tæt på rækken: ved 56,01 redder lånet fra F56 selv opslaget, men
        # ikke ved 56,0001.
        flows = [55.99, 56.0, 56.0001, 56.001, 56.01, 56.02]
        jump, where = self._sweep(t, flows, [7.9])

        self.assertLess(jump, self.MAX_JUMP, f"spring på {jump:.2f} ved {where}")

    def test_rows_measured_in_different_weather_meet_smoothly(self):
        t = table(f40={12: (5.0, 6)}, f41={5: (4.5, 6)})

        flows = self._range(40.9, 41.1, self.STEP) + [40.999, 41.0, 41.0001]
        for outdoor in (6.0, 9.0):
            jump, where = self._sweep(t, sorted(flows), [outdoor])

            self.assertLess(jump, self.MAX_JUMP, f"spring på {jump:.2f} ved {where}")

    def test_a_row_itself_and_a_hair_beside_it_agree_on_thin_evidence(self):
        # F43 er kun målt ved U9, så ved U12,8 har den under én målings
        # evidens tilbage. Rækken selv og interpolationen en titusindedel
        # ved siden af skal regne den ens - ellers springer COP'en på rækken.
        t = CopTable(
            {42: {17: Cell(cop=5.8, count=1.0)}, 43: {9: Cell(cop=3.0, count=50.0)}},
            fallback=table(f42={13: (4.0, 500)}, f43={13: (4.0, 500)}),
        )

        beside, on = t.lookup(42.9999, 12.8).cop, t.lookup(43.0, 12.8).cop

        self.assertLess(abs(beside - on), 0.005)


class FallbackTest(unittest.TestCase):
    """BT12-tabellen starter tom og står på setpunkt-tabellens skuldre.

    Tabellen læres fra 14. september på varmepumpens eget fremløb, BT12. Den
    gamle, lært på UVR'ens setpunkt, har sytten tusind målinger og er det
    bedste bud indtil den nye har sine egne - men den er forurenet af glidende
    opladning, hvor pumpen gik mod 60 mens UVR'en viste 33, og derfor skal den
    vige celle for celle i takt med at der kommer rigtige målinger.
    """

    def test_an_empty_bt12_table_answers_with_the_setpoint_table(self):
        old = table(f35={10: (4.4, 50)})
        new = CopTable(fallback=old)

        got = new.lookup(35, 10)

        self.assertAlmostEqual(got.cop, 4.4)
        self.assertEqual(got.source, "fallback")
        self.assertIn("setpunkt-tabel", got.detail)

    def test_the_new_table_takes_over_one_measurement_at_a_time(self):
        old = table(f35={10: (4.4, 50)})
        values = []
        for n in range(7):
            cells = {35: {10: Cell(cop=3.4, count=float(n))}} if n else None
            values.append(CopTable(cells, fallback=old).lookup(35, 10).cop)

        # Fra den gamle tabels tal mod den nyes, aldrig tilbage.
        self.assertEqual(values, sorted(values, reverse=True))
        # Med én måling vejer den nye en femtedel - mod den gamle tabel, ikke
        # mod fabrikskurven.
        self.assertAlmostEqual(values[1], 4.4 * 0.8 + 3.4 * 0.2)
        self.assertAlmostEqual(values[5], 3.4)
        self.assertAlmostEqual(values[6], 3.4)

    def test_the_fallback_is_not_written_with_the_table(self):
        old = table(f35={10: (4.4, 50)})
        new = CopTable(fallback=old)
        new.learn(40, 10, 4.0)

        self.assertEqual(list(new.to_raw()), ["40"])
        self.assertEqual(new.sample_count, 1.0)


if __name__ == "__main__":
    unittest.main()
