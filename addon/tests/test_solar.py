import unittest

from varmeopt.solar import DayTracker, Geometry, Plane, SolarModel, daily_irradiance, diffuse_fraction, seed_scale

# Anlægget på Fyn: fire solfangere i syd med 45°, mod 6,4 kW syd/20° og
# 4 kW vest/15° solceller.
FYN = Geometry(
    latitude=55.4,
    thermal=Plane(tilt=45.0, azimuth=0.0),
    pv=(Plane(20.0, 0.0, 6.4), Plane(15.0, 90.0, 4.0)),
)

MIDSUMMER = 172
MIDWINTER = 355


def model(scale=None, days=0.0):
    return SolarModel(FYN, scale=scale, days=days)


class IncidenceTest(unittest.TestCase):
    def test_a_steep_south_panel_beats_a_flat_one_in_winter(self):
        steep = daily_irradiance(MIDWINTER, 55.4, Plane(45.0, 0.0))
        flat = daily_irradiance(MIDWINTER, 55.4, Plane(15.0, 0.0))

        self.assertGreater(steep, flat)

    def test_and_loses_to_it_in_summer(self):
        steep = daily_irradiance(MIDSUMMER, 55.4, Plane(45.0, 0.0))
        flat = daily_irradiance(MIDSUMMER, 55.4, Plane(15.0, 0.0))

        self.assertLess(steep, flat)

    def test_the_sun_is_up_far_longer_in_june(self):
        self.assertGreater(
            daily_irradiance(MIDSUMMER, 55.4, Plane(0.0, 0.0)),
            3 * daily_irradiance(MIDWINTER, 55.4, Plane(0.0, 0.0)),
        )


class DiffuseTest(unittest.TestCase):
    def test_the_sky_carries_most_of_the_light_here(self):
        # 55,4° nord. Under halvdelen kommer fra solskiven, selv midt om
        # sommeren, og om vinteren er det fire femtedele.
        self.assertGreater(diffuse_fraction(MIDSUMMER), 0.45)
        self.assertGreater(diffuse_fraction(MIDWINTER), diffuse_fraction(MIDSUMMER))
        self.assertLess(diffuse_fraction(MIDWINTER), 0.95)

    def test_a_flat_plane_sees_the_whole_sky_and_a_steep_one_does_not(self):
        # Udsynsfaktoren er hele grunden til at aarstidsudsvinget er mindre
        # end den direkte straaling alene siger.
        for doy in (MIDSUMMER, MIDWINTER):
            flat = daily_irradiance(doy, 55.4, Plane(0.0, 0.0))
            steep = daily_irradiance(doy, 55.4, Plane(90.0, 0.0))
            self.assertGreater(flat, 0.0)
            self.assertGreater(steep, 0.0)


class GeometryTest(unittest.TestCase):
    def test_the_ratio_swings_across_the_year(self):
        # Det er hele grunden til at geometrien regnes i stedet for at læres:
        # en fast faktor ville være forkert det halve af året.
        summer = FYN.ratio(MIDSUMMER)
        winter = FYN.ratio(MIDWINTER)

        self.assertLess(summer, 1.0)
        self.assertGreater(winter, 1.2)
        self.assertGreater(winter / summer, 1.3)

    def test_the_swing_is_not_the_one_beam_alone_would_predict(self):
        # Regnet paa kun den direkte straaling svinger forholdet 2,5x og
        # december lander paa 2,27. Det lovede planlaeggeren 74 % mere
        # solvarme end anlaegget kan levere, i den maaned hvor et forkert
        # loefte er dyrest.
        winter = FYN.ratio(MIDWINTER)

        self.assertLess(winter, 1.6)

    def test_identical_planes_give_a_ratio_of_one(self):
        same = Geometry(55.4, Plane(30.0, 0.0), (Plane(30.0, 0.0, 1.0),))

        self.assertAlmostEqual(same.ratio(MIDSUMMER), 1.0, places=9)

    def test_no_pv_planes_gives_no_ratio(self):
        self.assertIsNone(Geometry(55.4, Plane(45.0, 0.0), ()).ratio(MIDSUMMER))


class LearnTest(unittest.TestCase):
    def test_the_first_day_sets_the_scale(self):
        m = model()
        ratio = m.geometric_ratio(MIDSUMMER)

        m.learn(thermal_kwh=20.0, pv_forecast_kwh=50.0, day_of_year=MIDSUMMER)

        self.assertAlmostEqual(m.scale, 20.0 / (50.0 * ratio), places=9)
        self.assertTrue(m.known)

    def test_a_full_store_teaches_nothing(self):
        # Kernen: en dag hvor tanken var fuld siger "der var ikke plads",
        # ikke "solen var dårlig". Lærer vi af den, forgifter vi tallet.
        m = model(scale=0.4, days=20.0)

        note = m.learn(5.0, 50.0, MIDSUMMER, store_was_full=True)

        self.assertAlmostEqual(m.scale, 0.4, places=9)
        self.assertIn("fuldt", note)

    def test_a_grey_day_teaches_nothing_either(self):
        m = model(scale=0.4, days=20.0)

        m.learn(0.1, 0.2, MIDSUMMER)

        self.assertAlmostEqual(m.scale, 0.4, places=9)

    def test_good_and_bad_days_move_the_scale_equally_far(self):
        # Laeringen er symmetrisk. Det var den ikke: op med alfa 0,5, ned med
        # 0,05, saa maetning ikke skulle traekke tallet ned. Men maetning
        # filtreres allerede fra, og en skaev EMA er ikke en filtrering.
        up, down = model(scale=0.40, days=20.0), model(scale=0.40, days=20.0)
        ratio = up.geometric_ratio(MIDSUMMER)

        up.learn(0.50 * 50.0 * ratio, 50.0, MIDSUMMER)
        down.learn(0.30 * 50.0 * ratio, 50.0, MIDSUMMER)

        self.assertAlmostEqual(up.scale - 0.40, 0.40 - down.scale, places=9)

    def test_symmetric_noise_no_longer_biases_the_scale_upward(self):
        # Med 0,5 op mod 0,05 ned lagde en sand vaerdi paa 0,40 sig 12-37 %
        # for hoejt afhaengigt af spredningen, ogsaa naar stoejen var helt
        # symmetrisk: hvert udsving opad blev troet ti gange saa meget som
        # det tilsvarende nedad.
        m = model(scale=0.40, days=20.0)
        ratio = m.geometric_ratio(MIDSUMMER)

        for step in range(200):
            wobble = 0.10 if step % 2 else -0.10
            m.learn((0.40 + wobble) * 50.0 * ratio, 50.0, MIDSUMMER)

        self.assertLess(abs(m.scale - 0.40), 0.02)

    def test_a_regulated_day_is_filtered_not_smoothed_away(self):
        # 24. august koerte frit: PV 60,9 kWh, solvarme 29 kWh, top 5,4 kW.
        # 27. august var reguleret: PV faldt kun 9 %, solvarmen 34 %, og
        # toppen naaede kun 3,6 kW paa et anlaeg der kan 5,4.
        #
        # Den dag skal kasseres, ikke daempes. Det er det store_was_full er til.
        free = model()
        free.learn(29.0, 60.9, 236)
        truth = free.scale

        both = model()
        both.learn(29.0, 60.9, 236)
        both.learn(19.0, 55.4, 239, store_was_full=True)

        self.assertEqual(both.scale, truth)


class PersistenceTest(unittest.TestCase):
    def test_a_scale_survives_a_restart(self):
        m = SolarModel.from_raw(model(scale=0.42, days=9.0).to_raw(), FYN)

        self.assertAlmostEqual(m.scale, 0.42)
        self.assertEqual(m.days, 9.0)

    def test_a_scale_from_the_old_geometry_is_discarded(self):
        # Version 1 regnede kun direkte straaling, saa 0,42 betoed noget
        # andet end det goer nu. At laese det videre ville blande to
        # malestokke; det koster et doegn at laere forfra.
        m = SolarModel.from_raw({"scale": 0.42, "days": 40.0}, FYN)

        self.assertIsNone(m.scale)
        self.assertFalse(m.known)


class ExpectTest(unittest.TestCase):
    def test_nothing_is_predicted_before_anything_is_learned(self):
        self.assertIsNone(model().expected_kwh(50.0, MIDSUMMER))
        self.assertFalse(model().known)

    def test_prediction_follows_the_forecast_and_the_season(self):
        m = model(scale=0.4, days=20.0)

        summer = m.expected_kwh(50.0, MIDSUMMER)
        winter = m.expected_kwh(50.0, MIDWINTER)

        # Samme PV-prognose giver mere solvarme om vinteren, fordi 45° møder
        # den lave sol naermere vinkelret. Men kun omkring 45 % mere - ikke
        # de over 100 % den rene direkte straaling ville love, for om
        # vinteren kommer fire femtedele af lyset fra hele himlen, og der
        # ser en flad flade mere end en stejl.
        self.assertGreater(winter, summer * 1.3)
        self.assertLess(winter, summer * 1.7)

    def test_no_forecast_no_prediction(self):
        m = model(scale=0.4, days=20.0)

        self.assertIsNone(m.expected_kwh(None, MIDSUMMER))


class DayTrackerTest(unittest.TestCase):
    def setUp(self):
        self.t = DayTracker()

    def test_a_day_is_only_closed_when_the_next_one_starts(self):
        self.assertIsNone(self.t.observe("2026-08-24", 0, 60.9, 0.0))
        self.assertIsNone(self.t.observe("2026-08-24", 12, 30.0, 18.0))
        self.assertIsNone(self.t.observe("2026-08-24", 23, 0.0, 29.0))

        done = self.t.observe("2026-08-25", 0, 55.0, 0.0)

        self.assertEqual(done, (29.0, 60.9, "2026-08-24", False))

    def test_the_forecast_is_the_one_captured_at_midnight(self):
        # "Resten af dagen" er kun hele dagen hvis man spoerger foer solopgang.
        self.t.observe("2026-08-24", 0, 60.9, 0.0)
        self.t.observe("2026-08-24", 14, 20.0, 22.0)

        done = self.t.observe("2026-08-25", 0, 55.0, 0.0)

        self.assertEqual(done[1], 60.9)

    def test_a_day_started_in_the_afternoon_is_not_learned(self):
        # Add-on'en blev startet kl. 14. Da er "resten af dagen" ikke hele
        # dagen, og forholdet ville blive helt skaevt.
        self.t.observe("2026-08-24", 14, 20.0, 22.0)
        self.t.observe("2026-08-24", 23, 0.0, 29.0)

        self.assertIsNone(self.t.observe("2026-08-25", 0, 55.0, 0.0))

    def test_saturation_seen_during_the_day_is_carried_to_the_end(self):
        # Ved midnat er tankene koelet af. Saa maetningen skal huskes fra da
        # den skete, ikke aflaeses naar doegnet gores op.
        self.t.observe("2026-08-27", 0, 55.4, 0.0)
        self.t.observe("2026-08-27", 13, 20.0, 15.0, store_full=True)
        self.t.observe("2026-08-27", 23, 0.0, 19.0, store_full=False)

        done = self.t.observe("2026-08-28", 0, 50.0, 0.0)

        self.assertTrue(done[3])

    def test_saturation_resets_with_the_new_day(self):
        self.t.observe("2026-08-27", 0, 55.4, 0.0, store_full=True)
        self.t.observe("2026-08-28", 0, 50.0, 0.0)

        self.assertFalse(self.t.saturated)

    def test_round_trip_survives_a_restart(self):
        self.t.observe("2026-08-24", 0, 60.9, 5.0, store_full=True)

        back = DayTracker.from_raw(self.t.to_raw())
        done = back.observe("2026-08-25", 0, 55.0, 0.0)

        self.assertEqual(done, (5.0, 60.9, "2026-08-24", True))

    def test_garbage_gives_a_fresh_tracker(self):
        self.assertIsNone(DayTracker.from_raw("ikke en dag").date)


class StorageTest(unittest.TestCase):
    def test_round_trip(self):
        m = model(scale=0.412, days=7.0)

        back = SolarModel.from_raw(m.to_raw(), FYN)

        self.assertAlmostEqual(back.scale, 0.412, places=9)
        self.assertEqual(back.days, 7.0)

    def test_an_unlearned_model_survives_a_round_trip(self):
        back = SolarModel.from_raw(model().to_raw(), FYN)

        self.assertIsNone(back.scale)
        self.assertFalse(back.known)

    def test_garbage_gives_an_unlearned_model(self):
        self.assertIsNone(SolarModel.from_raw("ikke en model", FYN).scale)
        self.assertIsNone(SolarModel.from_raw({"scale": "aeh"}, FYN).scale)


if __name__ == "__main__":
    unittest.main()


class MinimumChargeTest(unittest.TestCase):
    """Mindste opladning der er vaerd at starte for."""

    def setUp(self):
        from dataclasses import replace
        from pathlib import Path

        from varmeopt.options import Options

        self.opts = Options.load(Path("findes-ikke.json"))
        self.replace = replace

    def test_minimum_follows_the_uvr_runtime(self):
        # 16 kW i 15 minutter er 4 kWh. Er der mindre plads end det, fylder
        # varmepumpen det og slukker igen.
        self.assertAlmostEqual(self.opts.min_charge_kwh, 4.0, places=6)

    def test_a_longer_minimum_runtime_raises_the_bar(self):
        slow = self.replace(self.opts, hp_min_runtime_minutes=30)

        self.assertAlmostEqual(slow.min_charge_kwh, 8.0, places=6)

    def test_a_modulating_pump_lowers_it(self):
        gentle = self.replace(self.opts, hp_charge_kw=4.0)

        self.assertAlmostEqual(gentle.min_charge_kwh, 1.0, places=6)


class SeedTest(unittest.TestCase):
    """Startvaerdien skal udledes, ikke skrives ned."""

    def test_the_seed_comes_out_of_the_calibration_day(self):
        # 24. august 2026: solcellerne lavede 60,9 kWh, solvarmen 29,0.
        seed = seed_scale(FYN)
        m = SolarModel(FYN, scale=seed, days=1.0)

        self.assertAlmostEqual(m.expected_kwh(60.9, 236), 29.0, places=6)

    def test_a_written_down_seed_goes_stale_when_the_geometry_moves(self):
        # Det er praecis det der skete i 0.19.0: den diffuse straaling kom
        # med, og 0,43 fra den gamle geometri blev 10 % for lavt.
        beam_only = seed_scale(FYN)

        self.assertGreater(beam_only, 0.43 * 1.05)
        self.assertAlmostEqual(beam_only, 0.476, places=3)

    def test_the_geometry_matches_the_plant(self):
        # Fire solfangere i syd med 45 grader, mod 6,4 kW syd/20 og
        # 4 kW vest/15, paa 55,4 grader nord.
        self.assertEqual(FYN.latitude, 55.4)
        self.assertEqual((FYN.thermal.tilt, FYN.thermal.azimuth), (45.0, 0.0))
        self.assertEqual([(p.tilt, p.azimuth, p.weight) for p in FYN.pv],
                         [(20.0, 0.0, 6.4), (15.0, 90.0, 4.0)])

